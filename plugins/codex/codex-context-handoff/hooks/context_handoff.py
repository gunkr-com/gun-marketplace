#!/usr/bin/env python3
"""Rotate an opted-in Codex CLI chat without replacing its Herdr terminal."""

import calendar
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TERMINAL = re.compile(r"t[0-9a-z]+\Z")
# Found anywhere in a prompt, so a continuation mixed with other text is caught too.
RESUME = re.compile(r"Codex Context Handoff: ([\w-]+) ([\w-]+) ([0-9a-f]{24})(?![0-9a-f])")
COMPOSER = re.compile(r"^›(?:\s+Ask Codex to do anything)?\s*$")
PLACEHOLDER = "› Ask Codex to do anything"
FOOTER = 4  # the prompt sits within the last 4 lines: prompt, blank, two status lines
WRAPPED_FOOTER = 8  # with the placeholder shown, a narrow terminal may wrap the status lines
DIALOG = ("›", "•", "╭", "│", "╰", "┌", "└", "esc to cancel")  # lines that are never part of the status
# A Herdr message is the whole prompt, typed by herdr-core: `<hc from="t7[ name]"[ reach="..."]>body</hc>`.
ENVELOPE = re.compile(r'\A<hc from="(t[0-9a-z]+)(?: ([^"\n]+))?"(?: reach="[^"\n]*")?>([\s\S]*)</hc>\Z')
# herdr-core's own notes to a direct parent about a child: exited, blocked, or hit an error.
CHILD_NOTE = re.compile(
    r"\A<hc>(t[0-9a-z]+)(?: ([^\n]+?))? (?:exited \((?:exit -?\d+|signal \d+)\)\.(?: Read: hc read \1"
    r"| Last lines:\n[\s\S]*\nRead: hc read \1)|is blocked \([^)\n]+\)\. See: hc read \1"
    r"|hit an error: [\s\S]*\. See: hc read \1)</hc>\Z"
)
WORKER_KINDS = {"worker", "worker-note"}
MAX_LINEAGE = 16
BUDGET = 3  # end-to-end attempts per handoff: the first request, then each note re-prompt or switch retry
RECEIPT_WAIT = 12.0
RECONCILE_WAIT = 20.0
RETRY_IDLE_WAIT = 60.0  # how long a retry waits for the next idle boundary
INPUT_SETTLE = 2.0  # how long a screen change may wait for its UserPromptSubmit hook to record it
HELD_IN_NOTE = 4000  # characters of each held worker message copied into the note (all of it is kept aside)
GOAL_WORKER_POLICY = "goal-worker-input.json"
TRANSCRIPT_TAIL = 4 * 1024 * 1024
MAX_NOTE = 12_000
POLL = 0.2
CLEAR_ECHO = 3.0  # seconds after /clear in which the old status and screen may still show
SETTLE = 2.0  # seconds the fresh chat must stay idle with an empty prompt before the continuation
# Submitted as the whole prompt, this reaches the model and arms a handoff (and a Goal transfer) right away.
NOW = {"handoff now", "핸드오프 지금"}
GOAL_TOOLS = {"get_goal", "update_goal", "create_goal"}
CODE_MODE = "exec"  # code mode's script tool; each nested tool call gets its own PreToolUse
MAX_ATTEMPTS = 3  # Goal transfer (or tool-boundary handoff) attempts per chat that user input may put off
PLAIN_RETRY = ".handoff-retry"  # put-off tool-boundary handoffs of a chat without an active Goal
RETRY_GROWTH = 5.0  # percentage points of context a put-off attempt waits for before trying again
MAX_DENIALS = 12  # denied tool calls before the gate gives up for the turn
CONTROLS = {
    "handoff off": "off", "handoff cancel": "off", "handoff on": "on", "handoff status": "status",
    "핸드오프 끄기": "off", "핸드오프 취소": "off", "핸드오프 켜기": "on", "핸드오프 상태": "status",
    "handoff goal transfer on": "goal-on", "handoff goal transfer off": "goal-off",
    "핸드오프 목표 이전 켜기": "goal-on", "핸드오프 목표 이전 끄기": "goal-off",
}
GOAL_STATUSES = {"active", "paused", "blocked", "usage_limited", "budget_limited", "complete"}
CONSENT = "goal-transfer.json"
CLOSING = {"end session", "wrap up", "세션 종료", "마무리"}
DONE = (".received", ".cancelled", ".closed")
RESTART = (
    "Exit Codex, then reopen this chat with `codex resume {session}` in this Herdr terminal so the "
    "launcher adds --no-daemon and CODEX_CONTEXT_HANDOFF=1."
)
HC_ENV = {key: value for key, value in os.environ.items() if key != "HC_TERMINAL_ID"}


def parse_rows(stream, kinds):
    for line in stream:
        if not any(kind in line for kind in kinds):
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and isinstance(row.get("payload"), dict):
            yield row.get("type"), row["payload"]


def transcript_rows(path, tail=TRANSCRIPT_TAIL, kinds=(b'"event_msg"', b'"compacted"')):
    with Path(path).open("rb") as stream:
        offset = max(stream.seek(0, os.SEEK_END) - tail, 0)
        stream.seek(offset)
        if offset:
            stream.readline()
        yield from parse_rows(stream, kinds)


def rows_after(path, offset):
    """Transcript rows written after byte `offset` (the transcript's size when a handoff was armed)."""
    with Path(path).open("rb") as stream:
        if type(offset) is not int or not 0 <= offset <= stream.seek(0, os.SEEK_END):
            return None
        stream.seek(max(offset - 1, 0))
        if offset and stream.read(1) != b"\n":
            stream.readline()  # armed while a line was being written: that line came before the arm
        return list(parse_rows(stream, (b'"event_msg"', b'"response_item"')))


def recent_turn(path):
    """Read the latest turn only, ignoring usage preceding a compaction.

    A long turn can start before the usual tail window; widen it until the turn's start shows.
    """
    tail = TRANSCRIPT_TAIL
    while True:
        turn = None
        for row_type, event in transcript_rows(path, tail):
            kind = event.get("type")
            if row_type == "compacted" and turn:
                turn["usage"] = None
            if row_type != "event_msg":
                continue
            if kind == "task_started":
                attribution = event.get("turn_attribution")
                trigger = attribution.get("turn_trigger") if isinstance(attribution, dict) else None
                turn = {"id": event.get("turn_id"), "usage": None, "complete": False, "trigger": trigger}
            elif turn and kind == "token_count":
                turn["usage"] = event.get("info")
            elif turn and kind == "task_complete" and event.get("turn_id") == turn["id"]:
                turn["complete"] = True
        if turn or tail >= os.path.getsize(path):
            return turn
        tail *= 4


def last_usage(path):
    usage = None
    for row_type, event in transcript_rows(path):
        if row_type == "compacted":
            usage = None
        elif event.get("type") == "token_count":
            usage = event.get("info")
    return usage


def is_interactive_root(transcript, session):
    with Path(transcript).open("rb") as stream:
        row = json.loads(stream.readline(1024 * 1024))
    if not isinstance(row, dict) or row.get("type") != "session_meta":
        return False
    meta = row.get("payload")
    return (
        isinstance(meta, dict)
        # A daemon-born chat resumed under --no-daemon keeps source "vscode"; the
        # foreground process-tree check still excludes real IDE and daemon hooks.
        and meta.get("source") in {"cli", "vscode"} and meta.get("thread_source") == "user"
        and meta.get("id") == session and meta.get("session_id") == session
    )

def usage_percent(usage):
    if not isinstance(usage, dict):
        return None
    last = usage.get("last_token_usage")
    used = last.get("total_tokens") if isinstance(last, dict) else None
    window = usage.get("model_context_window")
    if type(used) is int and used >= 0 and type(window) is int and window > 0:
        return 100 * used / window
    return None


def env_number(name, default, low, high, inclusive=False):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return None
    ok = low <= value <= high if inclusive else low < value < high
    return value if math.isfinite(value) and ok else None


def threshold():
    return env_number("CODEX_CONTEXT_HANDOFF_THRESHOLD", "40", 0, 100)


def grace():
    value = env_number("CODEX_CONTEXT_HANDOFF_GRACE", "10", 0, 60, inclusive=True)
    return 10.0 if value is None else value


def checkpoint_calls():
    value = env_number("CODEX_CONTEXT_HANDOFF_CHECKPOINT_CALLS", "3", 0, 10, inclusive=True)
    return 3 if value is None else int(value)


def min_progress():
    value = env_number("CODEX_CONTEXT_HANDOFF_MIN_PROGRESS", "10", 0, 90, inclusive=True)
    return 10.0 if value is None else value


def startup_wait():
    value = env_number("CODEX_CONTEXT_HANDOFF_STARTUP_WAIT", "150", 12, 600, inclusive=True)
    return 150.0 if value is None else value


def exceeds_threshold(usage, threshold):
    if not isinstance(usage, dict):
        return False
    last = usage.get("last_token_usage")
    used = last.get("total_tokens") if isinstance(last, dict) else None
    window = usage.get("model_context_window")
    return (
        type(used) is int and used >= 0 and type(window) is int and window > 0
        and 100 * used > threshold * window
    )


def hc(*args, timeout=8):
    result = subprocess.run(
        ["hc", *args], check=True, capture_output=True, text=True, timeout=timeout, env=HC_ENV
    )
    return json.loads(result.stdout)


def hc_list(timeout=3):
    """One bulk Herdr snapshot, live and finished terminals: {address: terminal}."""
    result = subprocess.run(
        ["hc", "ls", "--json", "--all"], check=True, capture_output=True, text=True, timeout=timeout, env=HC_ENV
    )
    data = json.loads(result.stdout)
    entries = data.get("terminals", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("unexpected hc ls output")
    return {e["address"]: e for e in entries if isinstance(e, dict) and isinstance(e.get("address"), str)}


class Snapshot:
    """A Herdr snapshot taken at most once per hook run, and only when needed; None if hc fails."""

    def __init__(self, timeout=3):
        self.timeout, self.taken, self.value, self.error = timeout, False, None, None

    def get(self):
        if not self.taken:
            self.taken = True
            try:
                self.value = hc_list(self.timeout)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.error = f"{type(error).__name__}: {error}"
        return self.value


def lineage(snapshot, address, root):
    """The chain from `address` up to `root` (root excluded) when `root` is an ancestor, else None."""
    chain, current = [], address
    while current not in chain and len(chain) < MAX_LINEAGE:
        entry = snapshot.get(current)
        if not isinstance(entry, dict):
            return None
        chain.append(current)
        parent = entry.get("parent")
        if parent == root:
            return chain
        if not isinstance(parent, str) or not parent:
            return None
        current = parent
    return None


def classify_input(text, terminal, snapshot, known=None):
    """(kind, sender, why) for one submitted message.

    Herdr types its messages into the terminal like a person would, and Codex records them as user
    input with no sender metadata. The evidence available is the envelope herdr-core writes around the
    whole message and its own record of which terminal started which (`parent`, set once at start).
    `worker`: an `<hc from=...>` message from a strict descendant of this terminal; `worker-note`:
    herdr-core's own note about a direct child in a state that fits the note (or, for a child removed
    since, its entry in this handoff's manifest). `parent` and `foreign` senders give directions, so
    they count as user input, like `human` (no envelope) and `unverified` (malformed, mixed with other
    text, or not checkable). `known` maps manifest addresses to their recorded entries.
    """
    text = (text or "").strip()
    if not text.startswith("<hc"):
        return "human", None, "no Herdr envelope"
    envelope = ENVELOPE.fullmatch(text)
    note = None if envelope else CHILD_NOTE.fullmatch(text)
    if not envelope and not note:
        return "unverified", None, "not exactly one Herdr message"
    found = envelope or note
    sender, name = found.group(1), found.group(2)
    if envelope and ("<hc from=" in envelope.group(3) or "<hc>" in envelope.group(3)):
        return "unverified", sender, "more than one Herdr message"
    if not TERMINAL.fullmatch(terminal or "") or sender == terminal:
        return "unverified", sender, "not checkable from this terminal"
    terminals = snapshot.get()
    if terminals is None:
        return "unverified", sender, f"Herdr could not be read ({snapshot.error})"
    entry = terminals.get(sender)
    if entry is None and note:
        entry = (known or {}).get(sender)
        if not isinstance(entry, dict):
            return "unverified", sender, "Herdr does not know the sender"
        source = "this handoff's worker manifest"
    elif entry is None:
        return "unverified", sender, "Herdr does not know the sender"
    else:
        source = "Herdr"
    if name is not None and entry.get("name") != name:
        return "unverified", sender, f"the sender's name does not match {source}"
    if note:
        if entry.get("parent") != terminal:
            return "unverified", sender, "Herdr notes come only from direct children"
        exited = " exited (" in text.split(">", 1)[1].split("\n", 1)[0]
        state = entry.get("state")
        if source == "Herdr" and exited and state != "finished":
            return "unverified", sender, "an exit note for a terminal Herdr shows running"
        if source == "Herdr" and not exited and state != "live":
            return "unverified", sender, "a blocked or error note for a terminal that has finished"
        if source != "Herdr" and not exited:
            return "unverified", sender, "only an exit note can come from a removed child"
        return "worker-note", sender, f"Herdr's note about a direct child ({source})"
    chain = lineage(terminals, sender, terminal)
    if chain:
        return "worker", sender, "descendant " + " < ".join([*chain, terminal])
    if lineage(terminals, terminal, sender):
        return "parent", sender, "an ancestor of this terminal"
    return "foreign", sender, "not started from this terminal"


class InputJudge:
    """Classifies the user-role messages of one handoff; verdicts are kept in `<state>.inputs.json`.

    Envelope verdicts are cached by text digest, so the gate asks Herdr at most once per new message
    and a sender removed later cannot change a verdict. A verdict reached while Herdr could not be read
    is not cached (it is `unverified`, which counts as user input, so the attempt is settled anyway).
    """

    def __init__(self, state, terminal):
        self.state, self.terminal, self.snapshot = state, terminal, Snapshot()
        self.path = state.with_suffix(".inputs.json") if state else None
        self.cache = (read_json(self.path) if self.path else None) or {}

    def verdict(self, text):
        text = (text or "").strip()
        if not text.startswith("<hc"):
            return {"kind": "human", "sender": None, "why": "no Herdr envelope"}
        key = digest(text)
        hit = self.cache.get(key)
        if isinstance(hit, dict) and isinstance(hit.get("kind"), str):
            return hit
        kind, sender, why = classify_input(text, self.terminal, self.snapshot, manifest_entries(self.state))
        verdict = {"kind": kind, "sender": sender, "why": why, "at": utc_now()}
        if self.path and self.snapshot.error is None:
            self.cache[key] = verdict
            try:
                with open_lock(self.state.with_suffix(".inputs-lock")):
                    stored = read_json(self.path) or {}
                    stored[key] = verdict
                    replace_private(self.path, json.dumps(stored))
            except OSError:
                pass
        return verdict

    def kind(self, text):
        return self.verdict(text)["kind"]


def manifest_entries(state):
    """{address: recorded entry} from this handoff's worker manifest (empty when there is none)."""
    data = read_json(state.with_suffix(".workers.json")) if state else None
    workers = data.get("workers") if data else None
    if not isinstance(workers, list):
        return {}
    return {w["address"]: w for w in workers if isinstance(w, dict) and isinstance(w.get("address"), str)}


def goal_worker_policy(root):
    """`exempt` once the user said verified worker messages may not put off a Goal transfer; `intervene`
    (the default, matching the standing instruction that worker messages count as intervening input)."""
    data = read_json(root / GOAL_WORKER_POLICY)
    return "exempt" if data and data.get("policy") == "exempt" else "intervene"


def input_counter(state, record, paused=False):
    """A function telling whether a user-role message after the arm counts as input against it.

    Verified worker messages never count for a handoff without a Goal transfer, nor after the
    transferred Goal was paused; before the pause they count unless the Goal worker policy is exempt.
    """
    judge = InputJudge(state, record.get("terminal", ""))
    exempt = not goal_moves(record) or paused or goal_worker_policy(state.parent.parent) == "exempt"

    def counts(text):
        return not (exempt and judge.kind(text) in WORKER_KINDS)
    return counts


def same_cwd(left, right):
    return isinstance(left, str) and isinstance(right, str) and (
        Path(left).resolve() == Path(right).resolve()
    )


def in_foreground_tree(pid):
    current = os.getpid()
    while current > 1:
        if current == pid:
            return True
        row = subprocess.check_output(
            ["ps", "-o", "ppid=", "-o", "comm=", "-p", str(current)], text=True, timeout=2
        )
        parent, command = row.strip().split(maxsplit=1)
        if Path(command).name == "codex":
            return False
        current = int(parent)
    return False


def terminal_info(terminal, cwd, expected_pid=None):
    info = hc("get", terminal)
    foreground = info.get("foreground") or {}
    if (
        info.get("state") != "live" or foreground.get("name") != "codex"
        or not same_cwd(foreground.get("cwd"), cwd)
    ):
        return None
    pid = foreground.get("pid")
    if type(pid) is not int or (expected_pid is not None and expected_pid != pid):
        return None
    return info


def new_private_file(path, content):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    return True


def store_for(root, session, turn):
    if not ID.fullmatch(session) or not ID.fullmatch(turn):
        raise ValueError("invalid session or turn id")
    return root / session / turn


def handoff_request(nonce):
    return (
        "The context threshold was crossed. Prepare a handoff now if any work can continue "
        "without user input, including work reserved for the fresh chat. Do not perform that "
        "remaining work in this chat. Start your final answer with this exact plain-text line "
        "so the handoff can run:\n"
        f"HANDOFF READY {nonce}\n"
        "Then write a short Markdown handoff with the goal, user constraints, decisions, "
        "worktree and branch, changes, validation, next action, and the IDs and state of any "
        "live Herdr workers. Include only necessary work context. If everything is complete, "
        "the user asked to end, or the remaining work needs the user or an approval, respond "
        "normally and omit the marker."
    )


def nudge(limit):
    return (
        f"Codex Context Handoff: this chat has passed {limit:g}% of its context window. Do not "
        "abandon the current step. If work will remain after this turn, bring it to a safe "
        "checkpoint (no half-applied edits; note running workers), avoid starting large new "
        "subtasks, and end the turn soon; the Stop hook will then ask for a handoff note and "
        "continue in a fresh chat. If the task is nearly done, finish it normally."
    )


def ids(payload):
    session = payload.get("session_id") or ""
    turn = payload.get("turn_id") or ""
    return (session, turn) if ID.fullmatch(session) and ID.fullmatch(turn) else (None, None)


def disabled(root, session, turn):
    return (root / session / ".off").exists() or (root / session / f"{turn}.skip").is_file()


def check_chat(payload, terminal):
    """Return (Herdr info, None) for the opted-in foreground root chat, else (None, why not)."""
    session = payload.get("session_id") or ""
    if os.environ.get("CODEX_CONTEXT_HANDOFF") != "1":
        return None, (
            "this Codex process was not started with CODEX_CONTEXT_HANDOFF=1 and --no-daemon "
            "(a daemon-backed session keeps its daemon's stale launch environment). "
            + RESTART.format(session=session)
        )
    if not TERMINAL.fullmatch(terminal):
        return None, "this Codex process is not running in a Herdr terminal."
    if threshold() is None:
        return None, "CODEX_CONTEXT_HANDOFF_THRESHOLD must be a number between 0 and 100."
    transcript, cwd = payload.get("transcript_path"), payload.get("cwd")
    if not transcript or not cwd or not is_interactive_root(transcript, session):
        return None, "this is not an interactive root Codex chat."
    info = terminal_info(terminal, cwd)
    if not info or not in_foreground_tree(info["foreground"]["pid"]):
        return None, (
            f"the hooks do not belong to the foreground Codex of Herdr terminal {terminal} in this "
            "worktree. " + RESTART.format(session=session)
        )
    return info, None


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def goals_db(transcript):
    """`<CODEX_HOME>/goals_1.sqlite` for a `<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl` transcript."""
    override = os.environ.get("CODEX_HANDOFF_GOALS_DB")
    if override:
        return Path(override)
    path = Path(transcript)
    parents = path.parents
    if (
        path.name.startswith("rollout-") and len(parents) > 4 and parents[3].name == "sessions"
        and all(parent.name.isdigit() for parent in parents[:3])
    ):
        return parents[4] / "goals_1.sqlite"
    return None


def goal_state(transcript, session, trigger=None):
    """("none", None), ("unknown", why) or ("row", goal) from a read-only look at Codex's Goal DB."""
    db = goals_db(transcript)
    if db is None:
        return "unknown", "the transcript is not in a Codex sessions directory"
    row = None
    if db.exists():
        try:
            connection = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=1)
            try:
                row = connection.execute(
                    "SELECT goal_id, objective, status, token_budget, tokens_used, time_used_seconds, created_at_ms, "
                    "updated_at_ms FROM thread_goals WHERE thread_id = ?", (session,),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as error:
            return "unknown", f"{type(error).__name__}: {error}"
    if row is None:
        if trigger == "goal":
            return "unknown", "this turn was started by a Goal but no Goal row was found"
        return "none", None
    goal_id, objective, status, budget, used, seconds, created, updated = row
    if (
        not isinstance(goal_id, str) or not isinstance(objective, str) or status not in GOAL_STATUSES
        or not (budget is None or type(budget) is int)
        or any(type(value) is not int for value in (used, seconds, created, updated))
    ):
        return "unknown", "unexpected Goal row"
    return "row", {
        "goal_id": goal_id, "status": status, "objective": objective, "objective_sha256": digest(objective),
        "token_budget": budget, "tokens_used": used, "time_used_seconds": seconds,
        "created_at_ms": created, "updated_at_ms": updated,
    }


def consent_file(root):
    """The plugin-wide Goal transfer consent as written by `handoff goal transfer on`, or None."""
    try:
        data = json.loads((root / CONSENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("enabled") is True and isinstance(data.get("since"), str) else None


def goal_killed():
    return os.environ.get("CODEX_CONTEXT_HANDOFF_GOAL", "").strip().lower() in {"off", "0", "false", "no"}


def goal_consent(root):
    # The environment can only turn transfer off; consent is the persisted file.
    return None if goal_killed() else consent_file(root)


def goal_wait(folder, why, marker=".goal-wait"):
    """Tell the chat once why no handoff will be requested; later Stops stay silent."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not new_private_file(folder / marker, why + "\n"):
        return {}
    return {"systemMessage": f"Context handoff is waiting: {why}. Codex auto-compaction will handle context."}


def goal_request(goal, nonce):
    if not goal:
        return ""
    if goal["mode"] == "left":
        return (
            f"\nThere is a Goal in this chat with status {goal['arm']['status']}. Do not change it and do not "
            "mention recreating it."
        )
    return (
        "\n\nGOAL TRANSFER (authorized by the user's standing Goal transfer instruction in AGENTS.md; "
        f"plugin consent since {goal['since']}):\n"
        "1. Call get_goal and print its raw result with text().\n"
        '2. Only if its status is "active": call update_goal with status "paused" and print its raw result. '
        "This pause only moves the Goal to the fresh chat. Do no more Goal work after it.\n"
        f"3. Start your final answer with: HANDOFF READY {nonce} GOAL transferred\n"
        f'   If the status was not "active", do not change it and use: HANDOFF READY {nonce} GOAL left\n'
        "Do not create, resume or edit any Goal in this chat."
    )


def handoff_turn_rows(path, nonce):
    """Transcript rows after our handoff request in the current turn, or None if it is not found.

    The request shows as a user-role <hook_prompt> message and an item_completed HookPrompt event,
    possibly combined with other plugins' Stop requests.
    """
    needle = f"HANDOFF READY {nonce}"
    tail = TRANSCRIPT_TAIL
    while True:
        rows, found = [], False
        for row_type, item in transcript_rows(path, tail, (b'"event_msg"', b'"response_item"')):
            if is_request(row_type, item, needle):
                rows, found = [], True
            else:
                rows.append((row_type, item))
        if found:
            return rows
        if tail >= os.path.getsize(path):
            return None
        tail *= 4


def is_request(row_type, item, needle):
    if row_type == "event_msg" and item.get("type") == "item_completed":
        hook = item.get("item")
        return isinstance(hook, dict) and hook.get("type") == "HookPrompt" and any(
            isinstance(part, dict) and needle in str(part.get("text", "")) for part in hook.get("fragments") or ()
        )
    if row_type == "response_item" and item.get("type") == "message" and item.get("role") == "user":
        return any(
            isinstance(part, dict) and str(part.get("text", "")).startswith("<hook_prompt")
            and needle in part["text"] for part in item.get("content") or ()
        )
    return False


def message_text(content):
    return "".join(
        str(part.get("text", "")) for part in content or () if isinstance(part, dict)
    ) if isinstance(content, list) else ""


def user_input(rows, exempt=None, counts=None):
    """Whether the user acted after our request: a prompt or steer (UserMessage), a /goal change
    (a user-role <codex_internal_context source="user_goal">), or any other user-role message that is
    not a hook prompt Codex itself recorded (paired with a HookPrompt event of the same id).

    `exempt` is the exact prompt that armed the handoff (`handoff now`), which is not an objection to it.
    `counts(text)` may rule a message out (a verified Herdr worker message, see `input_counter`).
    """
    def objects(content):
        if exempt is not None and " ".join(message_text(content).split()) == exempt:
            return False
        return counts is None or counts(message_text(content))

    hooks = set()
    for row_type, item in rows:
        if row_type == "event_msg" and item.get("type") == "item_completed" and isinstance(item.get("item"), dict):
            if item["item"].get("type") == "UserMessage" and objects(item["item"].get("content")):
                return True
            if item["item"].get("type") == "HookPrompt":
                hooks.add(item["item"].get("id"))
    return any(
        row_type == "response_item" and item.get("type") == "message" and item.get("role") == "user"
        and not (item.get("id") in hooks and all(
            isinstance(part, dict) and str(part.get("text", "")).startswith("<hook_prompt")
            for part in item.get("content") or ()
        ))
        and objects(item.get("content"))
        for row_type, item in rows
    )


def pause_index(rows):
    """The index just past the first tool output that printed the Goal paused by `update_goal paused`
    (len(rows) when there is none): input before it may put a transfer off, input after it may not."""
    calls = {}
    for index, (row_type, item) in enumerate(rows):
        if row_type != "response_item":
            continue
        kind, call = item.get("type"), item.get("call_id")
        if kind == "custom_tool_call" and item.get("name") == "exec":
            code = str(item.get("input") or "")
            calls[call] = "tools.update_goal" in code and "paused" in code
        elif kind == "function_call" and item.get("name") == "update_goal":
            calls[call] = "paused" in str(item.get("arguments") or "")
        elif kind in {"custom_tool_call_output", "function_call_output"} and calls.get(call):
            if any(goal.get("status") == "paused" for goal in output_goals(item.get("output"))):
                return index + 1
    return len(rows)


def objected(rows, record, state):
    """Whether input after the arm stands against this handoff (see `input_counter`)."""
    prompt = record.get("prompt")
    if not goal_moves(record):
        return user_input(rows, prompt, input_counter(state, record))
    cut = pause_index(rows)
    return user_input(rows[:cut], prompt, input_counter(state, record)) or user_input(
        rows[cut:], prompt, input_counter(state, record, paused=True)
    )


def armed_rows(record):
    """The evidence rows for a handoff: after our Stop request, or after the arm for one armed mid-turn.

    A record armed mid-turn (by the tool gate, `handoff now` or the transfer helper) has no persisted
    request, so its boundary is the transcript size at the arm. A tool call that was running at the
    arm (the helper's own call, or a script whose nested call the gate denied) is included when its
    output lands after the arm; the Goal was active in the DB at the arm, so a pause it printed
    happened after the arm.
    """
    if record.get("origin") not in {"tool", "manual"}:
        return handoff_turn_rows(record["transcript"], record["nonce"])
    after = rows_after(record["transcript"], record["offset"])
    if after is None:
        return None
    outputs = {item.get("call_id") for row_type, item in after if row_type == "response_item"
               and item.get("type") in {"custom_tool_call_output", "function_call_output"}}
    calls = {item.get("call_id") for row_type, item in after if row_type == "response_item"
             and item.get("type") in {"custom_tool_call", "function_call"}}
    return calls_before(record["transcript"], record["offset"], outputs - calls) + after


def calls_before(path, offset, wanted):
    """The tool call rows written before `offset` for the given call ids (a call running at the arm),
    looking back as far as needed (a long script can start well before the arm)."""
    found, tail = {}, TRANSCRIPT_TAIL
    while wanted - set(found):
        with Path(path).open("rb") as stream:
            start = max(offset - tail, 0)
            stream.seek(start)
            if start:
                stream.readline()
            while stream.tell() < offset:
                line = stream.readline()
                if not line:
                    break
                for row_type, item in parse_rows([line], (b'"response_item"',)):
                    if item.get("type") in {"custom_tool_call", "function_call"} and item.get("call_id") in wanted:
                        found[item["call_id"]] = item
        if start == 0:
            break
        tail *= 4
    return [("response_item", item) for item in found.values()]


def goal_calls(rows):
    """(update_goal, create_goal) invocations in the model's tool calls."""
    updates = creates = 0
    for row_type, item in rows:
        if row_type != "response_item":
            continue
        if item.get("type") == "custom_tool_call":
            code = str(item.get("input") or "")
            updates, creates = updates + code.count("update_goal"), creates + code.count("create_goal")
        elif item.get("type") == "function_call":
            updates += item.get("name") == "update_goal"
            creates += item.get("name") == "create_goal"
    return updates, creates


def output_goals(output):
    """Goal objects printed in a tool output, in order (one `{"goal":...}` JSON line each)."""
    if isinstance(output, str):
        texts = [output]
    elif isinstance(output, list):
        texts = [part.get("text") for part in output if isinstance(part, dict) and isinstance(part.get("text"), str)]
    else:
        return []
    goals = []
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith('{"goal":'):
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("goal"), dict):
                goals.append(value["goal"])
    return goals


def goal_snapshots(rows):
    """(goal, came from an update_goal call asking for paused) for each Goal tool output, in order."""
    calls, snapshots = {}, []
    for row_type, item in rows:
        if row_type != "response_item":
            continue
        kind, call = item.get("type"), item.get("call_id")
        if not isinstance(call, str):
            continue
        if kind == "custom_tool_call" and item.get("name") == "exec":
            code = str(item.get("input") or "")
            if "tools.get_goal" in code or "tools.update_goal" in code:
                calls[call] = "tools.update_goal" in code and "paused" in code
        elif kind == "function_call" and item.get("name") in {"get_goal", "update_goal"}:
            calls[call] = item["name"] == "update_goal" and "paused" in str(item.get("arguments") or "")
        elif kind in {"custom_tool_call_output", "function_call_output"} and call in calls:
            update = calls.pop(call)
            snapshots.extend((goal, update) for goal in output_goals(item.get("output")))
    return snapshots


def prior_lineage(folder, goal):
    """The lineage this chat's Goal arrived with, if it was a verified restore of the same Goal."""
    try:
        restored = json.loads((folder / ".goal-restored").read_text(encoding="utf-8"))
        lineage = json.loads((folder / ".goal-lineage.json").read_text(encoding="utf-8"))
        if (
            restored["objective_sha256"] == goal["objective_sha256"] == lineage["objective_sha256"]
            and restored["created_at_ms"] == goal["created_at_ms"] and restored["goal_id"] == goal["goal_id"] and ID.fullmatch(lineage["root_session"])
            and all(type(lineage[key]) is int for key in ("hops", "total_tokens_used", "total_time_used_seconds"))
        ):
            return lineage
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def verify_transfer(record, state):
    """(transfer, None) when the rollout and the DB prove the armed Goal was paused for us, else (None, why)."""
    arm, session = record["goal"]["arm"], record["session"]
    rows = armed_rows(record)
    if rows is None:
        return None, "the handoff request was not found in the transcript"
    if objected(rows, record, state):
        return None, "user input arrived during the handoff turn"
    # Exactly the one pause asked for: no second update, no create, so nothing changed after it.
    if goal_calls(rows) != (1, 0):
        return None, "the Goal tools were not called exactly as requested (one update_goal, no create_goal)"

    def armed(goal):
        objective = goal.get("objective")
        return (
            goal.get("threadId") == session and goal.get("createdAt") == arm["created_at_ms"] // 1000
            and isinstance(objective, str) and digest(objective) == arm["objective_sha256"]
        )

    snapshots = goal_snapshots(rows)
    paused = next((index for index, (goal, _) in enumerate(snapshots) if goal.get("status") == "paused"), None)
    if paused is None:
        return None, "no paused Goal in the tool output"
    goal, from_update = snapshots[paused]
    if not from_update:
        return None, "the first paused Goal was not printed by update_goal paused"
    if not armed(goal):
        return None, "the paused Goal is not the Goal armed for transfer"
    # A mid-turn arm read the Goal as active from the DB at the arm, before any row counted here.
    if record.get("origin") not in {"tool", "manual"} and not any(
        earlier.get("status") == "active" and armed(earlier) for earlier, _ in snapshots[:paused]
    ):
        return None, "no active Goal was printed before the pause"
    status, row = goal_state(record["transcript"], session)
    if status != "row":
        return None, f"the Goal DB shows {status}" + (f" ({row})" if row else "")
    if (
        row["status"] != "paused" or row["created_at_ms"] != arm["created_at_ms"]
        or row["objective_sha256"] != arm["objective_sha256"] or row["token_budget"] != arm["token_budget"]
        or row["goal_id"] != arm["goal_id"] or row["token_budget"] != goal.get("tokenBudget")
        or row["updated_at_ms"] // 1000 != goal.get("updatedAt") or row["tokens_used"] != goal.get("tokensUsed")
        or row["time_used_seconds"] != goal.get("timeUsedSeconds")
    ):
        return None, "the Goal DB does not match the paused Goal"
    budget = row["token_budget"]
    remaining = None if budget is None else budget - row["tokens_used"]
    if remaining is not None and remaining <= 0:
        return None, "the Goal's token budget is exhausted"
    lineage = {"root_session": session, "hops": 1, "total_tokens_used": row["tokens_used"],
               "total_time_used_seconds": row["time_used_seconds"]}
    prior = prior_lineage(state.parent, row)
    if prior:
        lineage = {"root_session": prior["root_session"], "hops": prior["hops"] + 1,
                   "total_tokens_used": prior["total_tokens_used"] + row["tokens_used"],
                   "total_time_used_seconds": prior["total_time_used_seconds"] + row["time_used_seconds"]}
    return {
        "source_session": session, "source_turn": record["turn"], "goal_id": row["goal_id"], "objective": row["objective"],
        "objective_sha256": row["objective_sha256"], "created_at_ms": row["created_at_ms"],
        "paused_at_ms": row["updated_at_ms"], "token_budget": budget, "tokens_used": row["tokens_used"],
        "time_used_seconds": row["time_used_seconds"], "remaining": remaining, "lineage": lineage,
    }, None


def verify_left(record):
    """Why a Goal that was not active at arm changed during the handoff turn, or None."""
    arm = record["goal"]["arm"]
    rows = armed_rows(record)
    if rows is None:
        return "the handoff request was not found in the transcript"
    if goal_calls(rows) != (0, 0):
        return "update_goal or create_goal was called"
    status, row = goal_state(record["transcript"], record["session"])
    if status != "row" or any(
        row[key] != arm.get(key) for key in ("goal_id", "status", "created_at_ms", "updated_at_ms", "objective_sha256")
    ):
        return "the Goal changed"
    return None


def create_goal_args(transfer):
    args = {"objective": transfer["objective"]}
    if transfer["remaining"] is not None:
        args["token_budget"] = transfer["remaining"]
    return json.dumps(args, ensure_ascii=False)


def goal_note(transfer):
    lineage = transfer["lineage"]
    budget = (
        f"token_budget {transfer['remaining']}" if transfer["remaining"] is not None
        else "no token_budget (the Goal is unbounded; omit it)"
    )
    return (
        "\n## Goal transfer\n\n"
        f"- Objective (JSON string): {json.dumps(transfer['objective'], ensure_ascii=False)}\n"
        f"- create_goal: {budget}; exact arguments {create_goal_args(transfer)}\n"
        f"- This segment (chat {transfer['source_session']}): {transfer['tokens_used']} tokens, "
        f"{transfer['time_used_seconds']} s.\n"
        f"- Lineage: {lineage['hops']} transfer(s) from chat {lineage['root_session']}, "
        f"{lineage['total_tokens_used']} tokens and {lineage['total_time_used_seconds']} s in all.\n"
    )


def goal_paused_line(state):
    """The cancellation notice for a handoff whose source Goal was paused for transfer."""
    if not state.with_suffix(".goal.json").is_file() or state.with_suffix(".received").exists():
        return ""
    return (
        f"The Goal of chat {state.parent.name} was paused for this handoff and stays paused; resume it with "
        "/goal in that chat to continue there."
    )


def consent_revoked(state):
    """Why a handoff that carries a paused Goal may not go on: transfer consent was withdrawn since."""
    if state.with_suffix(".goal.json").is_file() and not goal_consent(state.parent.parent):
        return "Goal transfer was turned off"
    return None


def goal_failure(state, record, why):
    """Fail a Goal transfer closed: no switch, one message, and no further requests in this chat."""
    status, row = goal_state(record["transcript"], record["session"])
    if status == "row" and row["status"] == "complete":
        new_private_file(state.with_suffix(".closed"), f"goal completed instead of transferred: {why}\n")
        return {"systemMessage": "Context handoff: the Goal was completed, so nothing was transferred."}
    if status == "row" and row["status"] == "active":
        reason = "active goal was not paused"
        message = "Context handoff skipped: the active Goal was not paused for transfer. Work continues here."
    elif status == "unknown":
        reason = f"goal state unknown ({why})"
        message = (
            "Context handoff stopped: the Goal state could not be read, so the Goal may be paused and its "
            "transfer could not be verified. If it is paused, resume it with /goal to continue here."
        )
    elif status == "row" and row["status"] == "paused":
        reason = f"goal transfer not verified: {why}"
        message = (
            "Context handoff stopped: the Goal is paused but its transfer could not be verified. Resume it "
            "with /goal to continue here."
        )
    else:
        reason = f"goal transfer not verified: {why}"
        message = (
            f"Context handoff stopped: the Goal transfer could not be verified ({why}); the Goal is now "
            f"{row['status'] if status == 'row' else 'gone'}."
        )
    new_private_file(state.with_suffix(".closed"), reason + "\n")
    new_private_file(state.parent / ".goal-wait", reason + "\n")
    return {"systemMessage": message}


def restore_instruction(transfer, lead):
    return (
        f"Codex Context Handoff: {lead} Call create_goal with exactly these arguments and print its raw "
        f"result: {create_goal_args(transfer)}. This recreates the Goal paused in chat {transfer['source_session']} "
        "for the handoff (authorized by the user's standing Goal transfer instruction). Do not create, pause or "
        "edit any other Goal."
    )


def recreated(row, expected):
    # A fresh chat has no Goal of its own, so an exact objective and budget prove the recreation; the
    # Goal may already have finished natively (complete, budget_limited) by this chat's first Stop.
    return (
        row["status"] != "paused" and row["objective"] == expected["objective"]
        and row["token_budget"] == expected["remaining"]
    )


def restored_record(row, expected):
    return json.dumps({
        "goal_id": row["goal_id"], "objective_sha256": row["objective_sha256"], "created_at_ms": row["created_at_ms"],
        "source": expected["source_session"], "status": row["status"],
    })


def pending_restore(folder):
    """(path, expected Goal or None if unreadable) for a chat waiting to confirm a transferred Goal."""
    path = folder / ".goal-restore"
    if not path.is_file():
        return None, None
    try:
        expected = json.loads(path.read_text(encoding="utf-8"))
        create_goal_args(expected)
        if not ID.fullmatch(expected["source_session"]) or not isinstance(expected["objective"], str):
            raise ValueError("bad restore record")
    except (OSError, ValueError, KeyError, TypeError):
        expected = None
    return path, expected


def received_worker_message(payload, root, terminal, judges=None):
    """Whether the prompt is a verified worker message, judged with the received handoff's manifest (a
    removed child's exit note) and one Herdr snapshot shared by the whole hook run (`judges`)."""
    prompt = (payload.get("prompt") or "").strip()
    session = payload.get("session_id") or ""
    if not prompt.startswith("<hc") or not ID.fullmatch(session):
        return False
    judges = {} if judges is None else judges
    if "judge" not in judges:
        state, _ = received_handoff(root / session, root)
        judges["judge"] = InputJudge(state, terminal)
    return judges["judge"].kind(prompt) in WORKER_KINDS


def is_control(payload):
    return bool(CONTROLS.get(" ".join((payload.get("prompt") or "").split()).lower()))


def cancel_restore(payload, root, terminal="", judges=None):
    """Any prompt after the continuation ends the restore check, so no reminder lands in a new task.
    A verified Herdr worker message is not the user's next task, and a `handoff` control never reaches
    the model, so both leave the check pending."""
    session = payload.get("session_id") or ""
    if not ID.fullmatch(session):
        return None
    folder = root / session
    path, expected = pending_restore(folder)
    if path is None:
        return None
    if is_control(payload) or received_worker_message(payload, root, terminal, judges):
        return None
    if expected and payload.get("transcript_path"):
        status, row = goal_state(payload["transcript_path"], session)
        if status == "row" and recreated(row, expected):
            new_private_file(folder / ".goal-restored", restored_record(row, expected))
            path.unlink(missing_ok=True)
            return None
    new_private_file(folder / ".goal-mismatch", "a prompt arrived before the Goal was confirmed\n")
    path.unlink(missing_ok=True)
    source = f"chat {expected['source_session']}" if expected else "the old chat"
    return (
        "Context handoff: a prompt arrived before the transferred Goal was recreated, so the plugin no longer "
        f"asks for it. The Goal of {source} stays paused; resume it there with /goal, or create it here."
    )


def restore_check(payload, root, session):
    """In a chat that received a Goal, confirm the Goal was recreated exactly; at most one reminder."""
    folder = root / session
    path, expected = pending_restore(folder)
    if path is None:
        return None

    def finish(marker, content, message=None):
        new_private_file(folder / marker, content)
        path.unlink(missing_ok=True)
        return {"systemMessage": message} if message else None

    if expected is None:
        return finish(".goal-mismatch", "unreadable restore record\n", (
            "Context handoff: the transferred Goal's record could not be read. The old chat's Goal stays "
            "paused; create it with /goal."
        ))
    if not goal_consent(root):
        return finish(".goal-mismatch", "Goal transfer was turned off\n", (
            "Context handoff: Goal transfer was turned off, so the transferred Goal is no longer checked here. "
            f"The Goal of chat {expected['source_session']} stays paused."
        ))

    status, row = goal_state(payload["transcript_path"], session)
    if status == "unknown":
        return finish(".goal-mismatch", f"goal state unknown: {row}\n", (
            f"Context handoff: could not read the Goal state to confirm the transferred Goal ({row}). The old "
            "chat's Goal stays paused; check /goal."
        ))
    if status == "none":
        if new_private_file(folder / ".goal-reminded", ""):
            return {"decision": "block", "reason": restore_instruction(expected, "the transferred Goal does not exist yet.")}
        return finish(".goal-mismatch", "not recreated\n", (
            "Goal not recreated; the old chat's Goal stays paused; create it with /goal."
        ))
    if recreated(row, expected):
        return finish(".goal-restored", restored_record(row, expected), None if row["status"] in {
            "active", "complete", "budget_limited",
        } else (
            f"Context handoff: the transferred Goal was recreated here and is now {row['status']}."
        ))
    why = (
        f"status {row['status']}" if row["status"] == "paused"
        else "different objective" if row["objective"] != expected["objective"]
        else f"token_budget {row['token_budget']} instead of {expected['remaining']}"
    )
    return finish(".goal-mismatch", why + "\n", (
        f"Context handoff: the Goal in this chat does not match the transferred Goal ({why}). The old chat's "
        "Goal stays paused; fix it with /goal."
    ))


def baseline(folder, transcript):
    """Context use at the first request of a chat that received a handoff; None for any other chat."""
    if not (folder / ".received-from").is_file():
        return None
    cached = folder / ".baseline"
    try:
        return float(cached.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    for row_type, event in transcript_rows(transcript, os.path.getsize(transcript)):
        if row_type == "event_msg" and event.get("type") == "token_count":
            used = usage_percent(event.get("info"))
            if used is not None:
                new_private_file(cached, f"{used}\n")
                return used
    return None


def headroom(folder, transcript, usage, limit):
    """Whether this chat may hand off now: past the threshold, and for a chat that received a handoff
    whose first request was already past it, after real progress since (no handoff loops)."""
    if not exceeds_threshold(usage, limit):
        return False
    base = baseline(folder, transcript)
    return base is None or base <= limit or usage_percent(usage) >= base + min_progress()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def transfer_blocked(folder, used):
    """Why an automatic Goal transfer may not be armed in this chat now, or None."""
    if (folder / ".goal-wait").exists():
        return "waiting"
    if (folder / ".goal-requested").exists():
        return "requested"
    return retry_blocked(folder / ".goal-retry", used)


def retry_blocked(retry, used):
    """Why a put-off attempt may not be armed again yet (attempts used up, or too little growth), or None."""
    if retry.exists():
        data = read_json(retry)
        if not data or type(data.get("count")) is not int or data["count"] >= MAX_ATTEMPTS:
            return "attempts used up"
        if used is None or used < float(data.get("usage") or 0) + RETRY_GROWTH:
            return "waiting for more context use"
    return None


def confirm_restore(folder, transcript, session):
    """True unless this chat still waits for a transferred Goal to be recreated (confirmed here if it was)."""
    path, expected = pending_restore(folder)
    if path is None:
        return True
    if expected is None:
        return False
    status, row = goal_state(transcript, session)
    if status == "row" and recreated(row, expected):
        new_private_file(folder / ".goal-restored", restored_record(row, expected))
        path.unlink(missing_ok=True)
        return True
    return False


def arm_record(payload, root, terminal, info, goal, origin, used, prompt=None):
    """Write the per-turn handoff record (once), or return None if this turn is already armed."""
    session, turn = ids(payload)
    transcript = payload["transcript_path"]
    folder, state = root / session, store_for(root, session, turn)
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.exists():
        return None  # this turn already had a handoff, even a closed one
    latch = goal and goal["mode"] == "transfer"
    if latch and not new_private_file(folder / ".goal-requested", turn + "\n"):
        return None
    record = {
        "session": session, "turn": turn, "cwd": payload["cwd"], "terminal": terminal,
        "pid": info["foreground"]["pid"], "transcript": transcript, "nonce": secrets.token_hex(12),
        "origin": origin, "offset": os.path.getsize(transcript), "usage": used,
    }
    if prompt is not None:
        record["prompt"] = prompt
    if goal:
        record["goal"] = goal
    if not new_private_file(state, json.dumps(record)):
        if latch:
            (folder / ".goal-requested").unlink(missing_ok=True)  # ours: never leave a latch without a record
        return None
    set_stage(state, "armed", origin=origin)
    return record


def mid_turn_request(record, lead):
    goal = record.get("goal")
    return f"Codex Context Handoff: {lead}\n" + handoff_request(record["nonce"]) + goal_request(goal, record["nonce"])


def checkpoint_text(checkpoint):
    return (
        f"You may make at most {checkpoint} more tool call(s), only to reach a safe checkpoint (finish a "
        "half-applied edit, note running workers); start no new work. " if checkpoint else "Start no new work. "
    )


def gate_lead(checkpoint, transfer=True):
    return (
        "this tool call was not run. " + (
            "The context threshold was crossed while a Goal is active, so the Goal moves to a fresh chat now. "
            if transfer else "The context threshold was crossed, so this chat continues in a fresh chat now. "
        ) + checkpoint_text(checkpoint) + "Other tool calls are denied until this turn ends."
    )


def manual_lead(record):
    if goal_moves(record):
        return (
            "the user asked to hand this chat off now. Until this turn ends, tool calls other than the Goal steps "
            "below are limited to reaching a safe checkpoint."
        )
    return "the user asked to hand this chat off now. " + checkpoint_text(checkpoint_calls()) + (
        "Other tool calls are denied until this turn ends."
    )


def goal_moves(record):
    return bool(record.get("goal")) and record["goal"]["mode"] == "transfer"


def tool_name(payload):
    name = payload.get("tool_name")
    if not isinstance(name, str):
        return ""
    return name[len("functions."):] if name.startswith("functions.") else name


def tool_args(payload):
    value = payload.get("tool_input")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def deny(text):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": text,
    }}


def new_gate(state, record):
    """Start holding a mid-turn armed turn: `checkpoint` tool calls, and for a handoff without a Goal
    transfer also `scripts` code-mode `exec` wrappers (their nested calls take the checkpoint calls)."""
    budget = checkpoint_calls()
    gate = {"checkpoint": budget, "denied": 0, "updated": None}
    if not goal_moves(record):
        gate["scripts"] = budget
    replace_private(state.with_suffix(".gate"), json.dumps(gate))
    return budget


def gate_hook(payload, root, terminal):
    """PreToolUse: arm a handoff at a tool boundary and hold the turn to it until the turn ends.

    A chat with an active Goal arms a Goal transfer (consent needed); a chat with no Goal arms an
    ordinary handoff (no consent, no Goal tool). A chat whose Goal is not active is left to Stop.
    """
    session, turn = ids(payload)
    transcript = payload.get("transcript_path")
    if not session or not transcript or not payload.get("cwd") or disabled(root, session, turn):
        return {}
    folder, state = root / session, store_for(root, session, turn)
    if state.is_file():
        # One record per turn: once it is released or done, this turn is never armed again.
        record = json.loads(state.read_text(encoding="utf-8"))
        if record.get("origin") not in {"tool", "manual"} or any(
            state.with_suffix(suffix).exists() for suffix in (".md", ".released", *DONE)
        ):
            return {}
        gate = state.with_suffix(".gate")
        if not goal_moves(record) and not gate.is_file():
            return {}  # armed by an older version that did not hold ordinary handoffs
        with open_lock(state.with_suffix(".gate-lock")):
            return gate_decision(payload, state, record)
    name = tool_name(payload)
    if name in GOAL_TOOLS or (folder / ".off").exists():
        return {}
    latest = recent_turn(transcript)
    limit = threshold()
    if limit is None or not latest or latest["id"] != turn or not headroom(folder, transcript, latest["usage"], limit):
        return {}
    used = usage_percent(latest["usage"])
    notice = settle_orphans(folder, turn)
    shown = {"systemMessage": notice} if notice else {}
    if pending(folder) or not confirm_restore(folder, transcript, session) or failed_recently(folder, used):
        return shown
    status, row = goal_state(transcript, session, latest.get("trigger"))
    if status == "none":
        if retry_blocked(folder / PLAIN_RETRY, used):
            return shown
        goal = None
    elif status != "row" or row["status"] != "active":
        return shown  # unknown state is reported by Stop once; a Goal that is not active is left to Stop
    else:
        consent = goal_consent(root)
        if not consent or transfer_blocked(folder, used) or (
            row["token_budget"] is not None and row["token_budget"] - row["tokens_used"] <= 0
        ):
            return shown  # the Stop hook reports a missing consent and exhausted budgets once
        goal = {"mode": "transfer", "since": consent["since"], "arm": row}
    info, _ = check_chat(payload, terminal)
    if not info or (goal and not goal_consent(root)):
        return shown
    record = arm_record(payload, root, terminal, info, goal, "tool", used)
    if not record:
        return shown
    budget = new_gate(state, record)
    return dict(deny(mid_turn_request(record, gate_lead(budget, goal_moves(record)))), **shown)


class open_lock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.stream = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600), "w")
        fcntl.flock(self.stream, fcntl.LOCK_EX)
        return self.stream

    def __exit__(self, *_):
        self.stream.close()


def gate_reminder(record, locked, why):
    nonce = record["nonce"]
    if locked:
        return (
            f"Codex Context Handoff: {why} The Goal is paused for the transfer; make no more tool calls. End the "
            f"turn now: start your final answer with HANDOFF READY {nonce} GOAL transferred, then the handoff note."
        )
    return (
        f"Codex Context Handoff: {why} Call get_goal and update_goal with status \"paused\" (print both raw "
        f"results), then end the turn: start your final answer with HANDOFF READY {nonce} GOAL transferred, then "
        "the handoff note. Do not create or edit any other Goal."
    )


def gate_decision(payload, state, record):
    """Allow the native Goal steps, a bounded checkpoint, and nothing else, while a transfer is armed.

    Codex may not run PreToolUse for its native Goal tools, so the pause is read from the Goal DB on
    every call: once the armed Goal is no longer active, nothing but get_goal runs until the turn ends.
    """
    gate = read_json(state.with_suffix(".gate")) or {"checkpoint": 0, "denied": 0, "updated": None}
    rows = rows_after(record["transcript"], record["offset"])
    if rows is None or objected(rows, record, state):
        # The user (or a worker message) spoke after the arm: their request wins. Settle the attempt now,
        # since an interrupted turn may never reach Stop.
        return settle(state, record, "user input arrived before the Goal was paused" if goal_moves(record)
                      else "user input arrived after the handoff was armed")
    if not goal_moves(record):
        return plain_gate_decision(payload, state, record, gate)
    name = tool_name(payload)
    status, row = goal_state(record["transcript"], record["session"])
    unreadable = status == "unknown"
    locked = bool(gate.get("updated")) or (status == "row" and (
        row["goal_id"] != record["goal"]["arm"]["goal_id"] or row["status"] != "active"
    )) or status == "none"
    if not locked and not unreadable and compacted(record):
        # Auto-compaction already made room: stop holding the turn and try again later if needed.
        new_private_file(state.with_suffix(".released"), "context was compacted below the threshold\n")
        put_off(state, record, "the context was compacted below the threshold")
        return {}
    allowed, why = False, "this tool call was denied while the Goal transfer is armed."
    if name == "get_goal" or (name == CODE_MODE and not locked):
        allowed = True  # after the pause even a script is not needed: only the final answer is
    elif unreadable:
        why = f"the Goal state could not be read ({row}), so no other work may run during the transfer."
    elif name == "update_goal":
        if not locked and tool_args(payload).get("status") == "paused":
            allowed, gate["updated"] = True, "paused"
        else:
            why = "only one update_goal, with status paused, is allowed for the Goal transfer."
    elif name == "create_goal":
        why = "create_goal is not allowed in this chat during a Goal transfer."
    elif not locked and gate.get("checkpoint", 0) > 0:
        allowed, gate["checkpoint"] = True, gate["checkpoint"] - 1
    elif not locked:
        why = "the safe-checkpoint allowance is used up."
    if allowed:
        replace_private(state.with_suffix(".gate"), json.dumps(gate))
        return {}
    gate["denied"] = gate.get("denied", 0) + 1
    replace_private(state.with_suffix(".gate"), json.dumps(gate))
    if gate["denied"] > MAX_DENIALS:
        return give_up(state, record, gate)
    result = deny(gate_reminder(record, locked and not unreadable, why))
    if unreadable and gate["denied"] == 1:
        result["systemMessage"] = f"Context handoff: the Goal state could not be read during the transfer ({row})."
    return result


def compacted(record):
    """Whether a turn armed by the gate itself is back under the threshold (auto-compaction made room)."""
    if record.get("origin") != "tool":
        return False  # an explicit request stands whatever the context use
    latest = recent_turn(record["transcript"])
    limit = threshold()
    return bool(latest) and latest["id"] == record["turn"] and (
        limit is None or not exceeds_threshold(latest["usage"], limit)
    )


def plain_reminder(record, why):
    return (
        f"Codex Context Handoff: {why} End the turn now: start your final answer with HANDOFF READY "
        f"{record['nonce']}, then the handoff note. If everything is complete, or the remaining work needs the "
        "user or an approval, respond normally without the marker." + (
            " Do not change the Goal in this chat." if record.get("goal") else ""
        )
    )


def plain_gate_decision(payload, state, record, gate):
    """Hold a handoff without a Goal transfer to a bounded checkpoint until the turn ends.

    Every tool call other than code mode's `exec` takes one checkpoint call, including each call nested
    in a script. Each `exec` wrapper takes one script, and runs only while a checkpoint call is left, so
    a script with no nested call is bounded too. No Goal tool runs here (the handoff needs none), and a
    Goal that appears after an arm without one (or a Goal state that can no longer be read) releases the
    handoff at once, so no Goal is left behind or touched.
    """
    why = goal_changed(record)
    if why:
        return release(state, record, why)
    if compacted(record):
        return release(state, record, "the context was compacted below the threshold")
    name = tool_name(payload)
    allowed, why = False, "this tool call was denied while the handoff is armed."
    if name in GOAL_TOOLS:
        why = f"{name} is not part of this handoff; no Goal tool may run while this chat hands off."
    elif gate.get("checkpoint", 0) <= 0:
        why = "the safe-checkpoint allowance is used up."
    elif name == CODE_MODE:
        if gate.get("scripts", 0) > 0:
            allowed, gate["scripts"] = True, gate["scripts"] - 1
        else:
            why = "no more scripts may run while this chat hands off."
    else:
        allowed, gate["checkpoint"] = True, gate["checkpoint"] - 1
    if not allowed:
        gate["denied"] = gate.get("denied", 0) + 1
    replace_private(state.with_suffix(".gate"), json.dumps(gate))
    if allowed:
        return {}
    if gate["denied"] > MAX_DENIALS:
        return give_up(state, record, gate)
    return deny(plain_reminder(record, why))


def goal_changed(record):
    """Why a handoff without a Goal transfer may not go on because of the Goal DB, or None.

    Armed with no Goal: any Goal now (or an unreadable state) would be left behind by the switch.
    Armed leaving a Goal that was not active: that Goal must still be exactly as it was.
    """
    if goal_moves(record):
        return None
    status, row = goal_state(record["transcript"], record["session"])
    if status == "unknown":
        return f"the Goal state could not be read ({row})"
    goal = record.get("goal")
    if not goal:
        return f"a Goal ({row['status']}) appeared in this chat after the handoff was armed" if status == "row" else None
    arm = goal["arm"]
    if status != "row" or any(row[key] != arm.get(key) for key in (
        "goal_id", "status", "created_at_ms", "updated_at_ms", "objective_sha256",
    )):
        return "the Goal in this chat changed after the handoff was armed"
    return None


def release(state, record, why):
    """Stop holding the turn now and put the attempt off; the Goal, if any, is never touched."""
    new_private_file(state.with_suffix(".released"), why + "\n")
    result = put_off(state, record, why)
    return {"systemMessage": result["systemMessage"]} if result.get("systemMessage") else {}


def settle(state, record, why):
    """End an armed handoff that will not complete in this turn, at once: put off while no Goal was
    paused for it, otherwise fail closed (the Goal stays paused). Returns the hook output."""
    new_private_file(state.with_suffix(".released"), why + "\n")
    if not goal_moves(record) or untouched(state, record):
        result = put_off(state, record, why)
    else:
        result = goal_failure(state, record, why)
    return {"systemMessage": result.get("systemMessage", "")} if result.get("systemMessage") else {}


def settle_orphans(folder, turn):
    """A handoff armed in an earlier turn that ended without its note (an interrupt, say) is over: a
    Goal transfer is settled (put off while the Goal is untouched, else failed closed), any other
    handoff is closed. Without this it would stay pending and block every later handoff in the chat.
    Returns the messages for the user, joined."""
    messages = []
    for state in pending(folder):
        if state.name == turn or state.with_suffix(".md").exists():
            continue
        try:
            record = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            new_private_file(state.with_suffix(".closed"), "unreadable handoff record\n")
            continue
        if goal_moves(record) or record.get("origin") in {"tool", "manual"}:
            # A held turn that ended without its note (an interrupt) counts as a put-off attempt.
            messages.append(settle(state, record, "the turn ended without a handoff note").get("systemMessage"))
        else:
            new_private_file(state.with_suffix(".closed"), "the turn ended without a handoff note\n")
    return " ".join(filter(None, messages))


def give_up(state, record, gate):
    """The model kept working through MAX_DENIALS denials: stop holding the turn, visibly."""
    new_private_file(state.with_suffix(".released"), f"gave up after {MAX_DENIALS} denied tool calls\n")
    why = f"the model kept calling tools after {MAX_DENIALS} denials"
    if not goal_moves(record):
        return {"systemMessage": put_off(state, record, why)["systemMessage"]}
    status, row = goal_state(record["transcript"], record["session"])
    if status != "row" or row["status"] != "active" or row["goal_id"] != record["goal"]["arm"]["goal_id"]:
        result = goal_failure(state, record, why)
    else:
        result = put_off(state, record, why)
    return {"systemMessage": result.get("systemMessage", "")}


def plain_put_off(state, record, why):
    """End a handoff attempt without a Goal transfer; the gate may arm a later turn after more context use."""
    folder = state.parent
    count = ((read_json(folder / PLAIN_RETRY) or {}).get("count") or 0) + 1
    new_private_file(state.with_suffix(".closed"), f"handoff put off: {why}\n")
    replace_private(folder / PLAIN_RETRY, json.dumps({"count": count, "usage": record.get("usage") or 0}))
    if count >= MAX_ATTEMPTS:
        return {"systemMessage": (
            f"Context handoff: the handoff was put off ({why}); work continues here. Tool calls are no longer held "
            f"for a handoff in this chat after {count} attempts; a later turn past the threshold still asks for "
            "one when it ends."
        )}
    return {"systemMessage": (
        f"Context handoff: the handoff was put off ({why}); work continues here. A later turn asks again when it "
        f"ends, or at a tool call after {RETRY_GROWTH:g} more percentage points of context use."
    )}


def put_off(state, record, why):
    """End this transfer attempt with the Goal still active and untouched; a later one may try again."""
    if not goal_moves(record):
        return plain_put_off(state, record, why)
    folder = state.parent
    count = ((read_json(folder / ".goal-retry") or {}).get("count") or 0) + 1
    new_private_file(state.with_suffix(".closed"), f"goal transfer put off: {why}\n")
    replace_private(folder / ".goal-retry", json.dumps({"count": count, "usage": record.get("usage") or 0}))
    (folder / ".goal-requested").unlink(missing_ok=True)
    if count >= MAX_ATTEMPTS:
        new_private_file(folder / ".goal-wait", f"goal transfer put off {count} times\n")
        return {"systemMessage": (
            f"Context handoff: the Goal transfer was put off ({why}) and will not be tried again in this chat "
            f"after {count} attempts. The Goal keeps running here; Codex auto-compaction will handle context."
        )}
    return {"systemMessage": (
        f"Context handoff: the Goal transfer was put off ({why}). The Goal keeps running here; the transfer "
        f"will be tried again after {RETRY_GROWTH:g} more percentage points of context use."
    )}


def user_spoke(record, state):
    """Whether a prompt, steer, or a Herdr message that counts as input arrived after the arm (the arming
    prompt excepted; see `input_counter` for worker messages)."""
    rows = armed_rows(record)
    return rows is not None and objected(rows, record, state)


def user_put_off(state, record):
    """The user spoke after the arm and the Goal was never touched: put the attempt off, not fail it."""
    return user_spoke(record, state) and untouched(state, record)


def untouched(state, record):
    """Whether the armed Goal is still active and no Goal tool ran since the arm."""
    rows = armed_rows(record)
    status, row = goal_state(record["transcript"], record["session"])
    arm = record["goal"]["arm"]
    if status != "row" or row["status"] != "active" or row["goal_id"] != arm["goal_id"]:
        return False
    if rows is None:
        # Our request never reached the transcript (interrupted first), so the model never saw it.
        return row["updated_at_ms"] == arm["updated_at_ms"]
    return goal_calls(rows) == (0, 0)


def stop_request_again(record, state):
    gate = read_json(state.with_suffix(".gate")) or {}
    nonce = record["nonce"]
    status, row = goal_state(record["transcript"], record["session"])
    if goal_moves(record) and (gate.get("updated") == "paused" or (
        status == "row" and row["status"] == "paused" and row["goal_id"] == record["goal"]["arm"]["goal_id"]
    )):
        return (
            "Codex Context Handoff: the Goal was paused for the transfer. Do not call any Goal tool again. Start "
            f"your final answer with this exact plain-text line:\nHANDOFF READY {nonce} GOAL transferred\nThen write "
            "a short Markdown handoff with the goal, user constraints, decisions, worktree and branch, changes, "
            "validation, next action, and the IDs and state of any live Herdr workers."
        )
    return handoff_request(nonce) + goal_request(record.get("goal"), nonce)


def arm_now(payload, root, terminal, prompt=None):
    """(record, None) after arming an explicit handoff for this turn, else (None, why not)."""
    session, turn = ids(payload)
    if not session or not payload.get("transcript_path") or not payload.get("cwd"):
        return None, "could not identify this chat and turn"
    folder, transcript = root / session, payload["transcript_path"]
    if (folder / ".off").exists():
        return None, "handoff is off for this chat (submit `handoff on` first)"
    info, why = check_chat(payload, terminal)
    if not info:
        return None, why
    settle_orphans(folder, turn)
    if pending(folder):
        return None, "a handoff is already pending in this chat"
    (folder / ".switch-failed").unlink(missing_ok=True)  # an explicit request is a new attempt
    status, row = goal_state(transcript, session)
    goal = None
    if status == "unknown":
        return None, f"the Goal state could not be read ({row})"
    if status == "row" and row["status"] != "active":
        goal = {"mode": "left", "arm": row}  # paused before this request: never recreated
    elif status == "row":
        consent = goal_consent(root)
        if not consent:
            return None, "this chat has an active Goal and Goal transfer is off (submit `handoff goal transfer on`)"
        if row["token_budget"] is not None and row["token_budget"] - row["tokens_used"] <= 0:
            return None, "the active Goal's token budget is exhausted"
        (folder / ".goal-requested").unlink(missing_ok=True)  # an explicit request is a new attempt
        goal = {"mode": "transfer", "since": consent["since"], "arm": row}
    record = arm_record(payload, root, terminal, info, goal, "manual", usage_percent(last_usage(transcript)), prompt)
    if not record:
        return None, "this turn already has a handoff"
    new_gate(store_for(root, session, turn), record)  # the turn is held to the request until it ends
    return record, None


def manual_hook(payload, root, terminal):
    """`handoff now` typed as the whole prompt: arm this turn and give the model the request with it."""
    text = " ".join((payload.get("prompt") or "").split())
    if text.lower() not in NOW:
        return None
    record, why = arm_now(payload, root, terminal, prompt=text)
    if not record:
        return {"decision": "block", "reason": f"Codex Context Handoff: no handoff was started: {why}."}
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": mid_turn_request(
        record, manual_lead(record),
    )}}


def find_transcript(session):
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    found = sorted((home / "sessions").glob(f"*/*/*/rollout-*-{session}.jsonl"))
    return found[-1] if found else None


def user_asked(transcript, turn):
    """Whether a user message (not this plugin's continuation, not a Herdr message) arrived in the given turn."""
    tail = TRANSCRIPT_TAIL
    while True:
        started = asked = False
        for row_type, item in transcript_rows(transcript, tail, (b'"event_msg"',)):
            kind = item.get("type")
            if kind == "task_started":
                started, asked = item.get("turn_id") == turn, False
            elif started and kind == "item_completed" and isinstance(item.get("item"), dict) and (
                item["item"].get("type") == "UserMessage"
            ):
                text = message_text(item["item"].get("content")).strip()
                # A Herdr message (worker, parent or note) is never the user's own request to transfer.
                asked = asked or (bool(text) and not text.startswith(("Codex Context Handoff:", "<hc")))
        if started or tail >= os.path.getsize(transcript):
            return started and asked
        tail *= 4


def request_transfer(root):
    """`codex context-handoff request-transfer`: the model arms the transfer the user asked for in words.

    Run from a tool call in the chat itself, before the Goal is paused. The same guards as the hooks
    apply, plus a user message in the running turn; plugin consent is still required for an active Goal.
    """
    root = Path(root).resolve()
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or ""
    terminal = os.environ.get("HC_TERMINAL_ID", "")

    def refuse(why):
        print(f"Codex Context Handoff: the transfer was not armed: {why}.", file=sys.stderr)
        return 2

    if not ID.fullmatch(session):
        return refuse("CODEX_THREAD_ID is missing; run this from a tool call in the Codex chat")
    transcript = find_transcript(session)
    if not transcript:
        return refuse(f"no transcript was found for chat {session}")
    with transcript.open("rb") as stream:
        meta = json.loads(stream.readline(1024 * 1024)).get("payload") or {}
    latest = recent_turn(transcript)
    if not latest or latest["complete"]:
        return refuse("no turn is running in this chat")
    payload = {"session_id": session, "turn_id": latest["id"], "cwd": meta.get("cwd"),
               "transcript_path": str(transcript)}
    if disabled(root, session, latest["id"]):
        return refuse("handoff is off for this chat or this turn")
    if not user_asked(transcript, latest["id"]):
        return refuse("no user message in this turn asked for it")
    info, why = check_chat(payload, terminal)
    if not info:
        return refuse(why)
    try:
        current = (root / session / ".hook-script").read_text(encoding="utf-8") == hook_script_record(
            info["foreground"]["pid"]
        )
    except OSError:
        current = False
    if not current:
        return refuse(
            "this chat's Codex process has not run this version of the handoff hooks. If it loaded an older plugin "
            "version, restart Codex through the launcher or continue in a new chat (a prompt does not upgrade it); "
            "otherwise submit any prompt in this chat first"
        )
    record, why = arm_now(payload, root, terminal)
    if not record:
        return refuse(why)
    print(mid_turn_request(record, "the transfer the user asked for is armed. Make each Goal call below in a new "
                           "tool call; this helper must have been the only call in its own tool call, or the transfer "
                           "fails closed. " + (
        "Until this turn ends, tool calls other than the Goal steps below are limited to reaching a safe "
        "checkpoint." if goal_moves(record) else checkpoint_text(checkpoint_calls())
        + "Other tool calls are denied until this turn ends."
    )))
    return 0


def stop_hook(payload, root, terminal):
    session, turn_id = ids(payload)
    transcript = payload.get("transcript_path")
    if not session or not transcript or not payload.get("cwd") or (root / session / ".off").exists():
        return {}
    # Before the .skip test: the turn that received a handoff is skipped, but it must create the Goal.
    restored = restore_check(payload, root, session)
    if restored is not None:
        return restored
    checklist = restoration_check(payload, root, session)
    if checklist is not None:
        return checklist
    if disabled(root, session, turn_id):
        return {}
    state = store_for(root, session, turn_id)
    if state.is_file():
        # Our request already ran this turn; capture its answer whatever else continued the turn.
        return capture(payload, state)
    # stop_hook_active alone may come from another plugin's Stop hook, so it is not a reason to skip.
    latest = recent_turn(transcript)
    limit = threshold()
    folder, goal = root / session, None
    if limit is None or not latest or latest["id"] != turn_id or not headroom(folder, transcript, latest["usage"], limit):
        return {}
    info, _ = check_chat(payload, terminal)
    if not info:
        return {}
    used = usage_percent(latest["usage"])
    notice = settle_orphans(folder, turn_id)
    if notice:
        return {"systemMessage": notice}  # the next Stop past the threshold arms again if it may
    if failed_recently(folder, used):
        return {}  # a switch failed here: wait for more context use (or `handoff now`)
    status, row = goal_state(transcript, session, latest.get("trigger"))
    if status == "unknown":
        return goal_wait(folder, f"could not read Goal state: {row}")  # never treated as no Goal
    if status == "row" and row["status"] != "active":
        goal = {"mode": "left", "arm": row}
    elif status == "row":
        if transfer_blocked(folder, used):
            return {}  # a failed transfer, one in progress, or one put off until more context is used
        consent = goal_consent(root)
        if not consent:
            # A separate marker: turning transfer on later lets this chat try once.
            return goal_wait(folder, (
                "this chat has an active Goal and Goal transfer is not enabled (handoff goal transfer on)"
            ), ".goal-consent-wait")
        if row["token_budget"] is not None and row["token_budget"] - row["tokens_used"] <= 0:
            return goal_wait(folder, "the active Goal's token budget is exhausted")
        goal = {"mode": "transfer", "since": consent["since"], "arm": row}
    record = arm_record(payload, root, terminal, info, goal, "stop", used)
    if not record:
        return {}
    return {"decision": "block", "reason": handoff_request(record["nonce"]) + goal_request(goal, record["nonce"])}


def read_ledger(state):
    """The handoff's attempt and stage ledger (`<state>.attempts.json`)."""
    data = read_json(state.with_suffix(".attempts.json")) or {}
    data.setdefault("budget", BUDGET)
    data.setdefault("attempts", [{"n": 1, "kind": "request", "at": utc_now()}])
    data.setdefault("phase", None)
    data.setdefault("stages", {})
    data.setdefault("clear", None)
    data.setdefault("sends", [])
    return data


def update_ledger(state, change):
    """Read, change and atomically replace the ledger under its own lock; returns `change`'s result."""
    with open_lock(state.with_suffix(".ledger-lock")):
        data = read_ledger(state)
        result = change(data)
        replace_private(state.with_suffix(".attempts.json"), json.dumps(data))
    return result


def set_stage(state, name, **info):
    """Record the first time a completion stage was reached (later calls keep the first record)."""
    update_ledger(state, lambda data: data["stages"].setdefault(name, {"at": utc_now(), **info}))


def set_phase(state, phase, **fields):
    def change(data):
        data["phase"] = phase
        data.update(fields)
    update_ledger(state, change)


def take_attempt(state, kind, reason, phase):
    """Start the next end-to-end attempt (a note re-prompt or a switch retry); None when all BUDGET are used."""
    def change(data):
        attempts = data["attempts"]
        if len(attempts) >= BUDGET:
            return None
        if attempts and "outcome" not in attempts[-1]:
            attempts[-1].update(outcome="retry", reason=reason, at_end=utc_now())
        attempts.append({"n": len(attempts) + 1, "kind": kind, "phase": phase, "after": reason, "at": utc_now()})
        return len(attempts)
    return update_ledger(state, change)


def finish_attempt(state, outcome, reason):
    def change(data):
        last = data["attempts"][-1]
        if "outcome" not in last:
            last.update(outcome=outcome, reason=reason, at_end=utc_now())
    update_ledger(state, change)


def ledger_summary(state):
    data = read_ledger(state)
    stages = ", ".join(data["stages"]) or "none"
    tried = len(data["attempts"])
    return f"attempt {tried} of {data['budget']}, phase {data['phase'] or 'none'}, stages reached: {stages}"


def validate_note(answer, nonce):
    """(note, None) for a valid handoff answer, else (None, why).

    `missing` means no handoff marker at all (at Stop, the model's way to decline a handoff it does not
    need); every other reason is a malformed attempt that one specific re-prompt may fix.
    """
    text = (answer or "").strip()
    base = f"HANDOFF READY {nonce}"
    marker, _, body = text.partition("\n")
    if marker.rstrip() in {base, f"{base} GOAL transferred", f"{base} GOAL left"}:
        if text.count("HANDOFF READY") > 1:
            return None, "the marker line appears more than once"
        body = body.strip()
        if not body:
            return None, "the handoff note after the marker line is empty"
        if len(body) > MAX_NOTE:
            return None, f"the handoff note is {len(body)} characters, more than {MAX_NOTE}"
        return body + "\n", None
    if base in text:
        return None, "the marker line is not the first line of the final answer"
    if re.search(r"HANDOFF READY [0-9a-f]{24}", text):
        return None, "the marker line has another handoff's nonce"
    if "HANDOFF READY" in text:
        return None, "the marker line is malformed"
    return None, "missing"


def note_retry(state, record, why):
    """Ask once more, with the same nonce, for a note that `why` rejected; None when no attempt is left
    or this reason was already asked about (a re-prompt never repeats itself)."""
    asked = [a.get("after") for a in read_ledger(state)["attempts"] if a.get("kind") == "note"]
    if why in asked or not take_attempt(state, "note", why, "note"):
        return None
    lead = (
        "the turn ended without the handoff note. " if why == "missing"
        else f"the handoff answer was not accepted: {why}. "
    )
    return {"decision": "block", "reason": f"Codex Context Handoff: {lead}" + stop_request_again(record, state)}


def capture(payload, state):
    if any(state.with_suffix(suffix).exists() for suffix in (".md", *DONE)):
        return {}
    record = json.loads(state.read_text(encoding="utf-8"))
    goal = record.get("goal")
    transfer = goal and goal["mode"] == "transfer"
    mid_turn = record.get("origin") in {"tool", "manual"}
    # The GOAL suffix is informative only; the transfer is proven from the rollout and the DB.
    note, invalid = validate_note(payload.get("last_assistant_message"), record["nonce"])
    if invalid:
        if transfer and user_put_off(state, record):
            return put_off(state, record, "user input arrived before the Goal was paused")
        if mid_turn and not transfer and user_spoke(record, state):
            # Never ask again over the user's (or a non-worker Herdr) later message.
            return put_off(state, record, "user input arrived after the handoff was armed")
        changed = goal_changed(record) if mid_turn else None
        if changed:
            return put_off(state, record, changed)  # never ask again for a handoff that may not switch
        declined = invalid == "missing" and not mid_turn  # a Stop request the model chose not to take
        if not declined and not user_spoke(record, state):
            again = note_retry(state, record, invalid)
            if again:
                return again
        why = "no handoff note" if invalid == "missing" else f"no valid handoff note ({invalid})"
        if transfer:
            return goal_failure(state, record, why)
        new_private_file(state.with_suffix(".closed"), why + "\n")
        finish_attempt(state, "closed", why)
        if declined:
            return {}
        return {"systemMessage": f"Context handoff: {why}, so nothing was handed off. Work continues here."}
    if mid_turn and not transfer and user_spoke(record, state):
        # A message after a mid-turn arm cancels it even when the model still wrote the note.
        return put_off(state, record, "user input arrived after the handoff was armed")
    if transfer:
        # Consent can be withdrawn while the turn runs (a blocked control or another terminal).
        moved, why = (None, "Goal transfer was turned off") if not goal_consent(state.parent.parent) else (
            verify_transfer(record, state)
        )
        if not moved:
            if user_put_off(state, record):
                return put_off(state, record, "user input arrived before the Goal was paused")
            return goal_failure(state, record, why)
        if not new_private_file(state.with_suffix(".goal.json"), json.dumps(moved, ensure_ascii=False)):
            return {}
        note += goal_note(moved)
    elif goal:
        why = verify_left(record)
        if why:
            new_private_file(state.with_suffix(".closed"), f"goal changed during the handoff: {why}\n")
            new_private_file(state.parent / ".goal-wait", f"goal changed during the handoff: {why}\n")
            return {"systemMessage": (
                f"Context handoff stopped: the Goal in this chat changed during the handoff turn ({why}), so "
                "nothing was handed off. Work continues here."
            )}
    else:
        # Armed with no Goal (at Stop or mid-turn, even a turn whose last step was only the final answer):
        # a Goal that appeared since would be left behind in this chat by the switch.
        why = goal_changed(record)
        if why:
            new_private_file(state.with_suffix(".closed"), f"goal check failed: {why}\n")
            return {"systemMessage": (
                f"Context handoff stopped: {why}, so nothing was handed off and no Goal was touched. Work "
                "continues here."
            )}
    secret = secrets.token_hex(16)
    if not new_private_file(state.with_suffix(".restore.json"), json.dumps({"secret": secret})):
        return {}
    if not new_private_file(state.with_suffix(".md"), note + restore_checklist(state, secret, transfer)):
        return {}
    set_stage(state, "note-valid", sha256=digest(note))
    if not guard_busy(state, record):
        arm_guard(state, record)  # the switch arms it again once it holds the terminal lock
    with os.fdopen(
        os.open(state.with_suffix(".log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--switch", str(state)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
        )
    return {"systemMessage": (
        f"Context handoff ready: the note is saved and a fresh chat starts in about {grace():g}s. Type anything "
        "or submit `handoff off` to cancel; submit `handoff status` if no fresh chat appears."
        + (" The Goal is paused here and will be recreated in the fresh chat; if the handoff is "
           "cancelled it stays paused (resume it with /goal)." if transfer else "")
    )}


def read_token(secret, revision):
    """The note's read token at a revision: each held worker message appended to the note makes a new one."""
    return hashlib.sha256(f"{secret}:{revision}".encode()).hexdigest()[:20]


def restore_checklist(state, secret, transfer):
    first = (
        "1. First, before anything else, call create_goal exactly as the Goal transfer section says.\n"
        if transfer else ""
    )
    step = 2 if transfer else 1
    return (
        "\n## Restore checklist (Codex Context Handoff)\n\n"
        + first
        + f"{step}. Read this whole note, including every \"Held worker message\" section the plugin adds "
        "below while the chat switches.\n"
        f"{step + 1}. Review the Herdr workers started from this terminal: run `codex context-handoff workers`, "
        "read each worker's result, then record a decision for every listed worker with `codex context-handoff "
        "workers decide <id> keep|reuse|close --reason \"...\"` (close only after its result is captured), and "
        "finish with `codex context-handoff workers done`.\n"
        f"{step + 2}. Confirm with `codex context-handoff restored --token <token>`, using the last Read token "
        "line in this note.\n"
        f"\nRead token (revision 0): {read_token(secret, 0)}\n"
    )


def tool_hook(payload, root, terminal):
    session, turn_id = ids(payload)
    transcript = payload.get("transcript_path")
    if not session or not transcript or disabled(root, session, turn_id):
        return {}
    state = store_for(root, session, turn_id)
    marker = state.with_suffix(".nudged")
    if state.exists() or marker.exists():
        return {}
    latest = recent_turn(transcript)
    limit = threshold()
    folder = root / session
    if limit is None or not latest or latest["id"] != turn_id or not headroom(folder, transcript, latest["usage"], limit):
        return {}
    if latest.get("trigger") == "goal":
        return {}  # Goal turns chain without a user; the PreToolUse gate arms their transfer instead
    text = nudge(limit)
    # The chat is checked before the once-per-turn marker, so a failed check does not use up the nudge.
    if not check_chat(payload, terminal)[0]:
        return {}
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not new_private_file(marker, ""):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def pending(folder):
    if not folder.is_dir():
        return []
    return sorted(
        path for path in folder.iterdir()
        if ID.fullmatch(path.name) and path.is_file()
        and not any(path.with_suffix(suffix).exists() for suffix in DONE)
    )


def control_hook(payload, root, terminal):
    command = CONTROLS.get(" ".join((payload.get("prompt") or "").split()).lower())
    if not command:
        return None
    session = payload.get("session_id") or ""
    if not ID.fullmatch(session):
        return {"decision": "block", "reason": "Codex Context Handoff: could not identify this chat."}
    folder = root / session
    flag = folder / ".off"
    lines = []
    if command == "off":
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        flag.touch(mode=0o600)
        cancelled = [s for s in pending(folder) if new_private_file(
            s.with_suffix(".cancelled"), "handoff off\n" + goal_paused_line(s)
        )]
        lines.append("Handoff is off for this chat." + (
            f" Cancelled {len(cancelled)} armed or pending handoff(s)." if cancelled else ""
        ))
        lines.extend(filter(None, map(goal_paused_line, cancelled)))
    elif command == "on":
        flag.unlink(missing_ok=True)
    elif command == "goal-on" and not consent_file(root):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        replace_private(root / CONSENT, json.dumps({
            "enabled": True, "since": utc_now(), "chat": session, "command": "handoff goal transfer on",
        }))
    elif command == "goal-off":
        (root / CONSENT).unlink(missing_ok=True)
    requested = "off" if flag.exists() else "on"
    lines.append(f"Requested for this chat: {requested}.")
    try:
        info, why = check_chat(payload, terminal)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        info, why = None, f"could not verify the Herdr terminal ({error})."
    if info:
        used = usage_percent(last_usage(payload["transcript_path"]))
        lines.append(f"Operational: yes (threshold {threshold():g}%" + (
            f", last request used {used:.1f}%)." if used is not None else ").")
        )
    else:
        lines.append(f"Operational: no; {why}")
    consent = consent_file(root)
    lines.append("Goal transfer: " + (f"on since {consent['since']}" if consent else "off") + (
        " (disabled by CODEX_CONTEXT_HANDOFF_GOAL)" if consent and goal_killed() else ""
    ) + ".")
    for state in pending(folder):
        stage = "switch pending" if state.with_suffix(".md").exists() else "armed, waiting for a note"
        lines.append(f"Pending handoff {state.name}: {stage} ({ledger_summary(state)}).")
    latest = latest_outcome(folder)
    if latest:
        lines.append(latest)
    received, _ = received_handoff(folder, root)
    if received is not None and not received.with_suffix(".restored").exists():
        lines.append("This chat received handoff " + received.name + "; restore not confirmed yet: "
                     + "; ".join(restoration_missing(received)) + ".")
    paused = latest_goal_transfer(folder)
    if paused:
        lines.append(paused)
    return {"decision": "block", "reason": "Codex Context Handoff: " + " ".join(lines)}


def latest_outcome(folder):
    """A status line for this chat's latest finished handoff that did not reach a fresh chat."""
    if not folder.is_dir():
        return ""
    finished = [p for p in folder.iterdir() if ID.fullmatch(p.name) and p.is_file()
                and (p.with_suffix(".closed").exists() or p.with_suffix(".cancelled").exists())
                and not p.with_suffix(".received").exists()]
    if not finished:
        return ""
    state = max(finished, key=lambda p: p.stat().st_mtime)
    marker = state.with_suffix(".cancelled") if state.with_suffix(".cancelled").exists() else state.with_suffix(".closed")
    why = marker.read_text(encoding="utf-8").strip().splitlines()[0] if marker.read_text(encoding="utf-8").strip() else "?"
    return (f"Last handoff {state.name} {marker.suffix[1:]}: {why} ({ledger_summary(state)}); note "
            f"{state.with_suffix('.md') if state.with_suffix('.md').exists() else 'not written'}.")


def latest_goal_transfer(folder):
    """A status line for this chat's latest Goal paused for a handoff that was not received."""
    found = []
    for path in folder.glob("*.goal.json") if folder.is_dir() else ():
        state = path.with_name(path.name[: -len(".goal.json")])
        if state.with_suffix(".received").exists():
            continue
        try:
            paused_at = json.loads(path.read_text(encoding="utf-8"))["paused_at_ms"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        stage = next((s[1:] for s in (".cancelled", ".closed") if state.with_suffix(s).exists()), "switch pending")
        found.append((paused_at, stage))
    if not found:
        return ""
    paused_at, stage = max(found)
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(paused_at // 1000)) if type(paused_at) is int else "?"
    return f"Goal paused for transfer at {when} ({stage}); resume it with /goal if the handoff did not continue."


def mark_closing(payload, root):
    session, turn = ids(payload)
    text = " ".join((payload.get("prompt") or "").split()).lower().rstrip(".!")
    if session and text in CLOSING:
        skip = store_for(root, session, turn).with_suffix(".skip")
        skip.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        new_private_file(skip, "")


def read_screen(terminal):
    result = hc("read", "--json", "--lines", "0", terminal)
    return result.get("text", "") if result.get("complete") else None


def footer_prompt(screen):
    """(prompt line, line beneath it) from the footer, or None when the footer has no prompt."""
    lines = screen.rstrip().splitlines()
    # The prompt must be in the footer, beneath any transcript or dialogs.
    for index in range(len(lines) - 1, max(len(lines) - 1 - WRAPPED_FOOTER, -1), -1):
        if not lines[index].startswith("›"):
            continue
        beneath = lines[index + 1:]
        # Within the usual footer, or the placeholder above a status that a narrow terminal wrapped.
        if len(beneath) < FOOTER or (lines[index].rstrip() == PLACEHOLDER and wrapped_status(beneath)):
            return lines[index], beneath[0] if beneath else ""
        return None
    return None


def wrapped_status(beneath):
    """Whether the lines beneath the placeholder are one blank line and then only status lines."""
    return (
        not beneath[0].strip() and all(line.strip() for line in beneath[1:])
        and not any(line.strip().startswith(DIALOG) or "esc to cancel" in line for line in beneath)
    )


def composer_text(screen):
    """The composer's text: "" when empty, None when the footer has no prompt."""
    found = footer_prompt(screen)
    if found is None:
        return None
    prompt, below = found
    if below.strip():  # a multi-line draft (or one starting with a newline) continues beneath
        return f"{prompt[1:].strip()}\n{below.strip()}".strip()
    return "" if COMPOSER.fullmatch(prompt) else prompt[1:].strip()


def read_composer(terminal):
    screen = read_screen(terminal)
    return screen if screen is not None and composer_text(screen) == "" else None


def active_status(info):
    status = info.get("status") or {}
    return status.get("state") if status.get("app") == "codex" else None


def log(message):
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def replace_private(path, content):
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.replace(temporary, path)


def guard_path(state, record):
    return state.parent.parent / f"{record['terminal']}.switch"


def arm_guard(state, record):
    """Point the terminal at this handoff: any other prompt submitted there now stops it."""
    replace_private(guard_path(state, record), f"{record['session']} {record['turn']} {record['nonce']}\n")


def guard_armed(state, record):
    try:
        return guard_path(state, record).read_text(encoding="utf-8").split() == [
            record["session"], record["turn"], record["nonce"],
        ]
    except OSError:
        return False


def guard_busy(state, record):
    """Whether the terminal's guard points at another handoff that is still pending."""
    try:
        session, turn, _ = guard_path(state, record).read_text(encoding="utf-8").split()
        other = store_for(state.parent.parent, session, turn)
    except (OSError, ValueError):
        return False
    return other != state and other.is_file() and not any(other.with_suffix(s).exists() for s in DONE)


def disarm_guard(state, record):
    if guard_armed(state, record):
        guard_path(state, record).unlink(missing_ok=True)


def note_prompt_hook(payload, root, terminal):
    """Record that UserPromptSubmit hooks run in this terminal's foreground Codex for this chat.

    That hook is what stops a switch when a prompt is submitted, so only with this evidence does
    the switch wait out a long startup after /clear. A resumed chat records its new process.
    """
    session = payload.get("session_id") or ""
    if not ID.fullmatch(session):
        return
    evidence = root / session / ".prompt-hook"
    try:
        pid = int(evidence.read_text(encoding="utf-8"))
        if not in_foreground_tree(pid):
            raise ValueError("another process")
    except (OSError, ValueError):
        info = terminal_info(terminal, payload.get("cwd"))
        if not info or not in_foreground_tree(info["foreground"]["pid"]):
            return
        pid = info["foreground"]["pid"]
        evidence.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        replace_private(evidence, f"{pid}\n")
    note_hook_script(root / session, pid)


def hook_script_record(pid):
    return f"{Path(__file__).resolve()}\n{pid}\n"


def note_hook_script(folder, pid):
    """Record, for this chat only, that this hook script runs in its foreground Codex `pid`.

    `codex context-handoff request-transfer` runs exactly that script, and only from that process, so
    a chat whose process still runs an older hook version (which never writes this file) is refused
    before its Goal is touched.
    """
    path = folder / ".hook-script"
    try:
        if path.read_text(encoding="utf-8") == hook_script_record(pid):
            return
    except OSError:
        pass
    replace_private(path, hook_script_record(pid))


def prompt_hook_evidence(state, record):
    try:
        return (state.parent / ".prompt-hook").read_text(encoding="utf-8").strip() == str(record["pid"])
    except OSError:
        return False


def stopped(state):
    return (state.parent / ".off").exists() or state.with_suffix(".cancelled").exists()


def continuation(record, state):
    # Fixed once the note is written: nothing in it changes when worker messages are held later, so the
    # prompt hook can compare it exactly.
    return (
        f"Codex Context Handoff: {record['session']} {record['turn']} {record['nonce']} "
        f"Read the local handoff note at {json.dumps(str(state.with_suffix('.md')))} and follow its restore "
        "checklist. Check the working tree and live Herdr workers before resuming the remaining work. Preserve "
        "the user's scope and decisions."
        + (" A Goal was transferred with this handoff: before anything else, call create_goal with exactly the "
           "objective and token_budget from the note's \"Goal transfer\" section and print its raw result."
           if state.with_suffix(".goal.json").is_file() else "")
    )


def herdr_turn(info):
    turn = info.get("turn") if info else None
    if type(turn) is not int:
        raise RuntimeError("Could not read Herdr's turn counter for the original Codex terminal.")
    return turn


class Retryable(RuntimeError):
    """A switch failure that a later safe idle boundary may recover: a technical problem, or a verified
    worker message that was held. `phase` is where the next attempt starts (pre-clear or post-clear)."""

    def __init__(self, message, phase):
        super().__init__(message)
        self.phase = phase


def held_entries(state):
    """The worker messages held for this handoff (`<state>.held.jsonl`), in order."""
    try:
        lines = state.with_suffix(".held.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def held_count(state):
    """Distinct held messages: the note's read token revision."""
    return len(held_entries(state))


def held_events(state):
    """Every held delivery, repeats included (`<state>.held-events.jsonl`): what the switch watches."""
    try:
        lines = state.with_suffix(".held-events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return [e for e in events if isinstance(e, dict)]


def held_seen(state):
    return len(held_events(state))


def may_be_held(problem):
    """Whether a fresh-chat problem could be the trace of a worker message the prompt hook is holding
    (a short busy period, a new prompt line, another Herdr turn); drafts, dialogs, cancellations and
    a missing guard are final at once."""
    return any(sign in problem for sign in (
        "ran something besides", "(old history)", "reported done", "The fresh chat changed",
    ))


def settle_change(state, record, held_base, phase, final):
    """The screen or Herdr turn changed: give the UserPromptSubmit hook a moment to record why.

    A worker message it held makes the change retryable; a cancellation (a person's prompt or `handoff
    off`) or no explanation at all is final.
    """
    deadline = time.monotonic() + INPUT_SETTLE
    while True:
        if stopped(state):
            raise RuntimeError("Handoff was cancelled (a prompt was submitted or the handoff was turned off).")
        if held_seen(state) > held_base:
            raise Retryable("A Herdr worker message arrived and was held for the fresh chat.", phase)
        if time.monotonic() >= deadline:
            raise RuntimeError(final)
        time.sleep(POLL)


def await_old_chat(record, state, start_turn, held_base, wait=12.0):
    """Return (screen, Herdr turn) once the finished turn's chat shows an empty, idle prompt."""
    deadline = time.monotonic() + wait
    previous = None
    while time.monotonic() < deadline:
        if stopped(state):
            raise RuntimeError("Handoff was cancelled before /clear.")
        turn = recent_turn(record["transcript"])
        info = terminal_info(record["terminal"], record["cwd"], record["pid"])
        if not info:
            raise RuntimeError("The terminal no longer runs the original Codex in this worktree.")
        if not turn or turn["id"] != record["turn"]:
            raise RuntimeError("A new turn started in the original chat before /clear.")
        status = active_status(info)
        if status in {"blocked", "error"}:
            raise RuntimeError(f"The original chat reported {status}; check the terminal.")
        # Only the turn that wrote the note may end here: one step past the switch's start.
        if not start_turn <= herdr_turn(info) <= start_turn + 1 or (
            turn["complete"] and status == "working" and previous is not None
        ):
            settle_change(state, record, held_base, "pre-clear", "The original chat ran something else before /clear.")
        if turn["complete"] and status in {"done", "idle"}:
            screen = read_screen(record["terminal"])
            draft = composer_text(screen) if screen is not None else None
            if draft and not draft.startswith("<hc"):
                raise RuntimeError("Text is in the original chat's composer; the handoff yields to it.")
            current = (screen if draft == "" else None, info["turn"])
            if current[0] is not None and current == previous:
                return current
            previous = current
        else:
            previous = None
        time.sleep(POLL)
    raise Retryable("The original chat did not reach an empty, idle Codex prompt in time.", "pre-clear")


def look(record):
    """(status, Herdr turn, status time, screen) of the recorded Codex; None once it changed hands."""
    info = terminal_info(record["terminal"], record["cwd"], record["pid"])
    if not info:
        return None
    return active_status(info), info.get("turn"), info.get("status_ms"), read_screen(record["terminal"])


def prompt_state(screen, old_screen, history_ok=False):
    """How the prompt looks. `history_ok`: the fresh chat already shows held worker messages (and the
    hook's notice), so more than one prompt line is expected; the empty placeholder is still required."""
    if screen is None:
        return "unreadable"
    if not screen.strip():
        return "blank"
    found = footer_prompt(screen)
    if found is None:
        return "no prompt in the footer"
    if composer_text(screen):
        return "text in the composer"
    if screen == old_screen:
        return "old screen"
    if sum(line.startswith("›") for line in screen.splitlines()) != 1 and not history_ok:
        return "old history"
    # A fresh chat shows its placeholder; a bare prompt may hide a draft starting with a newline.
    return "fresh" if found[0].rstrip() == PLACEHOLDER else "no placeholder"


def describe(view, old_screen, history_ok=False):
    if view is None:
        return "terminal changed hands"
    return f"status {view[0]}, Herdr turn {view[1]}, prompt {prompt_state(view[3], old_screen, history_ok)}"


def source_moved(record, cache):
    """Whether the old chat's transcript shows another turn (re-read only when the file changes)."""
    stat = os.stat(record["transcript"])
    signature = (stat.st_size, stat.st_mtime_ns)
    if cache.get("signature") != signature:
        latest = recent_turn(record["transcript"])
        cache.update(signature=signature, moved=not latest or latest["id"] != record["turn"])
    return cache["moved"]


def fresh_chat_problem(view, state, record, old_screen, cleared_turn, echo, cache, history_ok=False):
    """Why the switch must stop now, or None while the fresh chat may still be starting.

    Codex reports `working` while MCP servers start after /clear, with the fresh, empty prompt
    on screen. That one busy period is allowed; a prompt, a draft, a dialog, a finished turn or
    a second busy period is not. `echo` covers the moment before Codex redraws after /clear.
    """
    if stopped(state):
        return "Handoff was cancelled (a prompt was submitted or the handoff was turned off)"
    if not guard_armed(state, record):
        return "The takeover guard for this terminal is missing"
    if consent_revoked(state):
        return consent_revoked(state)
    changed = goal_changed(record)
    if changed:
        return f"Stopped: {changed}"
    if source_moved(record, cache):
        return "The old chat started another turn"
    if view is None:
        return "The terminal no longer runs the original Codex in this worktree"
    status, turn, since, screen = view
    if type(turn) is not int or type(since) is not int:
        return "Herdr did not report the turn counter and status time"
    # Herdr's turn goes up each time the status leaves working: startup may end once.
    if not cleared_turn <= turn <= cleared_turn + 1 or (status in {"working", "done"} and turn != cleared_turn):
        return "The fresh chat ran something besides its startup"
    if status in {"blocked", "error"}:
        return f"The fresh chat reported {status}"
    shown = prompt_state(screen, old_screen, history_ok)
    if echo:
        # Until Codex redraws, only the old chat, the typed /clear itself, or the blank frame
        # /clear draws before the fresh chat may show.
        draft = composer_text(screen) if screen is not None else None
        if shown in {"old screen", "blank"} or (draft and "/clear".startswith(draft)):
            return None
    elif status not in {"working", "idle"}:
        return f"The fresh chat reported {status}"
    elif shown == "fresh":
        return None
    if shown == "text in the composer":
        draft = composer_text(screen) or ""
        if draft.startswith("<hc"):
            return None  # Herdr is pasting a worker message; its hook decides
        return "Text appeared in the composer"
    return f"No fresh, empty prompt after /clear ({shown})"


def await_fresh_chat(record, state, old_screen, cleared_turn, cache, rebased=False, held_base=0, history_ok=False):
    """Return (turn, status time, screen) once the fresh chat has stayed idle at an empty prompt.

    `rebased`: a later attempt in the same fresh chat (after a held worker message or a continuation
    that provably never arrived); the Herdr turn was re-read and there is no redraw echo any more.
    """
    start = time.monotonic()
    evidence = prompt_hook_evidence(state, record)
    # Without proof that the takeover hook runs in this process, a long busy startup is not trusted.
    limit = startup_wait() if evidence else 12.0
    log(f"waiting up to {limit:g}s for the fresh chat (prompt hook evidence: {'yes' if evidence else 'no'}"
        + (", same fresh chat as the previous attempt" if rebased else "") + ")")
    settled = shown = None
    echo = not rebased  # until Codex has redrawn: closed after CLEAR_ECHO or at the first fresh prompt
    while True:
        now = time.monotonic()
        elapsed = now - start
        view = look(record)
        summary = describe(view, old_screen, history_ok)
        if summary != shown:
            log(f"+{elapsed:.1f}s after /clear: {summary}")
            shown = summary
        fresh = view is not None and prompt_state(view[3], old_screen, history_ok) == "fresh"
        echo = echo and elapsed < CLEAR_ECHO and not fresh
        problem = fresh_chat_problem(view, state, record, old_screen, cleared_turn, echo, cache, history_ok)
        if problem:
            message = f"{problem} {elapsed:.1f}s after /clear ({summary})."
            if not may_be_held(problem):
                raise RuntimeError(message)
            settle_change(state, record, held_base, "post-clear", message)
        if view[0] == "idle" and fresh:
            if settled is None or settled[0] != view[1:]:
                settled = (view[1:], now)
            elif now - settled[1] >= SETTLE:
                log(f"+{elapsed:.1f}s after /clear: fresh chat settled at an empty, idle prompt")
                return view[1:]
        else:
            settled = None
        if elapsed >= limit:
            message = f"The fresh chat did not settle at an idle prompt within {limit:g}s after /clear ({summary})."
            if view[0] in {"working", "idle"} and view[3] is not None and composer_text(view[3]) == "":
                raise Retryable(message, "post-clear")
            raise RuntimeError(message)
        time.sleep(POLL)


def worker_status(terminal):
    result = hc("ls")
    entries = result.get("terminals", []) if isinstance(result, dict) else result
    workers = []
    for entry in entries:
        address = entry.get("address", "")
        if entry.get("parent") != terminal or entry.get("state") != "live" or not TERMINAL.fullmatch(address):
            continue
        status = entry.get("status")
        if isinstance(status, dict):
            status = status.get("state")
        workers.append(f"- {address} ({status or 'live'})")
    return "\n".join(workers)


def descendants(terminals, root):
    """{address: lineage} for every strict descendant of `root` in a Herdr snapshot."""
    found = {}
    for address in terminals:
        if address == root or not TERMINAL.fullmatch(address):
            continue
        chain = lineage(terminals, address, root)
        if chain:
            found[address] = chain
    return found


def status_word(entry):
    status = entry.get("status")
    return status.get("state") if isinstance(status, dict) else status if isinstance(status, str) else None


def session_started_ms(transcript):
    try:
        with Path(transcript).open("rb") as stream:
            row = json.loads(stream.readline(1024 * 1024))
        stamp = row.get("timestamp") or (row.get("payload") or {}).get("timestamp")
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")) * 1000
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def worker_entry(entry, chain):
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    foreground = entry.get("foreground") if isinstance(entry.get("foreground"), dict) else {}
    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
    return {
        "address": entry["address"], "parent": entry.get("parent"), "lineage": chain, "depth": len(chain),
        "name": entry.get("name"), "template": entry.get("template"), "labels": labels, "cwd": entry.get("cwd"),
        "foreground": foreground.get("name"), "foreground_pid": foreground.get("pid"),
        "state": entry.get("state"), "status": status_word(entry),
        "status_msg": status.get("msg"), "turn": entry.get("turn"), "pid": entry.get("pid"),
        "created_ms": entry.get("created_ms"), "finished_ms": entry.get("finished_ms"),
        "exit_code": entry.get("exit_code"), "reason": entry.get("reason"),
        "result_location": next((labels[k] for k in ("result", "report", "output") if labels.get(k)), None),
    }


def capture_manifest(record, state, into_note):
    """Save the terminal's owned workers at the handoff: every live strict descendant, and the
    descendants that finished during the source chat (newest 20). Other terminals never appear."""
    terminals = hc_list(timeout=8)
    since = session_started_ms(record["transcript"])
    live, finished = [], []
    for address, chain in sorted(descendants(terminals, record["terminal"]).items()):
        entry = terminals[address]
        if entry.get("state") == "live":
            live.append(worker_entry(entry, chain))
        elif type(entry.get("finished_ms")) is int and entry["finished_ms"] >= since:
            finished.append(worker_entry(entry, chain))
    finished = sorted(finished, key=lambda w: w["finished_ms"], reverse=True)[:20]
    workers = live + finished
    replace_private(state.with_suffix(".workers.json"), json.dumps({
        "version": 1, "terminal": record["terminal"], "captured_at": utc_now(), "workers": workers,
    }))
    if not into_note:
        return workers
    lines = [f"\n## Herdr workers of {record['terminal']} at handoff\n"]
    for worker in workers:
        lines.append(
            f"- {worker['address']}" + (f" {worker['name']}" if worker["name"] else "")
            + f": {worker['state']}/{worker['status'] or 'unknown'}, parent {worker['parent']}"
            + (f", result {worker['result_location']}" if worker["result_location"] else "")
        )
    if not workers:
        lines.append("- none")
    lines.append(f"\nWorker manifest: {state.with_suffix('.workers.json')}\n")
    with open_lock(state.with_suffix(".note-lock")):
        with state.with_suffix(".md").open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
    return workers


def hold_for_cancel(record, state, screen, seconds, turn, held_base=0):
    """Keep the idle, unchanged prompt and Herdr turn for the grace window; a change is final unless a
    held worker message explains it (then the next attempt waits for the next idle boundary)."""
    deadline = time.monotonic() + seconds
    while True:
        if stopped(state):
            raise RuntimeError("Handoff was cancelled before /clear.")
        if consent_revoked(state):
            raise RuntimeError(f"{consent_revoked(state)} before /clear.")
        changed = goal_changed(record)
        if changed:
            raise RuntimeError(f"Stopped before /clear: {changed}.")
        if held_seen(state) > held_base:
            raise Retryable("A Herdr worker message arrived during the cancellation window and was held.", "pre-clear")
        info = terminal_info(record["terminal"], record["cwd"], record["pid"])
        if (
            not info or active_status(info) not in {"done", "idle"} or info.get("turn") != turn
            or read_composer(record["terminal"]) != screen
        ):
            settle_change(state, record, held_base, "pre-clear",
                          "The terminal changed before /clear; the handoff note is left for manual recovery.")
        if time.monotonic() >= deadline:
            return
        time.sleep(POLL)


def reconcile_clear(record, old_screen, turn, error):
    """`hc keys /clear` failed: was it typed? Polls through Codex's redraw; only a provable answer continues."""
    deadline = time.monotonic() + CLEAR_ECHO + 2.0
    unchanged_since = None
    while True:
        try:
            view = look(record)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            view = None
        if view is None:
            raise RuntimeError(f"Could not tell whether /clear was typed ({error}); inspect the terminal.")
        status, now_turn, _, screen = view
        if prompt_state(screen, old_screen) == "fresh":
            log(f"/clear reconciled: a fresh chat is on screen after {error}")
            return {"turn": turn, "status": "idle", "reconciled": True}
        if screen == old_screen and now_turn == turn and status in {"done", "idle"}:
            unchanged_since = unchanged_since or time.monotonic()
            if time.monotonic() - unchanged_since >= 1.0:
                raise Retryable(f"/clear was not typed ({error}).", "pre-clear")
        else:
            unchanged_since = None
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Could not tell whether /clear was typed ({error}); inspect the terminal.")
        time.sleep(POLL)


def sessions_dir(transcript):
    path = Path(transcript)
    parents = path.parents
    if path.name.startswith("rollout-") and len(parents) > 3 and parents[3].name == "sessions":
        return parents[3]
    return None


def continuation_rollouts(record, since):
    """Rollouts of other chats in the same worktree, written since `since`, whose user message starts
    with this handoff's continuation (the source transcript, which holds the nonce, is excluded)."""
    folder = sessions_dir(record["transcript"])
    if folder is None:
        return []
    prefix = f"Codex Context Handoff: {record['session']} {record['turn']} {record['nonce']}"
    source = Path(record["transcript"]).resolve()
    found = []
    for path in folder.glob("*/*/*/rollout-*.jsonl"):
        try:
            if path.stat().st_mtime < since or path.resolve() == source:
                continue
            with path.open("rb") as stream:
                meta = (json.loads(stream.readline(1024 * 1024)).get("payload") or {})
            if meta.get("id") == record["session"] or not same_cwd(meta.get("cwd"), record["cwd"]):
                continue
            if prefix.encode() not in path.read_bytes():
                continue
            for row_type, item in transcript_rows(path, os.path.getsize(path), (b'"event_msg"',)):
                message = item.get("item") if item.get("type") == "item_completed" else None
                if isinstance(message, dict) and message.get("type") == "UserMessage" and message_text(
                    message.get("content")
                ).strip().startswith(prefix):
                    found.append(str(path))
                    break
        except (OSError, ValueError, AttributeError):
            continue
    return found


def reconcile_receipt(record, state, old_screen, send_turn, typed_at, history_ok):
    """No receipt within RECEIPT_WAIT: wait longer, then decide from the evidence whether the
    continuation arrived. Only a continuation that provably never arrived may be sent again."""
    receipt = state.with_suffix(".received")
    deadline = time.monotonic() + RECONCILE_WAIT
    while time.monotonic() < deadline:
        if receipt.is_file():
            return True
        if stopped(state):
            raise RuntimeError("Handoff was cancelled after the continuation was sent.")
        time.sleep(POLL)
    view = look(record)
    if view is None:
        raise RuntimeError("The terminal changed hands after the continuation was sent; inspect it before retrying.")
    status, turn, _, screen = view
    draft = composer_text(screen) if screen is not None else None
    if draft and record["nonce"] in draft:
        raise RuntimeError(
            "The continuation was left unsubmitted in the composer; it was not sent again. Inspect the terminal."
        )
    evidence = []
    if screen and record["nonce"] in screen:
        evidence.append("it shows in the chat")
    if turn != send_turn:
        evidence.append(f"Herdr's turn moved from {send_turn} to {turn}")
    rollouts = continuation_rollouts(record, typed_at - 2.0)
    if rollouts:
        evidence.append(f"a new chat's transcript has it ({rollouts[0]})")
    if receipt.is_file():
        return True  # received while the evidence was gathered
    if evidence:
        raise RuntimeError(
            "Continuation delivery could not be confirmed, but it may have arrived (" + "; ".join(evidence)
            + "). It was not sent again; inspect the terminal."
        )
    if receipt.is_file():
        return True
    if status == "idle" and not draft and prompt_state(screen, old_screen, history_ok) == "fresh":
        update_ledger(state, lambda data: data["sends"][-1].update(delivery_absent=True) if data["sends"] else None)
        raise Retryable(
            "The continuation did not arrive (empty prompt, Herdr turn unchanged, no transcript has it).", "post-clear"
        )
    raise RuntimeError(
        f"Continuation delivery could not be confirmed ({describe(view, old_screen, history_ok)}). Inspect the "
        "terminal before retrying."
    )


def switch_chat(state):
    record = json.loads(state.read_text(encoding="utf-8"))
    terminal = record["terminal"]
    lock_path = state.parent.parent / f"{terminal}.lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    failure = None
    with os.fdopen(fd, "w") as lock:
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"Another handoff is switching terminal {terminal}.") from None
            switch_locked(record, state)
        except BaseException as error:
            # Close before disarming, so a late continuation always meets .closed or the guard.
            new_private_file(state.with_suffix(".closed"), f"{error}\n{goal_paused_line(state)}")
            contended = "Another handoff is switching" in str(error)
            if not contended:
                report_failure(state, record, error)
            failure = (error, contended)
            raise
        finally:
            if failure is None or not failure[1]:
                disarm_guard(state, record)
            lock.close()  # released before any notice is typed into the terminal
            if failure is not None and not failure[1]:
                try:
                    announce_failure(state, record)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    log(f"failure notice kept for the next hook ({type(error).__name__}: {error})")


def report_failure(state, record, error):
    """Make a switch that stopped visible: a notice for whichever chat is on the terminal, and no new
    automatic handoff in that chat until more context is used (a human cancellation excepted)."""
    try:
        finish_attempt(state, "failed", str(error))
        held = held_count(state)
        cancelled = state.with_suffix(".cancelled").exists() or (state.parent / ".off").exists()
        text = (
            f"Context handoff did not complete: {error} ({ledger_summary(state)}). The handoff note is "
            f"{state.with_suffix('.md')}" + (f"; it holds {held} worker message(s) that never reached a chat" if held else "")
            + ". Check the terminal; if the work should continue, read the note in a fresh chat."
            + (f" {goal_paused_line(state)}" if goal_paused_line(state) else "")
        )
        post_notice(state.parent.parent, record["terminal"], text)
        if not cancelled:
            replace_private(state.parent / ".switch-failed", json.dumps({
                "usage": record.get("usage") or 0, "turn": record["turn"], "at": utc_now(), "why": str(error),
            }))
    except (OSError, ValueError, KeyError):
        pass


NOTICE_PROMPT = re.compile(r"Codex Context Handoff notice ([0-9a-f]{16})\Z")


def post_notice(root, terminal, text):
    notice_id = secrets.token_hex(8)
    replace_private(root / f"{terminal}.notice", json.dumps({"text": text, "at": utc_now(), "id": notice_id}))
    return notice_id


def announce_failure(state, record):
    """Show a failed switch at once instead of waiting for the person's next prompt: submit, once, the
    plugin's own notice prompt, which its UserPromptSubmit hook blocks (it never reaches the model) and
    answers with the notice. Runs after the guard and the terminal lock were released. Every doubt
    leaves only the durable notice for the next hook in that terminal."""
    terminal, root = record["terminal"], state.parent.parent
    notice = read_json(root / f"{terminal}.notice")
    def skip(why):
        log(f"failure notice kept for the next hook ({why})")
        return False
    if not notice or not isinstance(notice.get("id"), str):
        return skip("no notice")
    if stopped(state) or state.with_suffix(".received").exists():
        return skip("cancelled or delivered")
    if not prompt_hook_evidence(state, record):
        return skip("no proof that the prompt hook runs in this Codex process")
    if not new_private_file(state.with_suffix(".notified"), utc_now() + "\n"):
        return skip("already announced")
    ledger = read_ledger(state)
    phase, clear, sends = ledger.get("phase"), ledger.get("clear") or {}, ledger.get("sends") or []
    # Never while /clear or the continuation may be on its way: only a send proven absent is settled.
    if phase in {"clearing", "sending", "sent"} or any(not send.get("delivery_absent") for send in sends):
        return skip(f"{phase}: the continuation may be in flight")
    info = None
    deadline = time.monotonic() + 2.0
    while True:
        info = terminal_info(terminal, record["cwd"], record["pid"])
        if not info or active_status(info) == "idle" or time.monotonic() >= deadline:
            break
        time.sleep(POLL)
    # Only `idle`: `hc send --if idle` makes Herdr refuse atomically if anything starts meanwhile.
    if not info or active_status(info) != "idle":
        return skip(f"status {active_status(info) if info else 'gone'}")
    screen = read_screen(terminal)
    if screen is None or composer_text(screen) != "" or record["nonce"] in screen:
        return skip("the composer is not empty or the screen is unreadable")
    turn = info.get("turn")
    if clear.get("typed_at") is None:
        latest = recent_turn(record["transcript"])
        if not latest or latest["id"] != record["turn"]:
            return skip("the original chat moved on")
        rows = rows_after(record["transcript"], record.get("offset"))
        if rows is None or user_input(rows, record.get("prompt"), input_counter(state, record)):
            return skip("input arrived in the original chat")
    else:
        found = footer_prompt(screen)
        fresh_held = [e for e in held_events(state) if e.get("session") != record["session"]]
        if type(turn) is not int or type(clear.get("turn")) is not int or turn > clear["turn"] + 1 + len(fresh_held) \
                or not found or found[0].rstrip() != PLACEHOLDER or (
                    not fresh_held and sum(line.startswith("›") for line in screen.splitlines()) != 1):
            return skip("the fresh chat is not at its clean, empty prompt")
    if not (root / f"{terminal}.notice").is_file():
        return skip("the notice was shown meanwhile")
    args = ["send", "--json", "--if", "idle", terminal, f"Codex Context Handoff notice {notice['id']}"]
    try:
        sent = hc(*args, timeout=6)
    except (subprocess.SubprocessError, ValueError) as error:
        return skip(f"hc send failed: {type(error).__name__}")
    log(f"failure notice sent: {json.dumps(sent)}")
    return True


def notice_hook(payload, root, terminal):
    """The plugin's own notice prompt: always blocked, answered with the pending notice (if still there)."""
    match = NOTICE_PROMPT.fullmatch((payload.get("prompt") or "").strip())
    if not match:
        return None
    path = root / f"{terminal}.notice" if TERMINAL.fullmatch(terminal or "") else None
    data = read_json(path) if path else None
    if data and data.get("id") == match.group(1):
        text = pop_notice(root, terminal)
        if text:
            return {"decision": "block", "reason": text}
    return {"decision": "block", "reason": "Codex Context Handoff: this notice was already shown."}


def pop_notice(root, terminal):
    """The pending notice for this terminal, shown once by whichever hook runs there next."""
    if not TERMINAL.fullmatch(terminal or ""):
        return None
    path = root / f"{terminal}.notice"
    taken = path.with_name(f".{path.name}.{os.getpid()}.taken")
    try:
        os.rename(path, taken)
    except OSError:
        return None
    data = read_json(taken)
    taken.unlink(missing_ok=True)
    return data.get("text") if data and isinstance(data.get("text"), str) else None


def failed_recently(folder, used):
    """Whether a switch in this chat failed and too little context was used since to try again."""
    data = read_json(folder / ".switch-failed")
    if not data:
        return False
    return used is None or used < float(data.get("usage") or 0) + RETRY_GROWTH


def switch_locked(record, state):
    arm_guard(state, record)
    set_stage(state, "switch-started")
    context = {}
    while True:
        try:
            switch_attempt(record, state, context)
            return
        except Retryable as error:
            attempt = take_attempt(state, "switch", str(error), error.phase)
            if attempt is None:
                raise RuntimeError(f"{error} All {BUDGET} attempts are used.") from None
            log(f"retryable: {error} Attempt {attempt} of {BUDGET} starts at {error.phase}.")
            if error.phase == "pre-clear":
                set_phase(state, "pre-clear", clear=None, rebase=False)
            else:
                set_phase(state, "post-clear", rebase=True)
            pause_before_retry(record, state)


def pause_before_retry(record, state, seconds=1.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stopped(state):
            raise RuntimeError("Handoff was cancelled before the retry.")
        if consent_revoked(state):
            raise RuntimeError(f"{consent_revoked(state)} before the retry.")
        changed = goal_changed(record)
        if changed:
            raise RuntimeError(f"Stopped before the retry: {changed}.")
        time.sleep(POLL)


def switch_attempt(record, state, context):
    phase = read_ledger(state)["phase"]
    if phase in {"clearing", "sending"}:
        raise RuntimeError(f"An earlier step stopped while {phase}; inspect the terminal before retrying.")
    try:
        if phase in {None, "pre-clear", "switch-started"}:
            pre_clear(record, state, context)
        post_clear(record, state, context)
    except (subprocess.SubprocessError, json.JSONDecodeError) as error:
        now = read_ledger(state)["phase"]
        if now in {"clearing", "sending", "sent"}:
            raise RuntimeError(f"Herdr did not answer while {now} ({type(error).__name__}).") from None
        raise Retryable(f"Herdr did not answer ({type(error).__name__}: {error}).",
                        "post-clear" if now in {"cleared", "post-clear"} else "pre-clear") from None


def pre_clear(record, state, context):
    terminal = record["terminal"]
    set_phase(state, "pre-clear")
    info = terminal_info(terminal, record["cwd"], record["pid"])
    if not info:
        raise RuntimeError("The terminal no longer runs the original Codex in this worktree.")
    start_turn = herdr_turn(info)
    first = not context.get("pre_clear_runs")
    context["pre_clear_runs"] = context.get("pre_clear_runs", 0) + 1
    log(f"switch for {terminal} (pid {record['pid']}) at Herdr turn {start_turn}"
        + ("" if first else f" (attempt {len(read_ledger(state)['attempts'])} of {BUDGET})"))
    capture_manifest(record, state, into_note=first)
    held_base = held_seen(state)
    screen, turn = await_old_chat(record, state, start_turn, held_base, 12.0 if first else RETRY_IDLE_WAIT)
    set_stage(state, "old-idle")
    log(f"old chat idle at Herdr turn {turn}; holding {grace():g}s for cancellation")
    hold_for_cancel(record, state, screen, grace(), turn, held_base)
    latest = recent_turn(record["transcript"])
    if not latest or latest["id"] != record["turn"]:
        raise RuntimeError("A new turn started before /clear.")
    hold_for_cancel(record, state, screen, 0, turn, held_base)  # final recheck immediately before /clear
    set_stage(state, "grace-held")
    intent = time.time()
    set_phase(state, "clearing", clear={"intent_at": intent, "turn": turn})
    try:
        cleared = hc("keys", "--json", terminal, "/", "c", "l", "e", "a", "r", "enter")
    except (subprocess.SubprocessError, ValueError) as error:
        cleared = reconcile_clear(record, screen, turn, f"{type(error).__name__}: {error}")
    log(f"/clear sent: {json.dumps(cleared)}")
    # hc keys waits for a quiet moment first; the chat must still be the one just checked.
    if cleared.get("turn") != turn or cleared.get("status") not in {"done", "idle"}:
        raise RuntimeError(f"The terminal changed while /clear was typed ({json.dumps(cleared)}).")
    typed = time.time()
    set_phase(state, "cleared", clear={"intent_at": intent, "typed_at": typed, "turn": turn, "result": cleared})
    set_stage(state, "cleared")
    context.update(old_screen=screen, cleared_turn=turn, typed_at=typed)


def post_clear(record, state, context):
    ledger = read_ledger(state)
    if "old_screen" not in context:
        raise RuntimeError("The switch lost track of the screen before /clear; inspect the terminal.")
    old_screen, typed_at = context["old_screen"], context["typed_at"]
    rebased = bool(ledger.get("rebase"))
    set_phase(state, "post-clear")
    held_base = held_seen(state)
    # The fresh chat already shows held worker messages: the hold itself proves the prompt hook runs there.
    history_ok = rebased and any(entry.get("session") != record["session"] for entry in held_events(state))
    cleared_turn = context["cleared_turn"]
    if rebased:
        view = look(record)
        if view is None or type(view[1]) is not int:
            raise RuntimeError("The terminal no longer runs the original Codex in this worktree.")
        cleared_turn = view[1]
        context["cleared_turn"] = cleared_turn
    cache = context.setdefault("cache", {})
    settled = await_fresh_chat(record, state, old_screen, cleared_turn, cache, rebased, held_base, history_ok)
    set_stage(state, "fresh-ready")
    # Final recheck immediately before the continuation.
    view = look(record)
    problem = fresh_chat_problem(view, state, record, old_screen, cleared_turn, False, cache, history_ok)
    if problem and not may_be_held(problem):
        raise RuntimeError(f"{problem} just before the continuation ({describe(view, old_screen, history_ok)}).")
    if problem or view[0] != "idle" or view[1:] != settled:
        settle_change(state, record, held_base, "post-clear", (
            f"{problem or 'The fresh chat changed'} just before the continuation "
            f"({describe(view, old_screen, history_ok)})."
        ))
    send_turn = view[1]
    update_ledger(state, lambda data: (data.update(phase="sending"), data["sends"].append(
        {"intent_at": time.time(), "turn": send_turn}
    )))
    try:
        sent = hc("send", "--json", "--if", "idle", record["terminal"], continuation(record, state))
    except (subprocess.SubprocessError, ValueError, OSError) as error:
        sent = {"error": f"{type(error).__name__}: {error}"}
    log(f"continuation sent: {json.dumps(sent)}")
    update_ledger(state, lambda data: (data.update(phase="sent"), data["sends"][-1].update(
        sent_at=time.time(), result=sent
    )))
    set_stage(state, "continuation-sent")
    receipt = state.with_suffix(".received")
    deadline = time.monotonic() + (RECEIPT_WAIT if sent.get("started") else 3.0)
    while time.monotonic() < deadline:
        if receipt.is_file():
            break
        time.sleep(POLL)
    if not receipt.is_file():
        log("no receipt yet; reconciling the delivery evidence")
        reconcile_receipt(record, state, old_screen, send_turn, typed_at, history_ok)
    log(f"Continuation received by {receipt.read_text(encoding='utf-8').strip()}.")
    set_phase(state, "delivered")


def hold_worker_message(state, record, payload, prompt, terminal):
    """Keep a verified worker message that arrives while the switch is pending out of both chats: it is
    appended (durably, under the note lock, deduplicated) to the held log and to the note with a new
    read token revision, before the prompt is blocked. Returns the block reason, "pass" when the
    continuation was received meanwhile (the message then reaches the fresh chat as usual), or None
    when the message is not a verified worker message."""
    verdict = InputJudge(state, terminal).verdict(prompt)
    if verdict["kind"] not in WORKER_KINDS:
        return None
    session = payload.get("session_id") if ID.fullmatch(payload.get("session_id") or "") else None
    key = digest(f"{session}\n{payload.get('turn_id') or ''}\n{prompt}")
    secret = (read_json(state.with_suffix(".restore.json")) or {}).get("secret")
    with open_lock(state.with_suffix(".note-lock")):
        if state.with_suffix(".received").exists():
            return "pass"
        entries = held_entries(state)
        same = next((entry for entry in entries if entry.get("key") == key), None)
        with open(state.with_suffix(".held-events.jsonl"), "a", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)  # every delivery, so a repeat still explains a changed screen
            stream.write(json.dumps({"at": utc_now(), "session": session, "key": key,
                                     "repeat": bool(same)}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if same:
            revision = same["revision"]
        else:
            revision = len(entries) + 1
            entry = {
                "revision": revision, "key": key, "at": utc_now(), "session": session, "from": verdict["sender"],
                "kind": verdict["kind"], "why": verdict["why"], "sha256": digest(prompt), "text": prompt,
            }
            with open(state.with_suffix(".held.jsonl"), "a", encoding="utf-8") as stream:
                os.chmod(stream.name, 0o600)
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            shown = prompt if len(prompt) <= HELD_IN_NOTE else (
                prompt[:HELD_IN_NOTE] + f"\n[... {len(prompt) - HELD_IN_NOTE} more characters in "
                f"{state.with_suffix('.held.jsonl')}, revision {revision}]"
            )
            with state.with_suffix(".md").open("a", encoding="utf-8") as stream:
                stream.write(
                    f"\n## Held worker message {revision} (from {verdict['sender']}, {entry['at']})\n\n"
                    "This message arrived while the chat switched; it was not shown to either chat.\n\n"
                    f"{shown}\n" + (f"\nRead token (revision {revision}): {read_token(secret, revision)}\n" if secret else "")
                )
                stream.flush()
                os.fsync(stream.fileno())
    return (
        f"Context handoff: this message from Herdr worker {verdict['sender']} was held for the fresh chat (held "
        f"message {revision}, saved with the handoff note); it did not run here."
    )


def mark_takeover(payload, root, terminal):
    """Stop this terminal's pending handoff on any prompt other than its exact continuation, except a
    verified worker message, which is held for the fresh chat. Returns (notice, block reason)."""
    try:
        session, turn, nonce = (root / f"{terminal}.switch").read_text(encoding="utf-8").split()
        state = store_for(root, session, turn)
        record = json.loads(state.read_text(encoding="utf-8"))
        if record["nonce"] != nonce or record["terminal"] != terminal:
            return None, None
        expected = continuation(record, state)
    except (OSError, ValueError, KeyError, TypeError):
        return None, None  # no armed guard; the switch itself fails closed without one
    if any(state.with_suffix(suffix).exists() for suffix in DONE):
        return None, None
    prompt = (payload.get("prompt") or "").strip()
    if prompt == expected:
        return None, None
    if prompt.startswith("<hc") and state.with_suffix(".md").is_file():
        held = hold_worker_message(state, record, payload, prompt, terminal)
        if held == "pass":
            return None, None
        if held:
            return None, held
    chat = payload.get("session_id") or ""
    goal = goal_paused_line(state)
    new_private_file(state.with_suffix(".cancelled"), (
        f"a prompt was submitted in chat {chat if ID.fullmatch(chat) else 'unknown'} before the continuation\n{goal}"
    ))
    held = held_count(state)
    return (
        "Context handoff stopped: a prompt was submitted before the continuation. "
        "The handoff note stays in the plugin data directory."
        + (f" It holds {held} worker message(s) that never reached a chat: {state.with_suffix('.md')}." if held else "")
        + (f" {goal}" if goal else "")
    ), None


def prompt_hook(payload, root, terminal):
    text = (payload.get("prompt") or "").strip()
    match = RESUME.search(text)
    if not match:
        return {}
    source, turn, nonce = match.groups()
    try:
        state = store_for(root, source, turn)
    except ValueError:
        return {}
    note = state.with_suffix(".md")
    if not note.is_file():
        return {}
    record = json.loads(state.read_text(encoding="utf-8"))
    if nonce != record["nonce"] or terminal != record["terminal"]:
        return {}
    manual = f" Read the handoff note at {note} yourself if the work should continue."
    if text != continuation(record, state):
        # A draft or another message got mixed in; never run it as a handoff.
        new_private_file(
            state.with_suffix(".cancelled"), "the continuation arrived mixed with other text\n" + goal_paused_line(state)
        )
        return {"decision": "block", "reason": "The handoff prompt arrived mixed with other text, so it was not run." + manual}
    if state.with_suffix(".cancelled").exists():
        return {"decision": "block", "reason": "This handoff was cancelled before its continuation arrived." + manual}
    if state.with_suffix(".closed").exists():
        return {"decision": "block", "reason": "This handoff stopped before its continuation arrived." + manual}
    destination = payload.get("session_id", "")
    if destination == source or not same_cwd(payload.get("cwd"), record["cwd"]):
        return {"decision": "block", "reason": "This handoff must open in a fresh chat in the original worktree."}
    destination_turn = payload.get("turn_id") or ""
    if not ID.fullmatch(destination) or not ID.fullmatch(destination_turn):
        return {"decision": "block", "reason": "Could not confirm the destination chat and turn."}
    transfer = None
    if state.with_suffix(".goal.json").is_file():
        if consent_revoked(state):
            new_private_file(state.with_suffix(".closed"), "Goal transfer was turned off\n" + goal_paused_line(state))
            return {"decision": "block", "reason": (
                "This handoff carries a paused Goal, but Goal transfer was turned off before it arrived, so it "
                f"was not run. {goal_paused_line(state)}" + manual
            )}
        transfer = json.loads(state.with_suffix(".goal.json").read_text(encoding="utf-8"))
    # Under the note lock: no worker message is held after the receipt, so the note (and its last read
    # token) is final from here on, and later worker messages reach this chat directly.
    with open_lock(state.with_suffix(".note-lock")):
        if not new_private_file(state.with_suffix(".received"), destination + "\n"):
            return {"decision": "block", "reason": "This handoff has already been delivered."}
        disarm_guard(state, record)
    set_stage(state, "delivered", chat=destination, held=held_count(state))
    # The receiving turn is checked like any other; its headroom is measured from this chat's first
    # request (baseline), so a note that starts past the threshold cannot hand off again at once.
    folder = root / destination
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    new_private_file(folder / ".received-from", f"{source} {turn}\n")
    if not transfer:
        return {}
    # This chat's hooks confirm once that the Goal was recreated exactly (restore_check, confirm_restore).
    new_private_file(folder / ".goal-restore", json.dumps({
        "state": str(state), "source_session": transfer["source_session"], "objective": transfer["objective"],
        "objective_sha256": transfer["objective_sha256"], "remaining": transfer["remaining"],
    }, ensure_ascii=False))
    replace_private(folder / ".goal-lineage.json", json.dumps(
        dict(transfer["lineage"], objective_sha256=transfer["objective_sha256"])
    ))
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": restore_instruction(
        transfer, "a Goal was transferred with this handoff. Before anything else:"
    )}}


def received_handoff(folder, root):
    """(source state, record) of the handoff this chat received, or (None, None)."""
    try:
        source, turn = (folder / ".received-from").read_text(encoding="utf-8").split()
        state = store_for(root, source, turn)
        record = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    return state, record


def restoration_missing(state):
    """What the destination still has to do before the handoff counts as restored."""
    missing = []
    if manifest_entries(state) and not state.with_suffix(".workers-report.json").is_file():
        missing.append("review the Herdr workers (`codex context-handoff workers`, then `workers decide` for "
                       "each and `workers done`)")
    missing.append("confirm with `codex context-handoff restored --token <the last Read token in the note>`")
    return missing


def restoration_check(payload, root, session):
    """Stop in the chat that received a handoff: one reminder to finish the restore checklist, then one
    notice that it was not confirmed. A transferred Goal is confirmed first (restore_check)."""
    folder = root / session
    state, _ = received_handoff(folder, root)
    if state is None or state.with_suffix(".restored").exists() or not state.with_suffix(".restore.json").is_file():
        return None
    if pending_restore(folder)[0] is not None:
        return None  # create_goal comes first; restore_check handles it
    if new_private_file(folder / ".restore-reminded", utc_now() + "\n"):
        return {"decision": "block", "reason": (
            "Codex Context Handoff: before you finish, complete the handoff's restore checklist (it is at the end of "
            f"the note {state.with_suffix('.md')}): " + "; ".join(restoration_missing(state)) + "."
        )}
    if new_private_file(folder / ".restore-unconfirmed", utc_now() + "\n"):
        return {"systemMessage": (
            "Context handoff: this chat did not confirm the handoff's restore checklist (note read, worker review). "
            f"Run `codex context-handoff status` here to see what is missing; the note is {state.with_suffix('.md')}."
        )}
    return None


def end_restoration_wait(payload, root, terminal, judges=None):
    """A person's prompt in the receiving chat before the restore was confirmed ends the reminder, so it
    never lands in their next task; a verified worker message or a `handoff` control does not."""
    session = payload.get("session_id") or ""
    if not ID.fullmatch(session):
        return None
    folder = root / session
    state, _ = received_handoff(folder, root)
    if state is None or state.with_suffix(".restored").exists() or (folder / ".restore-unconfirmed").exists():
        return None
    if not state.with_suffix(".restore.json").is_file():
        return None
    if is_control(payload) or received_worker_message(payload, root, terminal, judges):
        return None
    new_private_file(folder / ".restore-reminded", utc_now() + "\n")
    new_private_file(folder / ".restore-unconfirmed", "a prompt arrived first\n")
    return (
        "Context handoff: the restore checklist of the handoff this chat received was not confirmed before this "
        "prompt; the plugin no longer reminds about it. `codex context-handoff status` shows what is missing."
    )


class Refusal(Exception):
    pass


def helper_context(root, need_goal=True):
    """The chat a helper runs in, checked like `request-transfer`: the opted-in foreground Codex of this
    Herdr terminal running this hook script, and the receiving chat of a handoff in this terminal."""
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or ""
    terminal = os.environ.get("HC_TERMINAL_ID", "")
    if not ID.fullmatch(session):
        raise Refusal("CODEX_THREAD_ID is missing; run this from a tool call in the Codex chat")
    transcript = find_transcript(session)
    if not transcript:
        raise Refusal(f"no transcript was found for chat {session}")
    with transcript.open("rb") as stream:
        meta = json.loads(stream.readline(1024 * 1024)).get("payload") or {}
    payload = {"session_id": session, "cwd": meta.get("cwd"), "transcript_path": str(transcript)}
    info, why = check_chat(payload, terminal)
    if not info:
        raise Refusal(why)
    try:
        current = (root / session / ".hook-script").read_text(encoding="utf-8") == hook_script_record(
            info["foreground"]["pid"]
        )
    except OSError:
        current = False
    if not current:
        raise Refusal("this chat's Codex process has not run this version of the handoff hooks")
    folder = root / session
    state, record = received_handoff(folder, root)
    if state is None:
        raise Refusal("this chat did not receive a handoff")
    if record.get("terminal") != terminal:
        raise Refusal("the handoff belongs to another Herdr terminal")
    if need_goal and pending_restore(folder)[0] is not None and not confirm_restore(folder, transcript, session):
        raise Refusal("a Goal was transferred with this handoff: call create_goal exactly as the note says first")
    return {"root": root, "session": session, "terminal": terminal, "folder": folder, "state": state,
            "record": record}


def worker_now(worker, terminals, terminal):
    """The manifest worker's state now: live, finished, gone (no longer listed) or stale (another
    terminal under that address, or no longer a descendant of this terminal)."""
    entry = terminals.get(worker["address"])
    if entry is None:
        return "gone", None
    if entry.get("created_ms") != worker.get("created_ms") or entry.get("parent") != worker.get("parent") or not (
        lineage(terminals, worker["address"], terminal)
    ):
        return "stale", entry
    return ("live" if entry.get("state") == "live" else "finished"), entry


def decisions_path(state):
    return state.with_suffix(".workers-decisions.json")


def capture_evidence(state, address, label, strict=False):
    """Save the worker's screen and recent history before anything else is done with it.

    Herdr reports a history read of a working program as incomplete (its output keeps moving), while
    the current screen alone reads complete. For a review (keep or reuse a running worker) the screen
    is enough and is recorded as such (`scope` screen, `partial_history`); `strict` (before a close)
    accepts only a complete history read.
    """
    folder = state.with_suffix(".workers-evidence")
    folder.mkdir(mode=0o700, exist_ok=True)
    result, scope = hc("read", "--json", "--lines", "400", address), "history"
    complete = bool(result.get("complete")) if isinstance(result, dict) else False
    if not complete and not strict:
        screen = hc("read", "--json", "--lines", "0", address)
        if isinstance(screen, dict) and screen.get("complete"):
            result, scope, complete = screen, "screen", True
    text = result.get("text", "") if isinstance(result, dict) else ""
    path = folder / f"{address}-{label}-{int(time.time() * 1000)}.txt"
    replace_private(path, text)
    return {"path": str(path), "sha256": digest(text), "lines": text.count("\n"), "at": utc_now(),
            "complete": complete, "scope": scope, "partial_history": scope != "history"}


def workers_inspect(ctx):
    state, terminal = ctx["state"], ctx["terminal"]
    manifest = read_json(state.with_suffix(".workers.json"))
    if manifest is None:
        raise Refusal("this handoff has no worker manifest (the switch could not record one)")
    try:
        terminals = hc_list(timeout=8)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Refusal(f"Herdr could not be read ({type(error).__name__})") from None
    decisions = read_json(decisions_path(state)) or {}
    rows = []
    for worker in manifest.get("workers", []):
        now, entry = worker_now(worker, terminals, terminal)
        row = {"address": worker["address"], "name": worker.get("name"), "parent": worker.get("parent"),
               "depth": worker.get("depth"), "at_handoff": f"{worker.get('state')}/{worker.get('status')}",
               "now": now, "status": status_word(entry) if entry else None,
               "result_location": worker.get("result_location"), "decision": decisions.get(worker["address"])}
        if now in {"live", "finished"}:
            try:
                row["evidence"] = capture_evidence(state, worker["address"], "inspect")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                row["evidence_error"] = f"{type(error).__name__}: {error}"
        rows.append(row)
    listed = {worker["address"] for worker in manifest.get("workers", [])}
    new = sorted(a for a in descendants(terminals, terminal) if a not in listed and terminals[a].get("state") == "live")
    undecided = [row["address"] for row in rows if row["now"] in {"live", "finished"} and not row["decision"]]
    with open_lock(state.with_suffix(".workers-lock")):
        review = read_json(state.with_suffix(".workers-review.json")) or {"snapshots": []}
        review["snapshots"].append({"at": utc_now(), "chat": ctx["session"], "workers": rows, "new_workers": new})
        replace_private(state.with_suffix(".workers-review.json"), json.dumps(review, ensure_ascii=False))
    return {
        "handoff": f"{ctx['record']['session']}/{ctx['record']['turn']}", "terminal": terminal, "workers": rows,
        "new_workers_not_in_review": new, "undecided": undecided,
        "next": ("Read each worker's result (its evidence file and any result location), then run `codex "
                 "context-handoff workers decide <id> keep|reuse|close --reason \"...\"` for every undecided worker "
                 "(close needs --result <where its result is captured>), then `codex context-handoff workers done`."),
    }


def workers_decide(ctx, address, decision, reason, result_location, cancel_working):
    state, terminal = ctx["state"], ctx["terminal"]
    if decision not in {"keep", "reuse", "close"}:
        raise Refusal("the decision must be keep, reuse or close")
    if not reason.strip():
        raise Refusal("--reason is required")
    worker = manifest_entries(state).get(address)
    if worker is None:
        raise Refusal(f"{address} is not in this handoff's worker manifest; only those workers are reviewed here")
    if decision == "close" and not (result_location or "").strip():
        raise Refusal("--result is required for close: say where this worker's result was captured")
    with open_lock(state.with_suffix(".workers-lock")):
        decisions = read_json(decisions_path(state)) or {}
        prior = decisions.get(address)
        if prior and prior.get("outcome") in {"closed", "already-finished"} and not (
            decision == "close" and prior.get("decision") != "close"
        ):
            return {"worker": address, "decision": prior, "repeat": True}
        try:
            terminals = hc_list(timeout=8)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise Refusal(f"Herdr could not be read ({type(error).__name__})") from None
        now, entry = worker_now(worker, terminals, terminal)
        record = {"decision": decision, "reason": reason.strip(), "result_location": result_location, "at": utc_now(),
                  "chat": ctx["session"], "state_now": now, "status_now": status_word(entry) if entry else None}
        if result_location and Path(result_location).is_file():
            try:
                record["result_sha256"] = hashlib.sha256(Path(result_location).read_bytes()).hexdigest()
            except OSError:
                pass
        if decision != "close":
            record["outcome"] = "recorded" if now in {"live", "finished"} else "not-actionable"
        elif now in {"gone", "stale"}:
            record["outcome"] = "not-actionable"
        elif now == "finished":
            record["outcome"] = "already-finished"  # nothing to end; its screen and history stay in Herdr
        else:
            status = status_word(entry)
            if status not in {"done", "idle"} and not cancel_working:
                raise Refusal(f"{address} is {status or 'not reporting a status'}, so it is kept; close it only "
                              "with --cancel-working after you decided its work can be cancelled")
            closable(worker, entry, terminals, address)
            if not any(snapshot_has(state, address)):
                raise Refusal(f"run `codex context-handoff workers` first so {address}'s result is captured")
            try:
                record["evidence"] = capture_evidence(state, address, "before-close", strict=True)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise Refusal(f"{address}'s screen could not be saved ({type(error).__name__}), so it was not closed") from None
            if not record["evidence"]["complete"]:
                raise Refusal(f"Herdr gave an incomplete read of {address}'s screen and history, so it was not "
                              "closed (a worker that is still producing output can be kept and reviewed later)")
            # Reading the screen takes time: check everything again right before the kill.
            try:
                terminals = hc_list(timeout=8)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise Refusal(f"Herdr could not be read again before the close ({type(error).__name__})") from None
            again, entry = worker_now(worker, terminals, terminal)
            if again != "live":
                raise Refusal(f"{address} changed while its screen was saved (now {again}); run `workers` again")
            if status_word(entry) != record["status_now"]:
                raise Refusal(f"{address} changed from {record['status_now']} to {status_word(entry)} while its screen "
                              "was saved; run `workers` again")
            closable(worker, entry, terminals, address)
            killed = hc("kill", address)
            try:
                after = hc_list(timeout=8).get(address)
            except (OSError, ValueError, subprocess.SubprocessError):
                after = None
            ended = (isinstance(killed, dict) and killed.get("state") == "finished") or (
                isinstance(after, dict) and after.get("state") == "finished")
            record["kill"] = {key: killed.get(key) for key in ("state", "reason", "exit_code", "signal")} if isinstance(
                killed, dict) else killed
            record["outcome"] = "closed" if ended else "close-unconfirmed"
        decisions[address] = record
        replace_private(decisions_path(state), json.dumps(decisions, ensure_ascii=False))
    return {"worker": address, "decision": record}


def closable(worker, entry, terminals, address):
    """Refuse a close that is not safe now: another program in the worker (a resumed or replaced
    foreground), or live workers of its own (bottom-up: those are decided and closed first)."""
    foreground = entry.get("foreground") if isinstance(entry.get("foreground"), dict) else {}
    if worker.get("pid") is not None and entry.get("pid") != worker.get("pid"):
        raise Refusal(f"{address} runs another process than at the handoff, so it is not closed automatically")
    if worker.get("foreground_pid") is not None and foreground.get("pid") != worker.get("foreground_pid"):
        raise Refusal(f"{address}'s program was restarted since the handoff, so it is not closed automatically")
    if worker.get("foreground") and foreground.get("name") not in {None, worker.get("foreground")}:
        raise Refusal(f"{address} runs {foreground.get('name')} now instead of {worker.get('foreground')}, so it is "
                      "not closed automatically")
    busy = sorted(a for a in descendants(terminals, address) if terminals[a].get("state") == "live")
    if busy:
        raise Refusal(f"{address} has live workers of its own ({', '.join(busy)}); close those first (the ones in "
                      "this review with `workers decide`, any newer ones by hand)")


def snapshot_has(state, address):
    review = read_json(state.with_suffix(".workers-review.json")) or {}
    for snapshot in review.get("snapshots", []):
        for row in snapshot.get("workers", []):
            if row.get("address") == address and (row.get("evidence") or {}).get("complete"):
                yield True


def workers_done(ctx):
    state, terminal = ctx["state"], ctx["terminal"]
    manifest = read_json(state.with_suffix(".workers.json"))
    if manifest is None:
        raise Refusal("this handoff has no worker manifest")
    try:
        terminals = hc_list(timeout=8)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Refusal(f"Herdr could not be read ({type(error).__name__})") from None
    decisions = read_json(decisions_path(state)) or {}
    rows, undecided, unread, still_live = [], [], [], []
    for worker in manifest.get("workers", []):
        now, _ = worker_now(worker, terminals, terminal)
        decision = decisions.get(worker["address"])
        if not decision and now in {"live", "finished"}:
            undecided.append(worker["address"])
        if now in {"live", "finished"} and not any(snapshot_has(state, worker["address"])):
            unread.append(worker["address"])
        if decision and decision.get("decision") == "close" and now == "live":
            still_live.append(worker["address"])
        rows.append({"address": worker["address"], "name": worker.get("name"), "at_handoff": worker.get("state"),
                     "now": now, "decision": decision or {"outcome": "not-actionable" if now in {"gone", "stale"}
                                                          else "undecided"}})
    if unread:
        raise Refusal("these workers' screens were not saved yet (run `codex context-handoff workers`): "
                      + ", ".join(unread))
    if undecided:
        raise Refusal("these workers have no decision yet: " + ", ".join(undecided))
    if still_live:
        raise Refusal("these workers were to be closed but are still running: " + ", ".join(still_live))
    report = {"handoff": f"{ctx['record']['session']}/{ctx['record']['turn']}", "terminal": terminal,
              "chat": ctx["session"], "at": utc_now(), "workers": rows}
    replace_private(state.with_suffix(".workers-report.json"), json.dumps(report, ensure_ascii=False))
    lines = [f"# Worker review for handoff {report['handoff']}", "", f"Terminal {terminal}, chat {ctx['session']}, "
             f"{report['at']}. Finished workers are not removed from Herdr, so their screens and history stay "
             "available.", "", "| Worker | At handoff | Now | Decision | Outcome | Reason | Result |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        d = row["decision"]
        lines.append(f"| {row['address']} {row['name'] or ''} | {row['at_handoff']} | {row['now']} | "
                     f"{d.get('decision', '-')} | {d.get('outcome')} | {d.get('reason', '')} | "
                     f"{d.get('result_location') or ''} |")
    replace_private(state.with_suffix(".workers-report.md"), "\n".join(lines) + "\n")
    set_stage(state, "workers-reviewed", report=str(state.with_suffix(".workers-report.json")))
    return {"report": str(state.with_suffix(".workers-report.md")), "workers": rows}


def confirm_restored(ctx, token):
    state = ctx["state"]
    secret = (read_json(state.with_suffix(".restore.json")) or {}).get("secret")
    if not secret:
        raise Refusal("this handoff has no restore checklist")
    latest = held_count(state)
    if token != read_token(secret, latest):
        earlier = next((r for r in range(latest) if token == read_token(secret, r)), None)
        if earlier is not None:
            raise Refusal(f"that token is from revision {earlier}; {latest - earlier} held worker message(s) were "
                          "added to the note after it. Read the note again and use its last Read token")
        raise Refusal("the token does not match the note's last Read token")
    missing = [item for item in restoration_missing(state) if not item.startswith("confirm")]
    if missing:
        raise Refusal("first " + "; ".join(missing))
    new_private_file(state.with_suffix(".restored"), json.dumps({"chat": ctx["session"], "at": utc_now(),
                                                                 "revision": latest}))
    new_private_file(ctx["folder"] / ".restored", f"{state}\n")
    set_stage(state, "restored", chat=ctx["session"], revision=latest)
    return {"restored": True, "note_revision": latest, "handoff": f"{ctx['record']['session']}/{ctx['record']['turn']}"}


def helper_status(ctx):
    state = ctx["state"]
    ledger = read_ledger(state)
    return {"handoff": f"{ctx['record']['session']}/{ctx['record']['turn']}", "ledger": ledger,
            "held_worker_messages": held_count(state), "restored": state.with_suffix(".restored").exists(),
            "missing": [] if state.with_suffix(".restored").exists() else restoration_missing(state),
            "goal_restore_pending": pending_restore(ctx["folder"])[0] is not None}


def helper_main(root, argv):
    """`codex context-handoff workers|restored|status ...`, run from a tool call in the receiving chat."""
    import argparse
    parser = argparse.ArgumentParser(prog="codex context-handoff")
    commands = parser.add_subparsers(dest="command", required=True)
    workers = commands.add_parser("workers")
    actions = workers.add_subparsers(dest="action")
    actions.add_parser("inspect")
    decide = actions.add_parser("decide")
    decide.add_argument("worker")
    decide.add_argument("decision", choices=["keep", "reuse", "close"])
    decide.add_argument("--reason", required=True)
    decide.add_argument("--result")
    decide.add_argument("--cancel-working", action="store_true")
    actions.add_parser("done")
    restored = commands.add_parser("restored")
    restored.add_argument("--token", required=True)
    commands.add_parser("status")
    args = parser.parse_args(argv)
    try:
        ctx = helper_context(Path(root).resolve(), need_goal=args.command != "status")
        if args.command == "status":
            output = helper_status(ctx)
        elif args.command == "restored":
            output = confirm_restored(ctx, args.token)
        elif args.action == "decide":
            output = workers_decide(ctx, args.worker, args.decision, args.reason, args.result, args.cancel_working)
        elif args.action == "done":
            output = workers_done(ctx)
        else:
            output = workers_inspect(ctx)
    except Refusal as error:
        print(f"Codex Context Handoff: {error}.", file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


def with_notice(result, notice):
    """Add a pending notice to a hook result without hiding its own message."""
    if not notice:
        return result
    result = dict(result)
    result["systemMessage"] = f"{result['systemMessage']} {notice}" if result.get("systemMessage") else notice
    return result


def main():
    if sys.argv[1:2] == ["--switch"]:
        switch_chat(Path(sys.argv[2]))
        return
    if sys.argv[1:2] == ["--request-transfer"] and len(sys.argv) == 3:
        sys.exit(request_transfer(sys.argv[2]))
    if sys.argv[1:2] == ["--helper"] and len(sys.argv) >= 4:
        sys.exit(helper_main(sys.argv[2], sys.argv[3:]))
    payload = json.load(sys.stdin)
    terminal = os.environ.get("HC_TERMINAL_ID", "")
    root = os.environ.get("PLUGIN_DATA")
    if not root:
        return
    # Resolved, so a plugin data directory that is a symlink to another install's (same handoffs, other
    # marketplace key) yields the same note path in the continuation on both sides.
    root = Path(root).resolve()
    event = payload.get("hook_event_name")
    opted_in = os.environ.get("CODEX_CONTEXT_HANDOFF") == "1" and TERMINAL.fullmatch(terminal)
    if event == "UserPromptSubmit":
        own = notice_hook(payload, root, terminal)
        if own is not None:
            print(json.dumps(own), flush=True)
            return
        notice, held = mark_takeover(payload, root, terminal) if opted_in else (None, None)
        if held:
            # A worker message held for the fresh chat: it reaches neither chat's model.
            print(json.dumps({"decision": "block", "reason": held}), flush=True)
            return
        # Runs before prompt_hook, so only a prompt after the continuation (or a second one) cancels.
        judges = {}  # one Herdr snapshot and the received handoff's manifest for this whole hook run
        restore = cancel_restore(payload, root, terminal, judges)
        if restore:
            notice = f"{notice} {restore}" if notice else restore
        unconfirmed = end_restoration_wait(payload, root, terminal, judges)
        if unconfirmed:
            notice = f"{notice} {unconfirmed}" if notice else unconfirmed
        pending_notice = pop_notice(root, terminal) if opted_in else None
        if pending_notice:
            notice = f"{pending_notice} {notice}" if notice else pending_notice
        # Controls work even without the opt-in so `handoff status` can explain why.
        result = control_hook(payload, root, terminal)
        if result is not None:
            if notice:
                result["reason"] += f" {notice}"
        elif opted_in:
            mark_closing(payload, root)
            result = manual_hook(payload, root, terminal) or prompt_hook(payload, root, terminal)
            if notice and "decision" not in result:
                result = with_notice(result, notice)
        elif notice:
            result = {"systemMessage": notice}
        else:
            return
        print(json.dumps(result), flush=True)
        if opted_in:
            try:
                note_prompt_hook(payload, root, terminal)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                pass  # missing evidence only shortens a later startup wait
        return
    if not opted_in:
        return
    if event == "Stop":
        result = stop_hook(payload, root, terminal)
    elif event == "PreToolUse":
        result = gate_hook(payload, root, terminal)
    elif event == "PostToolUse":
        result = tool_hook(payload, root, terminal)
    else:
        return
    print(json.dumps(with_notice(result, pop_notice(root, terminal))))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Codex Context Handoff: {error}", file=sys.stderr)
        if sys.argv[1:2] == ["--switch"]:
            try:
                new_private_file(Path(sys.argv[2]).with_suffix(".closed"), f"{error}\n")
            except OSError:
                pass
            sys.exit(1)
