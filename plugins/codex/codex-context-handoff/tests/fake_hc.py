#!/usr/bin/env python3
"""A stand-in for Herdr's `hc` used only by the end-to-end tests.

It keeps one JSON world file (FAKE_HC_WORLD) with the terminals, the Codex screen of the handoff's
terminal and which chat it shows. Submitted text runs the real hook script as Codex's
UserPromptSubmit would, so the plugin's own decisions (hold, cancel, accept) shape what happens next.
Every call is logged in the world file, so a test can prove the real `hc` was never used.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLACEHOLDER = "› Ask Codex to do anything"
STATUS = "\n\n  gpt · ~/work\n  ? for shortcuts"
FRESH = "  >_ OpenAI Codex (v0.167.2)\n\n" + PLACEHOLDER + STATUS


def append_rows(transcript, rows):
    if transcript:
        with open(transcript, "a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(row) + "\n" for row in rows)


def event(kind, **fields):
    return {"type": "event_msg", "payload": {"type": kind, **fields}}


def started(turn):
    return [event("task_started", turn_id=turn, turn_attribution={"turn_id": turn, "turn_trigger": "user"})]


def blocked_rest(turn):
    """What Codex 0.169 records for a prompt its UserPromptSubmit hook blocked: context, no content."""
    return [
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"type": "input_text", "text": "<skills_instructions>"}]}},
        {"type": "world_state", "payload": {"full": False}},
        {"type": "turn_context", "payload": {"turn_id": turn}},
        event("task_complete", turn_id=turn, last_agent_message=None),
    ]


def answered_rest(turn, text):
    return [
        event("item_completed", item={"type": "UserMessage", "id": turn, "content": [{"type": "text", "text": text}]}),
        event("item_completed", item={"type": "AgentMessage", "id": f"a{turn}",
                                      "content": [{"type": "Text", "text": "ok"}]}),
        event("task_complete", turn_id=turn, last_agent_message="ok"),
    ]


def screen_with(history, text, reply):
    body = history.split("\n" + PLACEHOLDER)[0]
    first = text.splitlines()[0][:60] if text else ""
    return f"{body}\n› {first}\n\n• {reply[:60]}\n\n{PLACEHOLDER}{STATUS}"


class World:
    """The world file under an exclusive lock. The lock is never held while the real hook runs: the hook
    calls this fake `hc` itself (ls, get), so a `send` reads what it needs, releases the lock, runs the
    hook, then takes the lock again and applies the result to the world as it is then."""

    def __init__(self):
        self.path = Path(os.environ["FAKE_HC_WORLD"])

    def __enter__(self):
        self.lock = open(str(self.path) + ".lock", "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.data = json.loads(self.path.read_text())
        return self.data

    def __exit__(self, *_):
        self.path.write_text(json.dumps(self.data))
        self.lock.close()


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    with World() as world:
        world["calls"].append(sys.argv[1:])
        if args[0] != "send":
            out, code = handle(world, args)
        else:
            codex = world["terminals"][world["codex"]]
            if "--if" in args and codex["status"]["state"] != args[args.index("--if") + 1]:
                out, code = {"error": "status_mismatch"}, 1
            else:
                out, code = None, 0
                context = {key: world[key] for key in ("session", "transcripts", "cwd", "hook_env", "script")}
                context["call"] = len(world["calls"])
                context["turn"] = f"u{context['call']}"
                context["hook_delay"] = world.get("hook_delay", 0)
                # Codex starts the turn before its UserPromptSubmit hook runs.
                append_rows(world["transcripts"].get(world["session"]), started(context["turn"]))
                codex["status"] = {"app": "codex", "state": "working"}
    if out is None:
        time.sleep(context["hook_delay"])  # Codex may take a while to run the hook (startup, MCP servers)
        result, run = run_hook(context, args[-1])
        with World() as world:
            world["hook_runs"].append(run)
            transcript = context["transcripts"].get(context["session"])
            if result.get("decision") == "block":
                append_rows(transcript, blocked_rest(context["turn"]))
            else:
                append_rows(transcript, answered_rest(context["turn"], args[-1]))
            out, code = apply_send(world, args[-1], result), 0
    print(json.dumps(out))
    return code


def run_hook(context, prompt):
    session = context["session"]
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "turn_id": context["turn"],
               "cwd": context["cwd"], "prompt": prompt, "transcript_path": context["transcripts"].get(session, "")}
    env = dict(os.environ, **context["hook_env"])
    done = subprocess.run([sys.executable, context["script"]], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=30)
    run = {"session": session, "prompt": prompt[:80], "stdout": done.stdout, "stderr": done.stderr[-400:]}
    try:
        return (json.loads(done.stdout) if done.stdout.strip() else {}), run
    except ValueError:
        return {}, run


def apply_send(world, text, result):
    codex = world["terminals"][world["codex"]]
    # Herdr's turn goes up when the status leaves working, also for the blocked prompt's empty turn,
    # after which Codex reports done.
    codex["turn"] += 1
    codex["status_ms"] = int(time.time() * 1000)
    if result.get("decision") == "block":
        codex["status"] = {"app": "codex", "state": "done"}
        world["screen"] = screen_with(world["screen"], text, result.get("reason", "blocked"))
        return {"started": True, "status": "idle", "turn": codex["turn"]}
    codex["status"] = {"app": "codex", "state": "idle"}
    world["screen"] = screen_with(world["screen"], text, "ok")
    world["accepted"].append({"session": world["session"], "prompt": text})
    return {"started": True, "status": "idle", "turn": codex["turn"]}


def handle(world, args):
    command = args[0]
    terminals = world["terminals"]
    if command == "ls":
        return {"terminals": list(terminals.values())}, 0
    if command == "get":
        return terminals[args[1]], 0
    if command == "read":
        address = [a for a in args[1:] if a.startswith("t")][-1]
        if address == world["codex"]:
            return {"complete": True, "text": world["screen"]}, 0
        return {"complete": True, "text": f"{address} screen\nresult: saved in notes/{address}.md\n"}, 0
    codex = terminals[world["codex"]]
    if command == "keys":
        before = {"turn": codex["turn"], "status": codex["status"]["state"]}
        if "".join(args[2:]).startswith("/clear"):
            world["session"] = world["next_session"]
            # `startup_frame`: the fresh chat stays busy without a prompt in the footer until a prompt runs.
            world["screen"] = world.get("startup_frame") or FRESH
            codex["status"] = {"app": "codex", "state": "working" if world.get("startup_frame") else "idle"}
            codex["status_ms"] = int(time.time() * 1000)
        return before, 0
    if command == "kill":
        terminals[args[1]]["state"] = "finished"
        terminals[args[1]]["reason"] = "killed"
        return terminals[args[1]], 0
    return {"error": f"unsupported {command}"}, 2


if __name__ == "__main__":
    sys.exit(main())
