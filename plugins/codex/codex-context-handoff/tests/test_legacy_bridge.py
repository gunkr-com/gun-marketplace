"""0.6.1: a handoff captured and switched by the 0.5.0 hooks is received by the 0.6 hooks.

The 0.5.0 switch process runs its own code to the end and sends the 0.5.0 continuation; the fresh chat may
already run the newer hooks. Only a state proven to be 0.5.0 (its own files and switch log) is accepted, and
only with the exact 0.5.0 text; the 0.6 restore checklist is added when it arrives.
"""

import json
import unittest
from unittest.mock import patch

import test_context_handoff
from test_context_handoff import OBJECTIVE, PAUSED_MS, handoff

# 0.5.0's continuation, copied from origin/main 0.5.0 (`continuation`), for a handoff carrying a Goal.
V050_TEXT = (
    "Codex Context Handoff: {session} {turn} {nonce} Read the local handoff note at {note}. Check the working "
    "tree and live Herdr workers before resuming the remaining work. Preserve the user's scope and decisions. "
    "A Goal was transferred with this handoff: before anything else, call create_goal with exactly the objective "
    "and token_budget from the note's \"Goal transfer\" section and print its raw result."
)


def own_tests_only(cls):
    for name in dir(cls):
        if name.startswith("test_") and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


@own_tests_only
class LegacyBridgeTests(test_context_handoff.GoalTransferTests):  # not bound here: its tests run once
    def setUp(self):
        test_context_handoff.GoalTransferTests.setUp(self)
        self.transferred()  # a real capture with a paused Goal, then turned into what 0.5.0 leaves behind
        self.record = json.loads(self.state.read_text())
        for suffix in (".restore.json", ".attempts.json", ".workers.json"):
            self.state.with_suffix(suffix).unlink(missing_ok=True)
        note = self.state.with_suffix(".md")
        note.write_text(note.read_text().split("\n## Restore checklist")[0] + "\n")
        self.state.with_suffix(".log").write_text(
            f"23:40:01 switch for t1 (pid {self.record['pid']}) started at Herdr turn 7\n"
            "23:40:02 old chat idle at Herdr turn 8; holding 10s for cancellation\n"
        )
        handoff.arm_guard(self.state, self.record)

    def text(self):
        return V050_TEXT.format(note=json.dumps(str(self.state.with_suffix(".md"))), **self.record)

    def payload_for(self, prompt, destination="fresh", cwd=None):
        return {"session_id": destination, "turn_id": "first", "cwd": cwd or str(self.root),
                "transcript_path": str(self.transcript), "prompt": prompt}

    def receive(self, prompt=None, **where):
        payload = self.payload_for(self.text() if prompt is None else prompt, **where)
        notice, held = handoff.mark_takeover(payload, self.root, "t1")
        return notice, held, handoff.prompt_hook(payload, self.root, "t1")

    def ctx(self):
        return {"root": self.root, "session": "fresh", "terminal": "t1", "folder": self.root / "fresh",
                "state": self.state, "record": self.record}

    def test_the_frozen_text_is_the_050_continuation(self):
        self.assertTrue(handoff.legacy_state(self.state, self.record))
        self.assertEqual(handoff.legacy_continuation(self.record, self.state), self.text())
        self.assertNotEqual(handoff.continuation(self.record, self.state), self.text())

    def test_a_050_goal_handoff_is_received_and_gets_the_restore_checklist(self):
        notice, held, result = self.receive()
        self.assertEqual((notice, held), (None, None))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(json.dumps({"objective": OBJECTIVE, "token_budget": 141764}, ensure_ascii=False), context)
        self.assertIn("restore checklist", context)
        self.assertIn("login: false", context)
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")
        self.assertFalse((self.root / "t1.switch").exists())
        self.assertEqual(json.loads(self.state.with_suffix(".legacy.json").read_text())["from"], "0.5.0")
        secret = json.loads(self.state.with_suffix(".restore.json").read_text())["secret"]
        note = self.state.with_suffix(".md").read_text()
        self.assertIn("written by Codex Context Handoff 0.5.0", note)
        self.assertIn("1. First, before anything else, call create_goal exactly", note)
        checklist = note.split("## Restore checklist")[1]
        self.assertEqual(note.count(handoff.WORKER_CLEANUP_RULE), 1)
        self.assertLess(checklist.index("call create_goal"), checklist.index(handoff.WORKER_CLEANUP_RULE))
        self.assertLess(checklist.index(handoff.WORKER_CLEANUP_RULE), checklist.index("restored --token"))
        self.assertIn(f"Read token (revision 0): {handoff.read_token(secret, 0)}", note)
        restore = json.loads((self.root / "fresh" / ".goal-restore").read_text())
        self.assertEqual((restore["objective"], restore["remaining"]), (OBJECTIVE, 141764))
        self.assertEqual(handoff.read_ledger(self.state)["stages"]["delivered"]["legacy"], "0.5.0")
        self.assertNotIn(handoff.read_token(secret, 0), context)  # the token is only in the note
        # Replayed, exactly or mixed: already delivered, never cancelled, nothing written twice.
        self.assertFalse(handoff.legacy_state(self.state, self.record))  # the marker carries it from here
        for replay in (self.text(), self.text() + " and more", handoff.continuation(self.record, self.state)):
            with self.subTest(replay=replay[-12:]):
                _, _, again = self.receive(replay)
                self.assertEqual(again["decision"], "block")
                self.assertIn("already been delivered", again["reason"])
                self.assertFalse(self.state.with_suffix(".cancelled").exists())
        self.assertEqual(note, self.state.with_suffix(".md").read_text())
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n")

    def test_a_failed_preparation_is_not_a_receipt_and_can_be_repeated(self):
        with patch.object(handoff, "destination_files", side_effect=OSError("disk full")):
            _, _, result = self.receive()
        self.assertEqual(result["decision"], "block")
        self.assertIn("could not be prepared", result["reason"])
        self.assertFalse(self.state.with_suffix(".received").exists())  # the 0.5.0 switch is not told it arrived
        self.assertTrue(handoff.guard_armed(self.state, self.record))
        self.assertFalse(self.state.with_suffix(".cancelled").exists())
        notice, held, result = self.receive()  # the same exact text again, now proven by the marker
        self.assertEqual((notice, held), (None, None))
        self.assertIn("hookSpecificOutput", result)
        self.assertEqual(self.state.with_suffix(".md").read_text().count("## Restore checklist"), 1)
        secret = json.loads(self.state.with_suffix(".restore.json").read_text())["secret"]
        self.assertIn(f"Read token (revision 0): {handoff.read_token(secret, 0)}", self.state.with_suffix(".md").read_text())
        self.assertTrue((self.root / "fresh" / ".goal-restore").is_file())

    def test_the_exact_goal_is_recreated_from_a_050_note(self):
        self.receive()
        self.set_goal(thread="fresh", tokens_used=900, token_budget=141764, created_at_ms=PAUSED_MS + 9000)
        self.state.with_suffix(".restored").write_text("{}")  # the checklist has tests of its own below
        self.assertEqual(self.fresh_stop(), {})
        restored = json.loads((self.root / "fresh" / ".goal-restored").read_text())
        self.assertEqual(restored["created_at_ms"], PAUSED_MS + 9000)

    def test_a_different_goal_is_not_taken_as_the_recreation(self):
        self.receive()
        self.set_goal(thread="fresh", tokens_used=900, token_budget=141000, created_at_ms=PAUSED_MS + 9000)
        self.state.with_suffix(".restored").write_text("{}")
        self.assertIn("does not match the transferred Goal", self.fresh_stop()["systemMessage"])
        self.assertFalse((self.root / "fresh" / ".goal-restored").exists())

    def test_the_worker_review_cannot_be_skipped_and_its_manifest_is_taken_after_create_goal(self):
        self.receive()
        secret = json.loads(self.state.with_suffix(".restore.json").read_text())["secret"]
        token = handoff.read_token(secret, 0)
        self.assertIn("review the Herdr workers", "; ".join(handoff.restoration_missing(self.state)))
        with self.assertRaisesRegex(handoff.Refusal, "review the Herdr workers"):
            handoff.confirm_restored(self.ctx(), token)
        self.herdr = {"t1": {"address": "t1", "parent": None, "state": "live"}}  # no workers of its own
        result = handoff.workers_inspect(self.ctx())
        self.assertEqual(result["workers"], [])
        manifest = json.loads(self.state.with_suffix(".workers.json").read_text())
        self.assertEqual((manifest["terminal"], manifest["workers"]), ("t1", []))
        self.assertTrue(handoff.confirm_restored(self.ctx(), token)["restored"])

    def test_the_helpers_still_refuse_before_the_goal_is_recreated(self):
        self.receive()
        transcript = self.root / "home" / "sessions" / "2026" / "11" / "15" / "rollout-x-fresh.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "fresh", "session_id": "fresh", "cwd": str(self.root), "source": "cli", "thread_source": "user"}}) + "\n")
        (self.root / "fresh" / ".hook-script").write_text(handoff.hook_script_record(123))
        env = {"CODEX_THREAD_ID": "fresh", "HC_TERMINAL_ID": "t1", "CODEX_HOME": str(self.root / "home")}
        with patch.dict("os.environ", env), self.eligible():
            with self.assertRaisesRegex(handoff.Refusal, "call create_goal"):
                handoff.helper_context(self.root)
        self.assertFalse(self.state.with_suffix(".workers.json").exists())

    def test_mixed_changed_or_foreign_texts_are_refused_and_cancel(self):
        cases = {
            "draft appended": lambda t: t + " also fix the tests",
            "draft first": lambda t: "hi " + t,
            "changed path": lambda t: t.replace(".md", ".txt", 1),
            "0.6 text": lambda t: handoff.continuation(self.record, self.state),
            "words dropped": lambda t: t.replace(" Preserve the user's scope and decisions.", ""),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.setUp()
                notice, held, result = self.receive(change(self.text()))
                self.assertIsNone(held)
                self.assertEqual(result["decision"], "block")
                self.assertTrue(self.state.with_suffix(".cancelled").is_file())
                self.assertFalse(self.state.with_suffix(".received").exists())
                self.assertFalse((self.root / "fresh" / ".goal-restore").exists())
                self.assertFalse(self.state.with_suffix(".restore.json").exists())

    def test_a_wrong_nonce_terminal_chat_or_worktree_is_not_delivered(self):
        wrong = self.text().replace(self.record["nonce"], "b" * 24)
        self.assertEqual(handoff.prompt_hook(self.payload_for(wrong), self.root, "t1"), {})
        self.assertEqual(handoff.prompt_hook(self.payload_for(self.text()), self.root, "t9"), {})
        same = handoff.prompt_hook(self.payload_for(self.text(), destination="session"), self.root, "t1")
        self.assertIn("fresh chat in the original worktree", same["reason"])
        elsewhere = handoff.prompt_hook(self.payload_for(self.text(), cwd="/elsewhere"), self.root, "t1")
        self.assertIn("fresh chat in the original worktree", elsewhere["reason"])
        self.assertFalse(self.state.with_suffix(".received").exists())

    def test_goal_transfer_turned_off_meanwhile_closes_it(self):
        with patch.object(handoff, "check_chat", return_value=(None, "test")):
            self.control("handoff goal transfer off")
        _, _, result = self.receive()
        self.assertEqual(result["decision"], "block")
        self.assertIn("Goal transfer was turned off", result["reason"])
        self.assertFalse(self.state.with_suffix(".received").exists())

    def test_without_the_050_proof_the_050_text_is_refused(self):
        def ledger():
            handoff.set_stage(self.state, "armed", origin="stop")
        changes = {
            "0.6 switch log": lambda: self.state.with_suffix(".log").write_text(
                f"23:40:01 switch for t1 (pid {self.record['pid']}) at Herdr turn 7\n"),
            "other pid": lambda: self.state.with_suffix(".log").write_text(
                "23:40:01 switch for t1 (pid 999) started at Herdr turn 7\n"),
            "other terminal": lambda: self.state.with_suffix(".log").write_text(
                f"23:40:01 switch for t2 (pid {self.record['pid']}) started at Herdr turn 7\n"),
            "no log": lambda: self.state.with_suffix(".log").unlink(),
            "restore file": lambda: self.state.with_suffix(".restore.json").write_text('{"secret": "x"}'),
            "ledger": ledger,
            "manifest": lambda: self.state.with_suffix(".workers.json").write_text("{}"),
        }
        for name, change in changes.items():
            with self.subTest(case=name):
                self.setUp()
                change()
                self.assertFalse(handoff.legacy_state(self.state, self.record))
                _, _, result = self.receive()
                self.assertEqual(result["decision"], "block")
                self.assertFalse(self.state.with_suffix(".received").exists())

    def test_a_worker_message_under_a_050_switch_cancels_as_in_050(self):
        self.herdr = {"t1": {"address": "t1", "parent": None, "state": "live"},
                      "t2": {"address": "t2", "parent": "t1", "state": "live", "status": {"app": "claude", "state": "done"}}}
        payload = self.payload_for('<hc from="t2 reviewer">Sample fixture report: step done.</hc>')
        notice, held = handoff.mark_takeover(payload, self.root, "t1")
        self.assertIsNone(held)  # not held: a 0.5.0 switch cannot retry
        self.assertIn("Context handoff stopped", notice)
        self.assertTrue(self.state.with_suffix(".cancelled").is_file())
        self.assertFalse(self.state.with_suffix(".held.jsonl").exists())

    def test_a_06_handoff_is_unchanged(self):
        self.setUp_06()
        self.assertFalse(handoff.legacy_state(self.state, self.record))
        result, _ = self.deliver()
        self.assertIn("hookSpecificOutput", result)
        self.assertFalse(self.state.with_suffix(".legacy.json").exists())
        self.assertNotIn("written by Codex Context Handoff 0.5.0", self.state.with_suffix(".md").read_text())

    def test_a_050_handoff_without_a_goal_gets_the_checklist_and_its_reminder(self):
        self.state.with_suffix(".goal.json").unlink()
        text = self.text().split(" A Goal was transferred")[0]
        self.assertEqual(handoff.legacy_continuation(self.record, self.state), text)
        notice, held, result = self.receive(text)
        self.assertEqual((notice, held), (None, None))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("came from version 0.5.0", context)
        self.assertIn(str(self.state.with_suffix(".md")), context)
        self.assertIn("login: false", context)
        self.assertNotIn("Read token", context)
        self.assertNotIn("create_goal", self.state.with_suffix(".md").read_text().split("## Restore checklist")[1])
        self.assertFalse((self.root / "fresh" / ".goal-restore").exists())
        reminder = handoff.restoration_check({}, self.root, "fresh")
        self.assertEqual(reminder["decision"], "block")
        self.assertIn("review the Herdr workers", reminder["reason"])

    def setUp_06(self):
        test_context_handoff.GoalTransferTests.setUp(self)
        self.transferred()
        self.record = json.loads(self.state.read_text())


if __name__ == "__main__":
    unittest.main()
