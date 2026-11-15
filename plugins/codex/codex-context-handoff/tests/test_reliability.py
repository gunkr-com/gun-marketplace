"""0.6.0: Herdr worker provenance, bounded recovery, note validation, restore proof and worker review.

Every test runs in a temporary plugin data directory with Herdr faked (`hc_list`, `hc`, `terminal_info`
and `read_screen` patched); nothing here talks to a real Herdr core, Codex home or terminal.
"""

import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import test_context_handoff
from test_context_handoff import (
    FRESH, OLD, OBJECTIVE, FakeClock, event, exec_call, exec_output, goal_line, handoff, hook_prompt, steer, usage,
)

# Shared helpers only: the class itself is not bound here, so its tests run once, in their own module.
shared = test_context_handoff.ContextHandoffTests.__dict__

# The exact shape of a Herdr worker report as Codex 0.167.2 records it in a live chat, with the body
# shortened: a response_item user message with provenance "user" and its UserMessage event.
REPORT_TEXT = ('<hc from="t2c perf-review">R1-B static review written: '
                  'notes/review.md (78 lines). Holding.</hc>')


def report_rows(text=REPORT_TEXT, ident="01a1dd42"):
    return [
        {"type": "response_item", "payload": {
            "type": "message", "id": f"msg_{ident}", "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"], "content_item_metadata": [{"provenance": {"type": "user"}}]},
        }},
        event("item_completed", item={"type": "UserMessage", "id": ident, "client_id": "c",
                                      "content": [{"type": "text", "text": text, "text_elements": []}]}),
    ]


def terminal(address, parent, state="live", status="done", name=None, created=1, pid=None, foreground="codex",
             finished=None):
    entry = {"address": address, "parent": parent, "state": state, "status": {"app": "codex", "state": status},
             "created_ms": created, "pid": pid, "foreground": {"name": foreground}, "cwd": "/w", "labels": {}}
    if name:
        entry["name"] = name
    if finished is not None:
        entry["finished_ms"] = finished
    return entry


def world(*entries):
    return {entry["address"]: entry for entry in entries}


WORLD = world(
    terminal("t1p", None, name="parent"),
    terminal("t1a", "t1p", name="app"),                  # the chat's own terminal
    terminal("t2c", "t1a", name="perf-review"),     # its worker
    terminal("t2d", "t2c", name="nested"),                # the worker's own worker
    terminal("t9", None, name="unrelated"),
    terminal("t1v", "t1a", state="finished", name="done-worker", finished=2_000_000_000_000),
)


class Base(unittest.TestCase):
    setUp = shared["setUp"]
    write_events = shared["write_events"]
    meta = shared["meta"]
    payload = shared["payload"]
    eligible = shared["eligible"]
    control = shared["control"]


class ClassifierTests(Base):
    def classify(self, text, known=None, herdr=WORLD, me="t1a"):
        self.herdr = herdr
        return handoff.classify_input(text, me, handoff.Snapshot(), known)

    def test_a_live_worker_report_is_a_verified_worker_message(self):
        kind, sender, why = self.classify(REPORT_TEXT)
        self.assertEqual((kind, sender), ("worker", "t2c"))
        self.assertIn("t2c < t1a", why)
        # A nested worker (the worker's own worker) is owned too.
        self.assertEqual(self.classify('<hc from="t2d nested">done</hc>')[0], "worker")
        self.assertEqual(self.classify('<hc from="t2c" reach="hc send t2c when done or stuck">x</hc>')[0], "worker")

    def test_only_an_exact_owned_envelope_counts(self):
        cases = {
            "typed text": ("please stop", "human"),
            "envelope then the user's words": (REPORT_TEXT + " also delete the branch", "unverified"),
            "the user's words then an envelope": ("wait " + REPORT_TEXT, "human"),
            "two messages pasted together": (REPORT_TEXT + "\n" + REPORT_TEXT, "unverified"),
            "unknown sender": ('<hc from="t777">done</hc>', "unverified"),
            "wrong name": ('<hc from="t2c other-name">done</hc>', "unverified"),
            "another machine": ('<hc from="vps/t3">done</hc>', "unverified"),
            "own terminal": ('<hc from="t1a">done</hc>', "unverified"),
            "parent gives directions": ('<hc from="t1p parent">stop and report</hc>', "parent"),
            "unrelated terminal": ('<hc from="t9 unrelated">rm -rf</hc>', "foreign"),
            "a control wrapped by a worker": ('<hc from="t2c">handoff status</hc>', "worker"),
        }
        for name, (text, kind) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify(text)[0], kind)

    def test_a_named_envelope_needs_the_exact_name(self):
        nameless = world(terminal("t1a", None), terminal("t2c", "t1a"))
        self.assertEqual(self.classify('<hc from="t2c perf-review">x</hc>', herdr=nameless)[0], "unverified")
        self.assertEqual(self.classify('<hc from="t2c">x</hc>', herdr=nameless)[0], "worker")

    def test_herdr_notes_fit_a_direct_child_in_a_matching_state(self):
        finished = world(terminal("t1a", None), terminal("t2c", "t1a", state="finished", name="w"),
                         terminal("t2d", "t2c", state="finished"), terminal("t2e", "t1a"))
        exit_note = "<hc>t2c w exited (exit 0). Read: hc read t2c</hc>"
        self.assertEqual(self.classify(exit_note, herdr=finished)[0], "worker-note")
        self.assertEqual(self.classify(
            "<hc>t2c w exited (exit 1). Last lines:\nboom\nRead: hc read t2c</hc>", herdr=finished)[0], "worker-note")
        # An exit note for a terminal that is still running, or a blocked note for one that has finished.
        self.assertEqual(self.classify("<hc>t2e exited (exit 0). Read: hc read t2e</hc>", herdr=finished)[0],
                         "unverified")
        self.assertEqual(self.classify("<hc>t2c w is blocked (permission). See: hc read t2c</hc>",
                                       herdr=finished)[0], "unverified")
        self.assertEqual(self.classify("<hc>t2e is blocked (permission). See: hc read t2e</hc>",
                                       herdr=finished)[0], "worker-note")
        # Herdr notes go to the direct parent only: a grandchild's note is not one of ours.
        self.assertEqual(self.classify("<hc>t2d exited (exit 0). Read: hc read t2d</hc>", herdr=finished)[0],
                         "unverified")

    def test_a_removed_child_is_checked_against_its_recorded_entry(self):
        empty = world(terminal("t1a", None))
        note = "<hc>t31 w exited (exit 0). Read: hc read t31</hc>"
        direct = {"t31": {"address": "t31", "parent": "t1a", "name": "w", "state": "live"}}
        nested = {"t31": {"address": "t31", "parent": "t30", "name": "w", "state": "live"}}
        renamed = {"t31": {"address": "t31", "parent": "t1a", "name": "other", "state": "live"}}
        self.assertEqual(self.classify(note, direct, herdr=empty)[0], "worker-note")
        self.assertEqual(self.classify(note, nested, herdr=empty)[0], "unverified")
        self.assertEqual(self.classify(note, renamed, herdr=empty)[0], "unverified")
        self.assertEqual(self.classify(note, None, herdr=empty)[0], "unverified")
        self.assertEqual(self.classify("<hc>t31 w is blocked (auth). See: hc read t31</hc>", direct, herdr=empty)[0],
                         "unverified")

    def test_herdr_unreadable_fails_closed(self):
        with patch.object(handoff, "hc_list", side_effect=OSError("Operation not permitted")):
            kind, _, why = handoff.classify_input(REPORT_TEXT, "t1a", handoff.Snapshot())
        self.assertEqual(kind, "unverified")
        self.assertIn("could not be read", why)

    def test_verdicts_are_cached_per_handoff_and_one_snapshot_per_hook(self):
        state = self.root / "s" / "turn"
        state.parent.mkdir()
        calls = []
        self.herdr = WORLD
        with patch.object(handoff, "hc_list", side_effect=lambda *_, **__: calls.append(1) or WORLD):
            judge = handoff.InputJudge(state, "t1a")
            self.assertEqual(judge.kind(REPORT_TEXT), "worker")
            self.assertEqual(judge.kind('<hc from="t2d nested">x</hc>'), "worker")
            self.assertEqual(len(calls), 1)  # one bulk snapshot for the whole hook run
            self.assertEqual(handoff.InputJudge(state, "t1a").kind(REPORT_TEXT), "worker")
            self.assertEqual(len(calls), 1)  # cached in <state>.inputs.json
        self.assertIn("t2c", state.with_suffix(".inputs.json").read_text())

    def test_herdr_messages_never_authorize_the_transfer_helper(self):
        self.write_events(self.meta, event("task_started", turn_id="t"), *report_rows())
        self.assertFalse(handoff.user_asked(self.transcript, "t"))
        self.write_events(self.meta, event("task_started", turn_id="t"),
                          steer("<hc>t2c exited (exit 0). Read: hc read t2c</hc>"))
        self.assertFalse(handoff.user_asked(self.transcript, "t"))
        self.write_events(self.meta, event("task_started", turn_id="t"), steer("move the goal to a fresh chat"))
        self.assertTrue(handoff.user_asked(self.transcript, "t"))


class GatedInputTests(Base):
    """The tool gate and the Stop capture with worker messages after the arm (the live failure: two worker
    reports after the arm put the handoff off)."""

    def setUp(self):
        Base.setUp(self)
        self.herdr = world(terminal("t1", None), terminal("t2c", "t1", name="perf-review"),
                           terminal("t9", None))

    def append(self, *rows):
        with self.transcript.open("a") as stream:
            stream.writelines(json.dumps(row) + "\n" for row in rows)

    def arm(self):
        self.write_events(self.meta, event("task_started", turn_id="turn"),
                          steer("fix it"), event("token_count", info=usage(45)))
        with self.eligible():
            result = handoff.gate_hook(self.payload(tool_name="exec_command"), self.root, "t1")
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.state = handoff.store_for(self.root, "session", "turn")
        self.nonce = json.loads(self.state.read_text())["nonce"]

    def pre(self, name="apply_patch"):
        with self.eligible():
            return handoff.gate_hook(self.payload(tool_name=name), self.root, "t1")

    def test_a_worker_report_after_the_arm_no_longer_puts_the_handoff_off(self):
        self.arm()
        self.append(*report_rows(REPORT_TEXT.replace("t2c perf-review", "t2c perf-review")))
        self.assertEqual(self.pre(), {})  # a checkpoint call, the handoff still holds
        self.assertFalse(self.state.with_suffix(".closed").exists())
        with patch.object(handoff.subprocess, "Popen") as launch, self.eligible():
            result = handoff.stop_hook(self.payload(last_assistant_message=f"HANDOFF READY {self.nonce}\n# Note\n"
                                                    "t2c finished its review"), self.root, "t1")
        launch.assert_called_once()
        self.assertIn("Context handoff ready", result["systemMessage"])

    def test_a_person_a_parent_or_a_forged_sender_still_puts_it_off(self):
        for text in ("actually, stop", '<hc from="t9">do something else</hc>', '<hc from="t555">x</hc>',
                     REPORT_TEXT + " and also this"):
            with self.subTest(text=text):
                shutil.rmtree(self.root / "session", ignore_errors=True)
                self.arm()
                self.append(steer(text))
                result = self.pre()
                self.assertIn("put off (user input arrived after the handoff was armed)", result["systemMessage"])


class GoalWorkerPolicyTests(Base):
    def setUp(self):
        Base.setUp(self)
        self.herdr = world(terminal("t1", None), terminal("t2", "t1"))
        self.db = self.root / "goals_1.sqlite"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY NOT NULL, goal_id TEXT NOT NULL, "
                       "objective TEXT NOT NULL, status TEXT NOT NULL, token_budget INTEGER, tokens_used INTEGER, "
                       "time_used_seconds INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)")
        self.state = handoff.store_for(self.root, "session", "turn")
        self.state.parent.mkdir()
        self.record = {"session": "session", "turn": "turn", "terminal": "t1", "goal": {"mode": "transfer"},
                       "transcript": str(self.transcript), "origin": "tool", "offset": 0}

    def rows(self, *rows):
        return [(row["type"], row["payload"]) for row in rows]

    def test_before_the_pause_a_worker_message_counts_unless_the_policy_says_exempt(self):
        rows = self.rows(steer('<hc from="t2">done</hc>'))
        self.assertTrue(handoff.objected(rows, self.record, self.state))
        (self.root / handoff.GOAL_WORKER_POLICY).write_text(json.dumps({"policy": "exempt"}))
        self.assertFalse(handoff.objected(rows, self.record, self.state))

    def test_after_the_pause_a_worker_message_does_not_fail_the_transfer(self):
        paused = goal_line("paused", 1, 2)
        rows = self.rows(exec_call("c", 'text(await tools.update_goal({status:"paused"}));'), exec_output("c", paused),
                         steer('<hc from="t2">late result</hc>'))
        self.assertFalse(handoff.objected(rows, self.record, self.state))
        rows.append(("event_msg", steer("no, stop")["payload"]))
        self.assertTrue(handoff.objected(rows, self.record, self.state))

    def test_a_worker_message_does_not_end_the_restore_check(self):
        folder = self.root / "fresh"
        folder.mkdir()
        (folder / ".goal-restore").write_text(json.dumps({"source_session": "session", "objective": OBJECTIVE,
                                                          "remaining": None, "objective_sha256": "x"}))
        payload = {"session_id": "fresh", "prompt": '<hc from="t2">done</hc>', "transcript_path": str(self.transcript)}
        self.assertIsNone(handoff.cancel_restore(payload, self.root, "t1"))
        self.assertTrue((folder / ".goal-restore").exists())
        self.assertIn("stays paused", handoff.cancel_restore(dict(payload, prompt="hello"), self.root, "t1"))


class FooterTests(Base):
    """The narrow-terminal footer fix (a narrow pane wraps the status line under the empty prompt)."""
    fresh_wait = shared["fresh_wait"]
    armed = shared["armed"]
    faked = shared["faked"]

    STATUS = "\n\n  gpt-5.5 high · 40% context left · ~/Documents/Long Folder/My\n  Drive/projects/app\n  ? for shortcuts"

    def test_wrapped_footer_in_a_narrow_terminal_is_an_empty_prompt(self):
        wrapped_old = "› fix the bug\n• Fixed most of it.\n\n› Ask Codex to do anything" + self.STATUS
        wrapped_fresh = "  >_ OpenAI Codex (v0.167.2)\n\n› Ask Codex to do anything" + self.STATUS
        deeper = wrapped_fresh.replace("  ? for shortcuts", "  more status\n  ? for shortcuts")
        for screen in (wrapped_old, wrapped_fresh, deeper):
            self.assertEqual(handoff.footer_prompt(screen)[0], handoff.PLACEHOLDER)
            self.assertEqual(handoff.composer_text(screen), "")
            with patch.object(handoff, "hc", return_value={"complete": True, "text": screen}):
                self.assertEqual(handoff.read_composer("t1"), screen)
        self.assertEqual(handoff.prompt_state(wrapped_fresh, OLD), "fresh")
        self.assertEqual(handoff.prompt_state(wrapped_old, OLD), "old history")
        self.assertEqual(self.fresh_wait((0, "working", 5, wrapped_fresh), (20, "idle", 6, wrapped_fresh))[0], 6)

    def test_wrapped_footer_does_not_hide_drafts_transcripts_or_dialogs(self):
        empty = "› Ask Codex to do anything" + self.STATUS
        not_empty = {
            "bare prompt": "  >_ OpenAI Codex\n\n›" + self.STATUS,
            "draft starting with blank lines": "  >_ OpenAI Codex\n\n›\n\n  hello\n\n  gpt · ~/work\n  ? for shortcuts",
            "one-line draft": empty.replace("› Ask Codex to do anything", "› hel"),
            "multi-line draft": empty.replace("› Ask Codex to do anything", "› first\n  second"),
            "blank gap beneath": empty.replace("  Drive/", "\n  Drive/"),
            "transcript beneath": empty.replace("  Drive/projects/app", "• Working on it."),
            "dialog beneath": "› Ask Codex to do anything\n\n• Ran tests\n\n  Allow command?\n  › 1. Yes\n    2. No",
            "dialog without blank": "› Ask Codex to do anything\n\n  Allow command?\n› 1. Yes\n  2. No\n  esc to cancel",
            "boxed dialog": "› Ask Codex to do anything\n\n╭──────╮\n│ Allow? │\n╰──────╯\n  esc to cancel",
            "box border only": "› Ask Codex to do anything\n\n  status\n│ Allow command │\n  more",
            "too far up": "› Ask Codex to do anything\n\n" + "\n".join(f"  status {n}" for n in range(8)),
        }
        for name, screen in not_empty.items():
            with self.subTest(name):
                self.assertNotEqual(handoff.composer_text(screen), "")
                with patch.object(handoff, "hc", return_value={"complete": True, "text": screen}):
                    self.assertIsNone(handoff.read_composer("t1"))
                self.assertNotEqual(handoff.prompt_state(screen, OLD), "fresh")
        # A blank line made of spaces beneath the placeholder is still the status gap.
        spaced = "› Ask Codex to do anything\n   \n  gpt-5.5 · ~/Documents/Long Folder/My\n  Drive/x\n  ? for shortcuts"
        self.assertEqual(handoff.composer_text(spaced), "")


class NoteTests(Base):
    def test_validate_note(self):
        nonce = "a" * 24
        good = f"HANDOFF READY {nonce}\n# Note\nnext"
        self.assertEqual(handoff.validate_note(good, nonce), ("# Note\nnext\n", None))
        self.assertEqual(handoff.validate_note(f"HANDOFF READY {nonce} GOAL transferred\nx", nonce)[0], "x\n")
        cases = {
            "": "missing",
            "All done.": "missing",
            f"HANDOFF READY {nonce}": "empty",
            f"HANDOFF READY {nonce}\n   ": "empty",
            f"HANDOFF READY {nonce}\n" + "x" * (handoff.MAX_NOTE + 1): "characters, more than",
            f"Summary first\nHANDOFF READY {nonce}\nnote": "not the first line",
            f"HANDOFF READY {'b' * 24}\nnote": "another handoff's nonce",
            "HANDOFF READY\nnote": "malformed",
            f"HANDOFF READY {nonce}\nnote\nHANDOFF READY {nonce}": "more than once",
        }
        for answer, why in cases.items():
            with self.subTest(answer=answer[:30]):
                note, reason = handoff.validate_note(answer, nonce)
                self.assertIsNone(note)
                self.assertIn(why, reason)

    def stop_armed(self):
        self.write_events(self.meta, event("task_started", turn_id="turn"), event("token_count", info=usage(45)))
        with self.eligible():
            request = handoff.stop_hook(self.payload(), self.root, "t1")
        self.state = handoff.store_for(self.root, "session", "turn")
        self.nonce = json.loads(self.state.read_text())["nonce"]
        with self.transcript.open("a") as stream:
            for row in hook_prompt(request["reason"]):
                stream.write(json.dumps(row) + "\n")

    def stop(self, answer):
        with patch.object(handoff.subprocess, "Popen") as launch, self.eligible():
            return handoff.stop_hook(self.payload(last_assistant_message=answer), self.root, "t1"), launch

    def test_a_malformed_answer_is_asked_for_once_per_reason_within_the_budget(self):
        self.stop_armed()
        again, launch = self.stop(f"Done.\nHANDOFF READY {self.nonce}\n# Note")
        self.assertEqual(again["decision"], "block")
        self.assertIn("not the first line", again["reason"])
        self.assertIn(f"HANDOFF READY {self.nonce}", again["reason"])
        again, _ = self.stop(f"HANDOFF READY {self.nonce}\n" + "x" * (handoff.MAX_NOTE + 5))
        self.assertIn("more than", again["reason"])
        # Three attempts in all: the third malformed answer closes visibly.
        final, launch = self.stop(f"HANDOFF READY {'c' * 24}\nnote")
        launch.assert_not_called()
        self.assertIn("no valid handoff note (the marker line has another handoff's nonce)", final["systemMessage"])
        ledger = handoff.read_ledger(self.state)
        self.assertEqual([a["kind"] for a in ledger["attempts"]], ["request", "note", "note"])

    def test_a_valid_answer_after_a_re_prompt_switches_and_a_stop_decline_stays_silent(self):
        self.stop_armed()
        self.stop("HANDOFF READY\n# Note")
        result, launch = self.stop(f"HANDOFF READY {self.nonce}\n# Note\nnext")
        launch.assert_called_once()
        note = self.state.with_suffix(".md").read_text()
        self.assertTrue(note.startswith("# Note\nnext\n\n## Restore checklist"))
        secret = json.loads(self.state.with_suffix(".restore.json").read_text())["secret"]
        self.assertIn(f"Read token (revision 0): {handoff.read_token(secret, 0)}", note)
        self.assertIn("note-valid", handoff.read_ledger(self.state)["stages"])
        self.assertIn("submit `handoff status` if no fresh chat appears", result["systemMessage"])
        # The model's plain answer to a Stop request (no marker at all) is a decline, as before.
        shutil.rmtree(self.root / "session")
        self.stop_armed()
        final, launch = self.stop("Everything is complete.")
        self.assertEqual(final, {})
        launch.assert_not_called()


def worker_payload(session, text=REPORT_TEXT, turn="w1"):
    return {"session_id": session, "turn_id": turn, "cwd": "/w", "prompt": text}


class Term:
    """A scripted Herdr view of the one Codex: status, turn, screen and pid can change at any time."""

    def __init__(self, clock, status="done", turn=5, screen=OLD):
        self.clock, self.status, self.turn, self.screen, self.pid = clock, status, turn, screen, 123
        self.since = 0

    def info(self, terminal, cwd, pid=None):
        if self.pid is None or (pid is not None and pid != self.pid):
            return None
        return {"status": {"app": "codex", "state": self.status}, "turn": self.turn,
                "status_ms": self.since, "foreground": {"pid": self.pid}}

    def read(self, _):
        return self.screen

    def set(self, **fields):
        if "status" in fields and fields["status"] != self.status:
            self.since = int(self.clock.now * 1000)
        for key, value in fields.items():
            setattr(self, key, value)


HELD_OLD = OLD.replace("\n› Ask Codex to do anything", "\n› " + REPORT_TEXT[:40] + "\n\n• held\n\n› Ask Codex to do anything")
HELD_FRESH = FRESH.replace("\n› Ask Codex to do anything", "\n› " + REPORT_TEXT[:40] + "\n\n• held\n\n› Ask Codex to do anything")


class SwitchTests(Base):
    """The whole detached switch against a scripted terminal, with the ledger and its retries."""

    def setUp(self):
        Base.setUp(self)
        self.herdr = world(terminal("t1", None), terminal("t2c", "t1", name="perf-review", status="done"))
        self.clock = FakeClock()
        self.term = Term(self.clock)
        self.sends, self.keys, self.on_keys, self.on_send = [], [], None, None
        self.write_events(self.meta, event("task_started", turn_id="turn"), event("token_count", info=usage(41)),
                          event("task_complete", turn_id="turn"))
        self.state = handoff.store_for(self.root, "session", "turn")
        self.state.parent.mkdir()
        self.record = {"session": "session", "turn": "turn", "cwd": str(self.root), "terminal": "t1", "pid": 123,
                       "transcript": str(self.transcript), "nonce": "a" * 24, "usage": 41.0, "offset": 0}
        self.state.write_text(json.dumps(self.record))
        self.secret = "s" * 32
        self.state.with_suffix(".restore.json").write_text(json.dumps({"secret": self.secret}))
        self.state.with_suffix(".md").write_text("note\n" + handoff.restore_checklist(self.state, self.secret, False))
        handoff.arm_guard(self.state, self.record)
        (self.root / "session" / ".prompt-hook").write_text("123\n")
        environment = patch.dict(os.environ, {"CODEX_CONTEXT_HANDOFF_GRACE": "2"})
        environment.start()
        self.addCleanup(environment.stop)

    def deliver(self, prompt, session="fresh"):
        payload = {"session_id": session, "turn_id": "first", "cwd": str(self.root), "prompt": prompt}
        notice, held = handoff.mark_takeover(payload, self.root, "t1")
        if held:
            return {"decision": "block", "reason": held}
        return handoff.prompt_hook(payload, self.root, "t1")

    def fake_hc(self, *args, timeout=8):
        if args[0] == "keys":
            self.keys.append(self.clock.now)
            if self.on_keys:
                return self.on_keys()
            before = self.term.turn
            self.term.set(screen=FRESH, status="working")
            self.clock.at(1, lambda: self.term.set(status="idle", turn=self.term.turn + 1))
            return {"turn": before, "status": "done"}
        if args[0] == "send":
            self.sends.append(args[-1])
            if args[-1].startswith("Codex Context Handoff notice"):
                self.notice_result = handoff.notice_hook({"prompt": args[-1]}, self.root, "t1")
                return {"started": False, "status": "idle", "turn": self.term.turn}
            if self.on_send:
                return self.on_send(args[-1])
            self.delivered = self.deliver(args[-1])
            return {"started": True, "status": "idle", "turn": self.term.turn}
        if args[0] == "read":
            return {"complete": True, "text": "worker screen"}
        raise AssertionError(args)

    def run_switch(self):
        with patch.object(handoff.time, "monotonic", self.clock.monotonic), \
                patch.object(handoff.time, "sleep", self.clock.sleep), \
                patch.object(handoff, "terminal_info", self.term.info), \
                patch.object(handoff, "read_screen", self.term.read), \
                patch.object(handoff, "hc", side_effect=self.fake_hc):
            handoff.switch_chat(self.state)

    def ledger(self):
        return handoff.read_ledger(self.state)

    def test_clean_switch_records_every_stage_and_the_manifest(self):
        self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        stages = list(self.ledger()["stages"])
        for stage in ("switch-started", "old-idle", "grace-held", "cleared", "fresh-ready", "continuation-sent",
                      "delivered"):
            self.assertIn(stage, stages)
        self.assertEqual(len(self.ledger()["attempts"]), 1)
        manifest = json.loads(self.state.with_suffix(".workers.json").read_text())
        self.assertEqual([w["address"] for w in manifest["workers"]], ["t2c"])
        self.assertIn("- t2c perf-review: live/done, parent t1", self.state.with_suffix(".md").read_text())
        self.assertFalse((self.root / "t1.switch").exists())

    def test_worker_message_in_the_grace_window_is_held_then_the_switch_retries(self):
        def worker_arrives():
            result = self.deliver(REPORT_TEXT, session="session")
            self.assertEqual(result["decision"], "block")
            self.assertIn("held for the fresh chat (held message 1", result["reason"])
            self.term.set(screen=HELD_OLD, turn=6)
        self.clock.at(1.2, worker_arrives)  # inside the 2 s grace that follows the idle check
        self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual(len(self.keys), 1)  # /clear once
        continuation = handoff.continuation(self.record, self.state)
        self.assertEqual([s for s in self.sends if s.startswith("Codex Context Handoff:")], [continuation])
        attempts = self.ledger()["attempts"]
        self.assertEqual([a["kind"] for a in attempts], ["request", "switch"])
        self.assertEqual(attempts[1]["phase"], "pre-clear")
        note = self.state.with_suffix(".md").read_text()
        self.assertIn("## Held worker message 1 (from t2c", note)
        self.assertIn(REPORT_TEXT, note)
        self.assertIn(f"Read token (revision 1): {handoff.read_token(self.secret, 1)}", note)
        held = handoff.held_entries(self.state)
        self.assertEqual((held[0]["from"], held[0]["text"]), ("t2c", REPORT_TEXT))
        # The same message delivered twice is kept once.
        self.state.with_suffix(".received").unlink()
        handoff.arm_guard(self.state, self.record)
        self.deliver(REPORT_TEXT, session="session")
        self.assertEqual(handoff.held_count(self.state), 1)

    def test_worker_message_after_clear_is_held_and_the_same_fresh_chat_is_used(self):
        def keys():
            self.term.set(screen=FRESH, status="working")
            def worker():
                self.assertEqual(self.deliver(REPORT_TEXT, session="fresh")["decision"], "block")
                self.term.set(screen=HELD_FRESH, status="working")
                self.clock.at(0.6, lambda: self.term.set(status="idle", turn=7))
            self.clock.at(1, lambda: self.term.set(status="idle", turn=6))
            self.clock.at(2, worker)
            return {"turn": 5, "status": "done"}
        self.on_keys = keys
        self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual(len(self.keys), 1)  # no second /clear
        attempts = self.ledger()["attempts"]
        self.assertEqual([a.get("phase") for a in attempts], [None, "post-clear"])
        self.assertEqual(handoff.held_entries(self.state)[0]["session"], "fresh")

    def test_a_person_in_the_grace_window_cancels_without_any_retry(self):
        def person():
            self.deliver("wait, not yet", session="session")
            self.term.set(status="working")
        self.clock.at(1.2, person)
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.run_switch()
        self.assertEqual(self.keys, [])
        self.assertEqual(len(self.ledger()["attempts"]), 1)
        self.assertFalse((self.root / "session" / ".switch-failed").exists())  # a person's choice, not a failure
        self.assertFalse(any(s.startswith("Codex Context Handoff notice") for s in self.sends))

    def test_an_old_chat_that_never_settles_is_retried_then_exhausted_visibly(self):
        self.term.set(status="working")  # never idle: every attempt times out at the idle check
        with self.assertRaisesRegex(RuntimeError, "All 3 attempts are used"):
            self.run_switch()
        self.assertEqual(self.keys, [])
        attempts = self.ledger()["attempts"]
        self.assertEqual([a["kind"] for a in attempts], ["request", "switch", "switch"])
        self.assertEqual(attempts[-1]["outcome"], "failed")
        self.assertIn("All 3 attempts", self.state.with_suffix(".closed").read_text())
        self.assertTrue((self.root / "session" / ".switch-failed").is_file())
        # Still working, so the notice waits for the next hook instead of being typed in.
        self.assertFalse(any(s.startswith("Codex Context Handoff notice") for s in self.sends))
        with self.eligible():
            result = handoff.with_notice({}, handoff.pop_notice(self.root, "t1"))
        self.assertIn("Context handoff did not complete", result["systemMessage"])
        self.assertIn("attempt 3 of 3", result["systemMessage"])

    def test_exhausted_at_an_idle_prompt_shows_itself_without_any_input(self):
        def never_fresh():
            self.term.set(screen=FRESH.replace("› Ask Codex to do anything", "›"), status="idle")
            return {"turn": 5, "status": "done"}
        self.on_keys = never_fresh  # a bare prompt never proves an empty composer
        with self.assertRaises(RuntimeError):
            self.run_switch()
        self.assertEqual(len(self.keys), 1)
        # The bare prompt is not a clean fresh chat either, so no notice is typed: it waits for a hook.
        self.assertFalse(any(s.startswith("Codex Context Handoff notice") for s in self.sends))
        # Before /clear at an idle, empty prompt the notice is typed once and its own hook answers it.
        shutil.rmtree(self.root / "session")
        self.setUp_again()
        self.term.set(status="idle")
        with patch.object(handoff, "await_old_chat", side_effect=handoff.Retryable("not idle", "pre-clear")):
            with self.assertRaisesRegex(RuntimeError, "All 3 attempts"):
                self.run_switch()
        notices = [s for s in self.sends if s.startswith("Codex Context Handoff notice")]
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.notice_result["decision"], "block")
        self.assertIn("Context handoff did not complete: not idle", self.notice_result["reason"])
        self.assertFalse((self.root / "t1.notice").exists())  # shown once
        self.assertTrue(self.state.with_suffix(".notified").is_file())
        # Typed again by mistake, the notice prompt is still blocked and never reaches the model.
        again = handoff.notice_hook({"prompt": notices[0]}, self.root, "t1")
        self.assertEqual(again["decision"], "block")

    def setUp_again(self):
        self.sends, self.keys = [], []
        self.state.parent.mkdir()
        self.state.write_text(json.dumps(self.record))
        self.state.with_suffix(".restore.json").write_text(json.dumps({"secret": self.secret}))
        self.state.with_suffix(".md").write_text("note\n")
        handoff.arm_guard(self.state, self.record)
        (self.root / "session" / ".prompt-hook").write_text("123\n")

    def test_no_prompt_hook_evidence_means_no_typed_notice(self):
        (self.root / "session" / ".prompt-hook").unlink()
        self.term.set(status="idle")
        with patch.object(handoff, "await_old_chat", side_effect=handoff.Retryable("not idle", "pre-clear")):
            with self.assertRaises(RuntimeError):
                self.run_switch()
        self.assertEqual(self.sends, [])
        self.assertTrue((self.root / "t1.notice").is_file())

    def test_a_continuation_that_provably_never_arrived_is_sent_again(self):
        def lost(text):
            self.on_send = None  # the second send is delivered
            return {"started": False, "status": "idle", "turn": self.term.turn}
        self.on_send = lost
        self.run_switch()
        continuations = [s for s in self.sends if s.startswith("Codex Context Handoff:")]
        self.assertEqual(len(continuations), 2)
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual(len(self.keys), 1)
        self.assertEqual([a.get("phase") for a in self.ledger()["attempts"]], [None, "post-clear"])

    def test_a_continuation_that_may_have_arrived_is_never_sent_again(self):
        def moved(text):
            self.term.set(turn=self.term.turn + 1, status="working")  # something ran, no receipt
            return {"started": True, "status": "idle", "turn": self.term.turn}
        self.on_send = moved
        with self.assertRaisesRegex(RuntimeError, "may have arrived.*Herdr's turn moved"):
            self.run_switch()
        self.assertEqual(len([s for s in self.sends if s.startswith("Codex Context Handoff:")]), 1)

    def test_a_continuation_left_in_the_composer_is_never_sent_again(self):
        def pasted(text):
            self.term.set(screen=FRESH.replace("› Ask Codex to do anything", "› " + text[:60]))
            return {"started": False, "status": "idle", "turn": self.term.turn}
        self.on_send = pasted
        with self.assertRaisesRegex(RuntimeError, "left unsubmitted in the composer"):
            self.run_switch()
        self.assertEqual(len([s for s in self.sends if s.startswith("Codex Context Handoff:")]), 1)

    def test_a_clear_that_failed_before_typing_is_retried_and_one_that_typed_continues(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise handoff.subprocess.TimeoutExpired("hc", 8)  # nothing typed: screen and turn unchanged
            self.term.set(screen=FRESH, status="working")
            self.clock.at(1, lambda: self.term.set(status="idle", turn=6))
            return {"turn": 5, "status": "done"}
        self.on_keys = flaky
        self.run_switch()
        self.assertEqual(len(calls), 2)
        self.assertEqual([a.get("phase") for a in self.ledger()["attempts"]], [None, "pre-clear"])
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")

    def test_a_clear_whose_outcome_is_unknown_is_final(self):
        def half():
            self.term.set(screen=OLD.replace("› Ask Codex to do anything", "› /cle"))
            raise handoff.subprocess.TimeoutExpired("hc", 8)
        self.on_keys = half
        with self.assertRaisesRegex(RuntimeError, "Could not tell whether /clear was typed"):
            self.run_switch()
        self.assertEqual(len(self.ledger()["attempts"]), 1)

    def test_delivery_evidence_ignores_the_source_transcript(self):
        sessions = self.root / "home" / "sessions" / "2026" / "11" / "14"
        sessions.mkdir(parents=True)
        source = sessions / "rollout-2026-11-14T00-00-00-session.jsonl"
        prefix = f"Codex Context Handoff: session turn {'a' * 24}"
        meta = lambda sid: {"type": "session_meta", "payload": {"id": sid, "cwd": str(self.root)}}
        user = lambda text: event("item_completed", item={"type": "UserMessage", "content": [{"type": "text", "text": text}]})
        source.write_text("\n".join(json.dumps(r) for r in (meta("session"), user(prefix + " ..."))) + "\n")
        record = dict(self.record, transcript=str(source))
        self.assertEqual(handoff.continuation_rollouts(record, 0), [])
        other = sessions / "rollout-2026-11-14T00-01-00-other.jsonl"
        other.write_text("\n".join(json.dumps(r) for r in (meta("other"), user("quoting " + prefix))) + "\n")
        self.assertEqual(handoff.continuation_rollouts(record, 0), [])  # mentions it, does not start with it
        fresh = sessions / "rollout-2026-11-14T00-02-00-fresh.jsonl"
        fresh.write_text("\n".join(json.dumps(r) for r in (meta("fresh"), user(prefix + " Read ..."))) + "\n")
        self.assertEqual(handoff.continuation_rollouts(record, 0), [str(fresh)])
        elsewhere = dict(record, cwd="/somewhere/else")
        self.assertEqual(handoff.continuation_rollouts(elsewhere, 0), [])

    def test_after_the_receipt_worker_messages_reach_the_fresh_chat(self):
        self.run_switch()
        self.assertFalse((self.root / "t1.switch").exists())
        notice, held = handoff.mark_takeover(worker_payload("fresh"), self.root, "t1")
        self.assertEqual((notice, held), (None, None))
        self.assertEqual(handoff.held_count(self.state), 0)

    def test_a_failed_switch_holds_off_new_automatic_handoffs_until_more_context_is_used(self):
        folder = self.root / "session"
        (folder / ".switch-failed").write_text(json.dumps({"usage": 41.0}))
        self.assertTrue(handoff.failed_recently(folder, 45.0))
        self.assertFalse(handoff.failed_recently(folder, 46.5))
        self.write_events(self.meta, event("task_started", turn_id="next"), event("token_count", info=usage(44)))
        with self.eligible():
            self.assertEqual(handoff.stop_hook(self.payload(turn_id="next"), self.root, "t1"), {})


class RestoreTests(Base):
    def setUp(self):
        Base.setUp(self)
        self.state = handoff.store_for(self.root, "session", "turn")
        self.state.parent.mkdir()
        self.record = {"session": "session", "turn": "turn", "terminal": "t1", "nonce": "a" * 24, "cwd": "/w"}
        self.state.write_text(json.dumps(self.record))
        self.secret = "s" * 32
        self.state.with_suffix(".restore.json").write_text(json.dumps({"secret": self.secret}))
        self.state.with_suffix(".md").write_text("note\n")
        self.folder = self.root / "fresh"
        self.folder.mkdir()
        (self.folder / ".received-from").write_text("session turn\n")
        self.state.with_suffix(".received").write_text("fresh\n")
        self.ctx = {"root": self.root, "session": "fresh", "terminal": "t1", "folder": self.folder,
                    "state": self.state, "record": self.record}

    def stop(self):
        return handoff.restoration_check({}, self.root, "fresh")

    def test_the_token_must_be_the_latest_revision(self):
        with open(self.state.with_suffix(".held.jsonl"), "w") as stream:
            stream.write(json.dumps({"revision": 1, "text": "x"}) + "\n")
        with self.assertRaisesRegex(handoff.Refusal, "from revision 0; 1 held worker message"):
            handoff.confirm_restored(self.ctx, handoff.read_token(self.secret, 0))
        with self.assertRaisesRegex(handoff.Refusal, "does not match"):
            handoff.confirm_restored(self.ctx, "nope")
        result = handoff.confirm_restored(self.ctx, handoff.read_token(self.secret, 1))
        self.assertEqual(result["note_revision"], 1)
        self.assertTrue(self.state.with_suffix(".restored").is_file())
        self.assertIn("restored", handoff.read_ledger(self.state)["stages"])
        self.assertIsNone(self.stop())

    def test_restored_needs_the_worker_review_when_there_were_workers(self):
        self.state.with_suffix(".workers.json").write_text(json.dumps({"workers": [{"address": "t2", "parent": "t1"}]}))
        with self.assertRaisesRegex(handoff.Refusal, "review the Herdr workers"):
            handoff.confirm_restored(self.ctx, handoff.read_token(self.secret, 0))
        self.state.with_suffix(".workers-report.json").write_text("{}")
        self.assertTrue(handoff.confirm_restored(self.ctx, handoff.read_token(self.secret, 0))["restored"])

    def test_stop_reminds_once_then_reports_once(self):
        first = self.stop()
        self.assertEqual(first["decision"], "block")
        self.assertIn("restored --token", first["reason"])
        self.assertIn("Context handoff: this chat did not confirm", self.stop()["systemMessage"])
        self.assertIsNone(self.stop())

    def test_create_goal_comes_before_the_checklist(self):
        (self.folder / ".goal-restore").write_text("{}")
        self.assertIsNone(self.stop())  # restore_check reminds about the Goal first

    def test_a_person_ends_the_reminder_but_a_worker_message_does_not(self):
        self.herdr = world(terminal("t1", None), terminal("t2", "t1"))
        worker = {"session_id": "fresh", "prompt": '<hc from="t2">done</hc>'}
        self.assertIsNone(handoff.end_restoration_wait(worker, self.root, "t1"))
        self.assertIn("no longer reminds", handoff.end_restoration_wait(dict(worker, prompt="go on"), self.root, "t1"))
        self.assertIsNone(self.stop())

    def test_the_checklist_names_the_installed_launcher_and_a_non_login_shell(self):
        checklist = handoff.restore_checklist(self.state, self.secret, False)
        launcher = str(Path(handoff.__file__).resolve().parents[1] / "bin" / "codex")
        self.assertIn(handoff.shlex.quote(launcher) + " context-handoff workers", checklist)
        self.assertIn("login: false", checklist)
        self.assertIn("login: false", handoff.continuation({"session": "s", "turn": "t", "nonce": "a" * 24}, self.state))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "fresh"}):
            os.environ.pop("HC_TERMINAL_ID", None)
            with self.assertRaisesRegex(handoff.Refusal, "HC_TERMINAL_ID is missing.*login: false"):
                handoff.helper_context(self.root)

    def test_the_helpers_refuse_before_the_transferred_goal_is_recreated(self):
        transcript = self.root / "home" / "sessions" / "2026" / "11" / "14" / "rollout-x-fresh.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "fresh", "session_id": "fresh", "cwd": "/w", "source": "cli", "thread_source": "user"}}) + "\n")
        (self.folder / ".goal-restore").write_text(json.dumps({"source_session": "session", "objective": OBJECTIVE,
                                                               "remaining": None, "objective_sha256": "x"}))
        (self.folder / ".hook-script").write_text(handoff.hook_script_record(123))
        env = {"CODEX_THREAD_ID": "fresh", "HC_TERMINAL_ID": "t1", "CODEX_HOME": str(self.root / "home")}
        with patch.dict(os.environ, env), self.eligible():
            with self.assertRaisesRegex(handoff.Refusal, "call create_goal"):
                handoff.helper_context(self.root)
            self.assertEqual(handoff.helper_context(self.root, need_goal=False)["session"], "fresh")
            (self.folder / ".goal-restore").unlink()
            self.assertEqual(handoff.helper_context(self.root)["state"], self.state)
            (self.folder / ".hook-script").write_text("/old/hooks/context_handoff.py\n999\n")
            with self.assertRaisesRegex(handoff.Refusal, "has not run this version"):
                handoff.helper_context(self.root)
        with patch.dict(os.environ, dict(env, HC_TERMINAL_ID="t7")), self.eligible():
            (self.folder / ".hook-script").write_text(handoff.hook_script_record(123))
            with self.assertRaisesRegex(handoff.Refusal, "another Herdr terminal"):
                handoff.helper_context(self.root)


class WorkerReviewTests(Base):
    def setUp(self):
        Base.setUp(self)
        self.state = handoff.store_for(self.root, "session", "turn")
        self.state.parent.mkdir()
        self.record = {"session": "session", "turn": "turn", "terminal": "t1", "transcript": str(self.transcript)}
        self.herdr = world(
            terminal("t0", None), terminal("t1", "t0"),                       # t0 is the chat's own parent
            terminal("t2", "t1", status="done", pid=20, name="reviewer"),
            terminal("t3", "t1", status="working", pid=30),
            terminal("t4", "t3", status="done", pid=40),                      # a nested worker
            terminal("t5", "t1", state="finished", finished=2_000_000_000_000),
            terminal("t6", "t1", state="finished", finished=1),               # finished long before this chat
            terminal("t7", "t0"), terminal("t8", None),                       # a sibling and an unrelated terminal
        )
        self.write_events({"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
                           "payload": {"id": "session"}})
        self.ctx = {"root": self.root, "session": "fresh", "terminal": "t1", "folder": self.root / "fresh",
                    "state": self.state, "record": self.record}
        self.killed = []
        self.state.with_suffix(".md").write_text("note\n")

    def fake_hc(self, *args, timeout=8):
        if args[0] == "read":
            return {"complete": True, "text": f"screen of {args[-1]}\nresult: ok\n"}
        if args[0] == "kill":
            self.killed.append(args[-1])
            self.herdr[args[-1]] = dict(self.herdr[args[-1]], state="finished")
            return dict(self.herdr[args[-1]], reason="killed")
        raise AssertionError(args)

    def capture(self):
        with patch.object(handoff, "hc", side_effect=self.fake_hc):
            return handoff.capture_manifest(self.record, self.state, into_note=True)

    def call(self, function, *args):
        with patch.object(handoff, "hc", side_effect=self.fake_hc):
            return function(self.ctx, *args)

    def test_the_manifest_holds_only_owned_workers(self):
        workers = self.capture()
        self.assertEqual([w["address"] for w in workers], ["t2", "t3", "t4", "t5"])
        nested = next(w for w in workers if w["address"] == "t4")
        self.assertEqual((nested["parent"], nested["depth"], nested["lineage"]), ("t3", 2, ["t4", "t3"]))
        self.assertIn("## Herdr workers of t1 at handoff", self.state.with_suffix(".md").read_text())

    def test_inspect_saves_evidence_and_reports_gone_stale_and_new_workers(self):
        self.capture()
        self.herdr["t2"] = dict(self.herdr["t2"], created_ms=99)   # another terminal under t2's address now
        del self.herdr["t5"]                                        # removed from Herdr
        self.herdr["t9"] = terminal("t9", "t1")                     # started after the handoff
        result = self.call(handoff.workers_inspect)
        now = {row["address"]: row["now"] for row in result["workers"]}
        self.assertEqual(now, {"t2": "stale", "t3": "live", "t4": "live", "t5": "gone"})
        self.assertEqual(result["new_workers_not_in_review"], ["t9"])
        self.assertEqual(result["undecided"], ["t3", "t4"])
        evidence = next(row for row in result["workers"] if row["address"] == "t3")["evidence"]
        self.assertIn("screen of t3", Path(evidence["path"]).read_text())

    def test_close_is_guarded_and_idempotent(self):
        self.capture()
        self.call(handoff.workers_inspect)
        decide = lambda *a, **k: self.call(handoff.workers_decide, *a, k.get("cancel", False))
        with self.assertRaisesRegex(handoff.Refusal, "--result is required"):
            decide("t2", "close", "obsolete", None)
        with self.assertRaisesRegex(handoff.Refusal, "not in this handoff's worker manifest"):
            decide("t7", "close", "x", "r")
        with self.assertRaisesRegex(handoff.Refusal, "t3 is working, so it is kept"):
            decide("t3", "close", "obsolete", "notes.md")
        with self.assertRaisesRegex(handoff.Refusal, "has live workers of its own \\(t4\\)"):
            decide("t3", "close", "obsolete", "notes.md", cancel=True)
        self.assertEqual(self.killed, [])
        closed = decide("t4", "close", "its review is in the note", "the note, Held worker message 1")
        self.assertEqual(closed["decision"]["outcome"], "closed")
        self.assertIn("before-close", closed["decision"]["evidence"]["path"])
        self.assertEqual(self.killed, ["t4"])
        again = decide("t4", "close", "again", "x")
        self.assertTrue(again["repeat"])
        self.assertEqual(self.killed, ["t4"])  # never killed twice
        self.assertEqual(decide("t3", "close", "cancelled after review", "notes.md", cancel=True)["decision"]["outcome"],
                         "closed")
        self.assertEqual(decide("t5", "close", "finished", "its log")["decision"]["outcome"], "already-finished")
        self.assertEqual(self.killed, ["t4", "t3"])

    def test_close_refuses_a_worker_that_changed(self):
        self.capture()
        self.call(handoff.workers_inspect)
        self.herdr["t2"] = dict(self.herdr["t2"], pid=21)  # same terminal, another process (a resumed agent)
        with self.assertRaisesRegex(handoff.Refusal, "runs another process"):
            self.call(handoff.workers_decide, "t2", "close", "obsolete", "r", False)
        self.herdr["t2"] = dict(self.herdr["t2"], pid=20)
        original = self.fake_hc
        def busy_while_read(*args, timeout=8):
            if args[0] == "read":
                self.herdr["t2"] = dict(self.herdr["t2"], status={"state": "working"})
            return original(*args, timeout=timeout)
        with patch.object(handoff, "hc", side_effect=busy_while_read):
            with self.assertRaisesRegex(handoff.Refusal, "changed from done to working"):
                handoff.workers_decide(self.ctx, "t2", "close", "obsolete", "r", False)
        self.assertEqual(self.killed, [])

    def test_close_without_a_saved_screen_or_an_observed_end_is_not_counted(self):
        self.capture()
        with self.assertRaisesRegex(handoff.Refusal, "run `codex context-handoff workers` first"):
            self.call(handoff.workers_decide, "t2", "close", "obsolete", "r", False)
        self.call(handoff.workers_inspect)
        def stubborn(*args, timeout=8):
            if args[0] == "kill":
                self.killed.append(args[-1])
                return {"state": "live"}
            return self.fake_hc(*args, timeout=timeout)
        with patch.object(handoff, "hc", side_effect=stubborn):
            result = handoff.workers_decide(self.ctx, "t2", "close", "obsolete", "r", False)
        self.assertEqual(result["decision"]["outcome"], "close-unconfirmed")
        with self.assertRaisesRegex(handoff.Refusal, "still running: t2"):
            for address in ("t3", "t4", "t5"):
                self.call(handoff.workers_decide, address, "keep", "still useful", None, False)
            self.call(handoff.workers_done)

    def test_done_needs_evidence_and_a_decision_for_every_live_or_finished_worker(self):
        self.capture()
        with self.assertRaisesRegex(handoff.Refusal, "screens were not saved yet"):
            self.call(handoff.workers_done)
        self.call(handoff.workers_inspect)
        with self.assertRaisesRegex(handoff.Refusal, "no decision yet: t2, t3, t4, t5"):
            self.call(handoff.workers_done)
        self.call(handoff.workers_decide, "t2", "reuse", "next task", None, False)
        self.call(handoff.workers_decide, "t3", "keep", "still running tests", None, False)
        self.call(handoff.workers_decide, "t4", "keep", "parent decides", None, False)
        self.call(handoff.workers_decide, "t5", "keep", "its screen stays for audit", None, False)
        result = self.call(handoff.workers_done)
        report = Path(result["report"]).read_text()
        self.assertIn("| t2 reviewer | live | live | reuse | recorded | next task |", report)
        self.assertIn("workers-reviewed", handoff.read_ledger(self.state)["stages"])
        self.assertTrue(self.state.with_suffix(".workers-report.json").is_file())
        self.call(handoff.workers_done)  # re-runnable


def native_blocked_turn(turn):
    """Codex 0.169's record of a prompt its UserPromptSubmit hook blocked (live QA rollout shape)."""
    return [
        event("task_started", turn_id=turn, turn_attribution={"turn_id": turn, "turn_trigger": "user"}),
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"type": "input_text", "text": "<skills_instructions>"}]}},
        {"type": "world_state", "payload": {"full": False}},
        {"type": "turn_context", "payload": {"turn_id": turn}},
        event("task_complete", turn_id=turn, last_agent_message=None),
    ]


class BlockedTurnTests(SwitchTests):
    """A held worker message still gets an empty turn of its own in Codex; only that is not a takeover."""

    def append(self, *rows):
        with self.transcript.open("a") as stream:
            stream.writelines(json.dumps(row) + "\n" for row in rows)

    def hold(self, turn="w1", session="session"):
        payload = {"session_id": session, "turn_id": turn, "cwd": "/w", "prompt": REPORT_TEXT}
        return handoff.mark_takeover(payload, self.root, "t1")[1]

    def test_the_blocked_turn_shape_has_no_content(self):
        self.append(*native_blocked_turn("w1"))
        self.assertEqual(handoff.turns_since(self.transcript, "turn")[1], {"id": "w1", "complete": True, "content": False})
        self.assertEqual(handoff.old_chat_turns(self.record, self.state), ("pending", True))  # not recorded yet
        self.assertIn("held", self.hold())
        self.assertEqual(handoff.held_events(self.state)[0]["turn"], "w1")
        self.assertEqual(handoff.old_chat_turns(self.record, self.state), ("held", True))
        # Anything a person or the model adds makes it a takeover, held or not.
        for extra in (event("token_count", info=usage(50)), event("item_completed", item={"type": "UserMessage"}),
                      {"type": "response_item", "payload": {"type": "function_call", "call_id": "c"}}):
            with self.subTest(extra=extra["payload"]["type"]):
                rows = native_blocked_turn("w1")
                rows.insert(2, extra)
                self.transcript.write_text("")
                self.write_events(self.meta, event("task_started", turn_id="turn"), event("task_complete", turn_id="turn"),
                                  *rows)
                self.assertEqual(handoff.old_chat_turns(self.record, self.state)[0], "moved")

    def test_a_held_message_in_the_grace_window_with_its_native_turn_recovers(self):
        def worker_arrives():
            rows = native_blocked_turn("w1")
            self.append(rows[0])          # Codex starts the turn, then runs the prompt hook
            self.assertIn("held", self.hold("w1"))
            self.append(*rows[1:])
            self.term.set(screen=HELD_OLD, turn=6, status="done")
        self.clock.at(1.2, worker_arrives)
        self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual([a["kind"] for a in self.ledger()["attempts"]], ["request", "switch"])
        self.assertEqual(len(self.keys), 1)

    def test_an_unexplained_or_answered_turn_is_still_a_takeover(self):
        def stray():
            self.append(*native_blocked_turn("x9"))  # an empty turn no held message explains
            self.term.set(turn=6, status="done")
        self.clock.at(1.2, stray)
        with self.assertRaisesRegex(RuntimeError, "A new turn started|changed before /clear"):
            self.run_switch()
        self.assertEqual(self.keys, [])
        shutil.rmtree(self.root / "session")
        self.setUp()
        def answered():
            rows = native_blocked_turn("w2")
            self.append(rows[0])
            self.hold("w2")
            self.append(event("item_completed", item={"type": "AgentMessage", "content": []}), *rows[1:])
            self.term.set(screen=HELD_OLD, turn=6, status="done")
        self.clock.at(1.2, answered)
        with self.assertRaisesRegex(RuntimeError, "A new turn started"):
            self.run_switch()
        self.assertEqual(self.keys, [])

    def test_after_clear_a_running_empty_turn_in_the_old_chat_is_a_takeover(self):
        cache = {}
        self.assertFalse(handoff.source_moved(self.record, cache, self.state))
        self.append(native_blocked_turn("w1")[0])
        self.assertIn("held", self.hold("w1"))
        self.assertTrue(handoff.source_moved(self.record, cache, self.state))  # held but still running
        self.append(*native_blocked_turn("w1")[1:])
        self.assertFalse(handoff.source_moved(self.record, cache, self.state))  # finished and held


def own_tests_only(cls):
    """Reuse a test class's fixtures without running its tests a second time."""
    for name in dir(cls):
        if name.startswith("test_") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@own_tests_only
class CoreReviewTests(SwitchTests):
    """Regressions for the independent core review (H1-H4, M1-M6)."""

    def announce(self, **ledger):
        handoff.update_ledger(self.state, lambda data: data.update(ledger))
        handoff.post_notice(self.root, "t1", "Context handoff did not complete: test")
        self.state.with_suffix(".closed").write_text("x\n")
        handoff.disarm_guard(self.state, self.record)
        calls = []
        def fake(*args, timeout=8):
            calls.append(args)
            return {"started": False}
        with patch.object(handoff.time, "monotonic", self.clock.monotonic), \
                patch.object(handoff.time, "sleep", self.clock.sleep), \
                patch.object(handoff, "terminal_info", self.term.info), \
                patch.object(handoff, "read_screen", self.term.read), patch.object(handoff, "hc", side_effect=fake):
            shown = handoff.announce_failure(self.state, self.record)
        return shown, calls

    def test_h1_the_notice_is_typed_only_at_idle_and_always_with_if_idle(self):
        self.term.set(status="done")
        shown, calls = self.announce()
        self.assertFalse(shown)
        self.assertEqual(calls, [])
        self.assertTrue((self.root / "t1.notice").exists())
        self.state.with_suffix(".notified").unlink()
        self.clock.at(1, lambda: self.term.set(status="idle"))
        shown, calls = self.announce()
        self.assertTrue(shown)
        self.assertEqual(calls[0][:4], ("send", "--json", "--if", "idle"))

    def test_h2_never_while_clear_or_a_continuation_may_be_in_flight(self):
        self.term.set(status="idle", screen=FRESH)
        clear = {"turn": 5, "typed_at": 1}
        for case in ({"phase": "clearing"}, {"phase": "sending", "sends": [{"intent_at": 1, "turn": 5}]},
                     {"phase": "sent", "sends": [{"turn": 5, "result": {"started": False}}]},
                     {"phase": "post-clear", "sends": [{"turn": 5, "result": {"started": False}}]}):
            with self.subTest(case=case["phase"]):
                self.state.with_suffix(".notified").unlink(missing_ok=True)
                shown, calls = self.announce(clear=clear, **case)
                self.assertFalse(shown)
                self.assertEqual(calls, [])
        # A send proven absent (and retried) no longer blocks the notice.
        self.state.with_suffix(".notified").unlink(missing_ok=True)
        shown, _ = self.announce(phase="post-clear", clear=clear,
                                 sends=[{"turn": 5, "result": {"started": False}, "delivery_absent": True}])
        self.assertTrue(shown)

    def test_h2_an_os_error_from_the_continuation_send_is_reconciled_not_left_sending(self):
        calls = []
        original = self.fake_hc
        def fake(*args, timeout=8):
            if args[0] == "send" and args[-1].startswith("Codex Context Handoff:"):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("socket closed")
                return original(*args, timeout=timeout)
            return original(*args, timeout=timeout)
        self.on_send = None
        self.fake_hc = fake
        self.run_switch()
        self.assertEqual(len(calls), 2)  # proven not delivered, then sent again
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")

    def test_h4_a_repeated_identical_worker_message_still_explains_the_change(self):
        payload = {"session_id": "fresh", "turn_id": "", "cwd": "/w", "prompt": '<hc from="t2c">ping</hc>'}
        self.assertIn("held", handoff.mark_takeover(payload, self.root, "t1")[1])
        base = handoff.held_seen(self.state)
        self.assertIn("held", handoff.mark_takeover(payload, self.root, "t1")[1])
        self.assertEqual(handoff.held_count(self.state), 1)  # one revision in the note
        self.assertEqual(handoff.held_seen(self.state), base + 1)  # but a second delivery was seen
        with patch.object(handoff.time, "monotonic", self.clock.monotonic), \
                patch.object(handoff.time, "sleep", self.clock.sleep):
            with self.assertRaises(handoff.Retryable):
                handoff.settle_change(self.state, self.record, base, "post-clear", "final")

    def test_m1_a_clear_mid_redraw_is_reconciled_by_polling(self):
        frames = iter([("idle", 5, 0, "\n\n  \n"), ("idle", 5, 0, "\n\n  \n"), ("working", 5, 1, FRESH)])
        last = [None]
        def view(record):
            last[0] = next(frames, last[0])
            return last[0]
        with patch.object(handoff, "look", side_effect=view), \
                patch.object(handoff.time, "monotonic", self.clock.monotonic), \
                patch.object(handoff.time, "sleep", self.clock.sleep):
            self.assertTrue(handoff.reconcile_clear(self.record, OLD, 5, "TimeoutExpired")["reconciled"])

    def test_m2_a_receipt_written_while_evidence_is_gathered_is_success(self):
        def late(record, since):
            self.state.with_suffix(".received").write_text("fresh\n")
            return ["/x/rollout"]
        self.term.set(status="idle", screen=FRESH, turn=6)
        with patch.object(handoff, "continuation_rollouts", side_effect=late), \
                patch.object(handoff.time, "monotonic", self.clock.monotonic), \
                patch.object(handoff.time, "sleep", self.clock.sleep), \
                patch.object(handoff, "terminal_info", self.term.info), \
                patch.object(handoff, "read_screen", self.term.read):
            self.assertTrue(handoff.reconcile_receipt(self.record, self.state, OLD, 6, 0, False))


# Codex while it starts the fresh chat (or runs a just-submitted prompt) can show no prompt in the footer.
NO_FOOTER = "╭ Starting MCP servers ╮\n│ codex_apps │\n╰──────────────────────╯\n\n  Working (0s • esc to interrupt)\n"
BLANK = "\n\n  \n"


@own_tests_only
class PostClearStartupTests(BlockedTurnTests):
    """Live Goal-02: a worker message Herdr submits while the fresh chat starts draws over the footer
    before its UserPromptSubmit hook holds it; that startup frame is not a final failure."""

    def run_switch(self):
        """Also records when the switch failed (before its notice waits for an idle prompt)."""
        original = handoff.report_failure
        def failed(*args):
            self.failed_at = self.clock.now - self.cleared_at
            return original(*args)
        with patch.object(handoff, "report_failure", side_effect=failed):
            SwitchTests.run_switch(self)

    def startup(self, *steps):
        def keys():
            self.cleared_at = self.clock.now
            self.term.set(screen=BLANK, status="idle")
            self.clock.at(0.2, lambda: self.term.set(screen=NO_FOOTER, status="working"))
            for seconds, action in steps:
                self.clock.at(seconds, action)
            return {"turn": 5, "status": "done"}
        self.on_keys = keys

    def test_a_worker_message_held_during_startup_is_retried_in_the_same_fresh_chat(self):
        def worker():
            self.assertIn("held", self.hold("f1", session="fresh"))  # the hook runs ~1.4 s into the frame
        def blocked_turn_ends():
            self.term.set(screen=HELD_FRESH, status="done", turn=6)
        self.startup((1.6, worker), (2.4, blocked_turn_ends))
        self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual(len(self.keys), 1)
        self.assertEqual([a.get("phase") for a in self.ledger()["attempts"]], [None, "post-clear"])
        self.assertEqual(handoff.held_entries(self.state)[0]["session"], "fresh")
        self.assertFalse((self.root / "t1.switch").exists())

    def test_the_guard_stays_armed_while_the_startup_frame_lasts(self):
        armed = []
        self.startup(*((t, lambda: armed.append(handoff.guard_armed(self.state, self.record))) for t in (1, 3, 4.5)))
        settles, original = [], handoff.settle_change
        def settle(*args):
            settles.append(self.clock.now - self.cleared_at)
            return original(*args)
        with patch.object(handoff, "settle_change", side_effect=settle), \
                self.assertRaisesRegex(RuntimeError, "The fresh chat stayed busy without a prompt in the footer"):
            self.run_switch()
        self.assertEqual(armed, [True, True, True])
        # Bounded: the grace, then the hook's settle time, and no retry without a held message.
        self.assertEqual(len(settles), 1)
        self.assertGreaterEqual(settles[0], handoff.STARTUP_GRACE)
        self.assertLess(settles[0], handoff.STARTUP_GRACE + 0.5)
        self.assertEqual(len(self.ledger()["attempts"]), 1)
        self.assertEqual(len(self.keys), 1)
        self.assertEqual([s for s in self.sends if s.startswith("Codex Context Handoff:")], [])

    def test_a_person_or_a_draft_during_startup_is_still_final(self):
        self.startup((1, lambda: self.deliver("wait, not yet", session="fresh")))
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.run_switch()
        self.assertTrue(self.state.with_suffix(".cancelled").is_file())
        shutil.rmtree(self.root / "session")
        self.setUp()
        draft = FRESH.replace("› Ask Codex to do anything", "› my own note")
        self.startup((1, lambda: self.term.set(screen=draft, status="idle")))
        with self.assertRaisesRegex(RuntimeError, "Text appeared in the composer"):
            self.run_switch()
        self.assertLess(self.failed_at, 1.5)
        self.assertEqual([s for s in self.sends if s.startswith("Codex Context Handoff:")], [])

    def test_a_multiline_draft_above_the_footer_is_final_at_once(self):
        # A three-line draft pushes the composer's prompt line above the footer: "no prompt in the footer".
        draft = NO_FOOTER.replace("╭ Starting", "› my own note\n  second line\n  third line\n\n╭ Starting")
        self.startup((1, lambda: self.term.set(screen=draft, status="working")))
        with self.assertRaisesRegex(RuntimeError, "Text appeared in the composer"):
            self.run_switch()
        self.assertLess(self.failed_at, 1.5)
        # A worker message's own prompt line is not a draft.
        worker = NO_FOOTER.replace("╭ Starting", "› " + REPORT_TEXT[:40] + "\n  more\n\n╭ Starting")
        self.assertFalse(handoff.draft_above_footer(worker, OLD))
        self.assertFalse(handoff.draft_above_footer(NO_FOOTER + "› /clear\n", OLD))

    def test_footerless_frames_are_tolerated_only_while_working(self):
        cache = {}
        for status, expected in (("working", None), ("idle", "No fresh, empty prompt after /clear (no prompt in the footer)"),
                                 ("done", "The fresh chat reported done")):
            with self.subTest(status=status):
                view = (status, 5, 0, NO_FOOTER)
                self.assertEqual(handoff.fresh_chat_problem(view, self.state, self.record, OLD, 5, False, cache,
                                                            startup=True), expected)
        # Blank is the redraw echo only: never past CLEAR_ECHO, even in startup.
        self.assertIsNotNone(handoff.fresh_chat_problem(("working", 5, 0, BLANK), self.state, self.record, OLD, 5,
                                                        False, cache, startup=True))


def fresh_first_turn(turn, text=None):
    """A fresh chat's first turn as Codex 0.169 writes it before the prompt hook runs (live Goal-02)."""
    rows = [
        event("task_started", turn_id=turn, turn_attribution={"turn_id": turn, "turn_trigger": "user"}),
        {"type": "response_item", "payload": {"type": "additional_tools", "role": "developer", "tools": []}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"type": "input_text", "text": "You are Codex"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/w</cwd>\n</environment_context>"}]}},
        {"type": "world_state", "payload": {"full": True}},
        {"type": "turn_context", "payload": {"turn_id": turn}},
        event("thread_settings_applied", thread_id="fresh"),
    ]
    return rows


@own_tests_only
class FreshRolloutTests(PostClearStartupTests):
    """The fresh chat's own rollout: a turn that started without content is its prompt on the way to the
    hook (waited for, guard armed); content no held message explains is final."""

    def setUp(self):
        PostClearStartupTests.setUp(self)
        self.sessions = self.root / "home" / "sessions"
        day = self.sessions / handoff.time.strftime("%Y/%m/%d")
        day.mkdir(parents=True)
        source = day / "rollout-2000-01-01T00-00-00-session.jsonl"
        source.write_text(self.transcript.read_text())
        self.transcript = source
        self.record["transcript"] = str(source)
        self.state.write_text(json.dumps(self.record))
        self.fresh = None

    def fresh_rows(self, *rows):
        if self.fresh is None:
            stamp = handoff.time.strftime("rollout-%Y-%m-%dT%H-%M-%S", handoff.time.localtime())
            self.fresh = self.transcript.parent / f"{stamp}-fresh.jsonl"
            meta = {"type": "session_meta", "payload": {"id": "fresh", "cwd": str(self.root)}}
            self.fresh.write_text(json.dumps(meta) + "\n")
        with self.fresh.open("a") as stream:
            stream.writelines(json.dumps(row) + "\n" for row in rows)

    def queued_worker(self, hook_at, steps=()):
        """FRESH first, then Herdr submits a queued worker message whose hook runs only at `hook_at`."""
        def keys():
            self.cleared_at = self.clock.now
            self.term.set(screen=FRESH, status="idle")
            def submitted():
                self.fresh_rows(*fresh_first_turn("f1"))
                self.term.set(screen=NO_FOOTER, status="working")
            self.clock.at(0.6, submitted)
            for seconds, action in steps or ((hook_at, self.blocked),):
                self.clock.at(seconds, action)
            return {"turn": 5, "status": "done"}
        self.on_keys = keys

    def blocked(self):
        self.assertIn("held", self.hold("f1", session="fresh"))
        self.fresh_rows(event("task_complete", turn_id="f1", last_agent_message=None))
        self.term.set(screen=HELD_FRESH, status="done", turn=6)

    def test_the_first_turn_context_is_not_content(self):
        self.fresh_rows(*fresh_first_turn("f1"))
        self.assertEqual(handoff.turns_since(self.fresh, None), [{"id": "f1", "complete": False, "content": False}])
        self.assertEqual(handoff.fresh_chat_turns(self.record, self.state, handoff.time.time() - 5), "pending")
        self.assertEqual(handoff.fresh_chat_turns(self.record, self.state, handoff.time.time() + 5), "none")  # too old

    def test_a_hook_slower_than_the_input_settle_still_holds_after_the_first_fresh_prompt(self):
        self.queued_worker(hook_at=4.5)  # 3.9 s after the screen changed: past INPUT_SETTLE
        self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual(len(self.keys), 1)
        self.assertEqual([a.get("phase") for a in self.ledger()["attempts"]], [None, "post-clear"])
        self.assertEqual(len([s for s in self.sends if s.startswith("Codex Context Handoff:")]), 1)

    def armed_at(self, *times):
        armed = []
        return armed, [(t, lambda: armed.append(handoff.guard_armed(self.state, self.record))) for t in times]

    def test_a_20s_mcp_startup_before_the_hook_is_waited_for_with_proof(self):
        # The supported slow startup: Codex submits the queued worker message but runs its hook 20 s later.
        armed, checks = self.armed_at(5, 12, 19)
        self.queued_worker(None, steps=[*checks, (20.6, self.blocked)])
        self.run_switch()
        self.assertEqual(armed, [True, True, True])
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual(len(self.keys), 1)
        self.assertEqual([a.get("phase") for a in self.ledger()["attempts"]], [None, "post-clear"])
        self.assertEqual(len([s for s in self.sends if s.startswith("Codex Context Handoff:")]), 1)

    def test_a_pending_turn_ends_with_the_startup_wait_not_before(self):
        armed, checks = self.armed_at(10, 20, 29.5)
        self.queued_worker(None, steps=checks)
        with patch.object(handoff, "startup_wait", return_value=30.0), \
                self.assertRaisesRegex(RuntimeError, "stayed open past its deadline"):
            self.run_switch()
        self.assertEqual(armed, [True, True, True])
        self.assertGreaterEqual(self.failed_at, 30.0)
        self.assertLess(self.failed_at, 30.5)  # the same deadline: no settle time added on top
        self.assertEqual(len(self.ledger()["attempts"]), 1)  # never retried while a turn is open
        self.assertEqual([s for s in self.sends if s.startswith("Codex Context Handoff:")], [])

    def test_without_proof_a_pending_turn_waits_only_the_short_bound(self):
        (self.root / "session" / ".prompt-hook").unlink()
        self.queued_worker(None, steps=[(999, lambda: None)])  # the hook never decides
        with self.assertRaisesRegex(RuntimeError, "stayed open past its deadline"):
            self.run_switch()
        self.assertGreaterEqual(self.failed_at, 0.6 + handoff.PENDING_TURN_WAIT)
        self.assertLess(self.failed_at, 0.6 + handoff.PENDING_TURN_WAIT + 0.5)

    def test_a_message_held_right_at_the_deadline_gets_its_turn_flushed(self):
        def held():
            self.assertIn("held", self.hold("f1", session="fresh"))  # 0.1 s before the deadline
        def flushed():
            self.fresh_rows(event("task_complete", turn_id="f1", last_agent_message=None))
        def shown():
            self.term.set(screen=HELD_FRESH, status="done", turn=6)
        self.queued_worker(None, steps=[(19.9, held), (20.5, flushed), (20.6, shown)])
        with patch.object(handoff, "startup_wait", return_value=20.0):
            self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual([a.get("phase") for a in self.ledger()["attempts"]], [None, "post-clear"])

    def test_the_flush_grace_is_short_and_needs_a_held_message(self):
        def held():
            self.assertIn("held", self.hold("f1", session="fresh"))
        self.queued_worker(None, steps=[(19.9, held)])  # task_complete never written
        with patch.object(handoff, "startup_wait", return_value=20.0), \
                self.assertRaisesRegex(RuntimeError, "stayed open past its deadline"):
            self.run_switch()
        self.assertLess(self.failed_at, 19.9 + handoff.TURN_FLUSH + 0.5)

    def test_unheld_content_in_the_fresh_chat_is_final(self):
        def answered():
            self.fresh_rows(*report_rows(), event("item_completed", item={"type": "AgentMessage", "content": []}))
        self.queued_worker(None, steps=[(1.5, answered)])
        with self.assertRaisesRegex(RuntimeError, "Content that no held worker message explains"):
            self.run_switch()
        self.assertLess(self.failed_at, 1.8)
        self.assertEqual([s for s in self.sends if s.startswith("Codex Context Handoff:")], [])

    def test_an_idle_fresh_prompt_with_a_pending_turn_never_gets_the_continuation(self):
        # The screen still shows the fresh placeholder while Codex has started a turn: no settling.
        def started():
            self.fresh_rows(*fresh_first_turn("f1"))
        def keys():
            self.cleared_at = self.clock.now
            self.term.set(screen=FRESH, status="idle")
            self.clock.at(0.6, started)
            self.clock.at(5.0, self.blocked)
            return {"turn": 5, "status": "done"}
        self.on_keys = keys
        sent_at = []
        original = self.fake_hc
        def fake(*args, timeout=8):
            if args[0] == "send" and args[-1].startswith("Codex Context Handoff:"):
                sent_at.append(self.clock.now - self.cleared_at)
            return original(*args, timeout=timeout)
        self.fake_hc = fake
        self.run_switch()
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertEqual(len(sent_at), 1)
        self.assertGreater(sent_at[0], 5.0 + handoff.SETTLE)


@own_tests_only
class CoreReviewWorkerTests(WorkerReviewTests):
    def test_m3_session_start_is_utc_whatever_the_local_zone(self):
        import time as clock
        for zone in ("America/Los_Angeles", "Asia/Seoul", "UTC"):
            with self.subTest(zone=zone), patch.dict(os.environ, {"TZ": zone}):
                clock.tzset()
                for stamp, expected in (("2026-07-01T12:00:00Z", 1782907200000), ("2026-01-01T12:00:00Z", 1767268800000)):
                    self.write_events({"type": "session_meta", "timestamp": stamp, "payload": {}})
                    self.assertEqual(handoff.session_started_ms(self.transcript), expected)
        clock.tzset()

    def test_m4_an_incomplete_read_never_authorizes_a_close(self):
        self.capture()
        original = self.fake_hc
        def partial(*args, timeout=8):
            if args[0] == "read":
                return {"complete": False, "text": ""}
            return original(*args, timeout=timeout)
        with patch.object(handoff, "hc", side_effect=partial):
            handoff.workers_inspect(self.ctx)
            with self.assertRaisesRegex(handoff.Refusal, "run `codex context-handoff workers` first"):
                handoff.workers_decide(self.ctx, "t2", "close", "obsolete", "r", False)
        self.call(handoff.workers_inspect)  # a complete read now
        with patch.object(handoff, "hc", side_effect=partial):
            with self.assertRaisesRegex(handoff.Refusal, "incomplete read"):
                handoff.workers_decide(self.ctx, "t2", "close", "obsolete", "r", False)
        self.assertEqual(self.killed, [])

    def test_a_running_worker_is_reviewed_from_its_screen_and_kept_without_waiting(self):
        # Herdr: `hc read --lines 400` of a working agent is incomplete, `--lines 0` (screen) is complete.
        original = self.fake_hc
        def live_reads(*args, timeout=8):
            if args[0] == "read" and args[-1] in {"t3", "t4"}:
                screen_only = args[args.index("--lines") + 1] == "0"
                return {"complete": screen_only, "text": f"{args[-1]} working screen"}
            return original(*args, timeout=timeout)
        self.capture()
        with patch.object(handoff, "hc", side_effect=live_reads):
            result = handoff.workers_inspect(self.ctx)
            evidence = {row["address"]: row["evidence"] for row in result["workers"] if row.get("evidence")}
            self.assertEqual((evidence["t3"]["scope"], evidence["t3"]["partial_history"]), ("screen", True))
            self.assertEqual(evidence["t2"]["scope"], "history")
            # Closing a running worker still needs a complete read of its history.
            with self.assertRaisesRegex(handoff.Refusal, "incomplete read"):
                handoff.workers_decide(self.ctx, "t4", "close", "obsolete", "notes.md", False)
            for address in ("t3", "t4"):
                handoff.workers_decide(self.ctx, address, "keep", "still running; review its result later", None,
                                       False)
            handoff.workers_decide(self.ctx, "t2", "reuse", "next task", None, False)
            handoff.workers_decide(self.ctx, "t5", "keep", "finished; screen stays", None, False)
            self.assertIn("report", handoff.workers_done(self.ctx))
        self.assertEqual(self.killed, [])
        secret = "z" * 32
        self.state.with_suffix(".restore.json").write_text(json.dumps({"secret": secret}))
        self.ctx["folder"].mkdir()
        self.assertTrue(handoff.confirm_restored(self.ctx, handoff.read_token(secret, 0))["restored"])

    def test_m5_a_restarted_foreground_program_is_not_closed(self):
        self.herdr["t2"] = dict(self.herdr["t2"], foreground={"name": "codex", "pid": 200})
        self.capture()
        self.call(handoff.workers_inspect)
        self.herdr["t2"] = dict(self.herdr["t2"], foreground={"name": "codex", "pid": 201})
        with self.assertRaisesRegex(handoff.Refusal, "program was restarted"):
            self.call(handoff.workers_decide, "t2", "close", "obsolete", "r", False)
        self.assertEqual(self.killed, [])


@own_tests_only
class CoreReviewClassifierTests(ClassifierTests):
    def test_m6_child_notes_with_spaced_names(self):
        spaced = world(terminal("t1a", None), terminal("t9", "t1a", state="finished", name="build worker"))
        note = "<hc>t9 build worker exited (exit 0). Read: hc read t9</hc>"
        self.assertEqual(self.classify(note, herdr=spaced)[0], "worker-note")
        self.assertEqual(self.classify("<hc>t9 other name exited (exit 0). Read: hc read t9</hc>", herdr=spaced)[0],
                         "unverified")

    def test_h3_controls_and_removed_children_leave_the_destination_waits_pending(self):
        state = handoff.store_for(self.root, "session", "turn")
        state.parent.mkdir()
        state.write_text(json.dumps({"session": "session", "turn": "turn", "terminal": "t1"}))
        state.with_suffix(".restore.json").write_text(json.dumps({"secret": "s"}))
        state.with_suffix(".workers.json").write_text(json.dumps({"workers": [
            {"address": "t31", "parent": "t1", "name": "w", "state": "live"}]}))
        folder = self.root / "fresh"
        folder.mkdir()
        (folder / ".received-from").write_text("session turn\n")
        (folder / ".goal-restore").write_text(json.dumps({"source_session": "session", "objective": OBJECTIVE,
                                                          "remaining": None, "objective_sha256": "x"}))
        self.herdr = world(terminal("t1", None))  # t31 was removed with --rm right after it exited
        calls = []
        with patch.object(handoff, "hc_list", side_effect=lambda *_, **__: calls.append(1) or self.herdr):
            for prompt in ("handoff status", "  Handoff   STATUS ", "<hc>t31 w exited (exit 0). Read: hc read t31</hc>"):
                with self.subTest(prompt=prompt):
                    payload = {"session_id": "fresh", "prompt": prompt, "transcript_path": str(self.transcript)}
                    judges = {}
                    self.assertIsNone(handoff.cancel_restore(payload, self.root, "t1", judges))
                    self.assertIsNone(handoff.end_restoration_wait(payload, self.root, "t1", judges))
        self.assertEqual(len(calls), 1)  # one snapshot for the hook run that judged the note
        self.assertTrue((folder / ".goal-restore").exists())
        self.assertFalse((folder / ".restore-unconfirmed").exists())
        self.assertFalse((folder / ".goal-mismatch").exists())


own_tests_only(BlockedTurnTests)  # its SwitchTests fixtures, not SwitchTests' tests a second time


if __name__ == "__main__":
    unittest.main()
