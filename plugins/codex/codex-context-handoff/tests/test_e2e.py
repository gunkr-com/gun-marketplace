"""End-to-end handoffs in an isolated world: the real hook script, the real detached switch process,
and a fake `hc` (tests/fake_hc.py) first on PATH. Plugin data, Codex home and transcripts are
temporary; the real Herdr core, Codex home, terminals and workers are never touched (each test checks
that every Herdr call went to the fake).
"""

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "hooks" / "context_handoff.py"
FAKE = Path(__file__).resolve().parent / "fake_hc.py"
OLD = ("› fix the bug\n• Fixed most of it.\n\n› Ask Codex to do anything\n\n  gpt · ~/work\n  ? for shortcuts")
NONCE = "c" * 24
WORKER = '<hc from="t2 reviewer">Review written: notes/review.md. Holding.</hc>'


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="handoff e2e My Drive ")  # spaces, as in real Drive paths
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.data, self.home, self.work = self.base / "data", self.base / "home", self.base / "work"
        for path in (self.data, self.home, self.work):
            path.mkdir()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "hc"
        shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE))} \"$@\"\n")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        sessions = self.home / "sessions" / "2026" / "11" / "14"
        sessions.mkdir(parents=True)
        self.transcripts = {sid: str(sessions / f"rollout-2026-11-14T00-00-0{n}-{sid}.jsonl")
                            for n, sid in enumerate(("src", "fresh"))}
        for sid, path in self.transcripts.items():
            rows = [{"type": "session_meta", "timestamp": "2026-11-14T00:00:00Z", "payload": {
                "id": sid, "session_id": sid, "cwd": str(self.work), "source": "cli", "thread_source": "user"}}]
            if sid == "src":
                rows += [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
                         {"type": "event_msg", "payload": {"type": "token_count", "info": {
                             "last_token_usage": {"total_tokens": 45}, "model_context_window": 100}}},
                         {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}}]
            Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.pid = os.getpid()  # stands in for the terminal's Codex process (the helpers run below it)
        self.env = dict(
            os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", PLUGIN_DATA=str(self.data),
            CODEX_HOME=str(self.home), HC_TERMINAL_ID="t1", CODEX_CONTEXT_HANDOFF="1",
            CODEX_CONTEXT_HANDOFF_GRACE="1", CODEX_HANDOFF_GOALS_DB=str(self.base / "no-goals.sqlite"),
            FAKE_HC_WORLD=str(self.base / "world.json"),
        )
        self.env.pop("CODEX_CONTEXT_HANDOFF_GOAL", None)
        codex = {"address": "t1", "parent": "t0", "state": "live", "cwd": str(self.work), "turn": 5,
                 "status": {"app": "codex", "state": "done"}, "status_ms": 1,
                 "foreground": {"name": "codex", "cwd": str(self.work), "pid": self.pid}}
        worker = {"address": "t2", "parent": "t1", "name": "reviewer", "state": "live", "cwd": str(self.work),
                  "status": {"app": "claude", "state": "done"}, "created_ms": 7, "pid": 77,
                  "foreground": {"name": "claude", "pid": 78}, "labels": {}}
        hook_env = {key: self.env[key] for key in ("PLUGIN_DATA", "HC_TERMINAL_ID", "CODEX_CONTEXT_HANDOFF",
                                                  "CODEX_HANDOFF_GOALS_DB", "PATH", "FAKE_HC_WORLD", "CODEX_HOME")}
        self.write_world({
            "terminals": {"t0": {"address": "t0", "parent": None, "state": "live"}, "t1": codex, "t2": worker,
                          "t9": {"address": "t9", "parent": None, "state": "live", "name": "user-session"}},
            "codex": "t1", "screen": OLD, "session": "src", "next_session": "fresh", "cwd": str(self.work),
            "script": str(SCRIPT), "hook_env": hook_env, "transcripts": self.transcripts,
            "calls": [], "hook_runs": [], "accepted": [],
        })
        self.state = self.data / "src" / "turn"
        self.state.parent.mkdir()
        self.record = {"session": "src", "turn": "turn", "cwd": str(self.work), "terminal": "t1", "pid": self.pid,
                       "transcript": self.transcripts["src"], "nonce": NONCE, "origin": "stop", "offset": 0,
                       "usage": 45.0}
        self.state.write_text(json.dumps(self.record))
        (self.data / "src" / ".prompt-hook").write_text(f"{self.pid}\n")

    def write_world(self, world):
        Path(self.env["FAKE_HC_WORLD"]).write_text(json.dumps(world))

    def world(self):
        return json.loads(Path(self.env["FAKE_HC_WORLD"]).read_text())

    def hook(self, event, **payload):
        body = {"hook_event_name": event, "session_id": "src", "turn_id": "turn", "cwd": str(self.work),
                "transcript_path": self.transcripts["src"], **payload}
        done = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(body), capture_output=True, text=True,
                              env=self.env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else {}

    def capture(self):
        result = self.hook("Stop", last_assistant_message=f"HANDOFF READY {NONCE}\n# Note\nNext: finish the tests.")
        self.assertIn("Context handoff ready", result["systemMessage"])

    def herdr_send(self, text):
        """A Herdr delivery into the Codex terminal (a worker's `hc send t1 ...`), through the fake."""
        done = subprocess.run(["hc", "send", "--json", "t1", text], capture_output=True, text=True, env=self.env,
                              timeout=30)
        return json.loads(done.stdout)

    def ledger(self):
        try:
            return json.loads(self.state.with_suffix(".attempts.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, condition, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        self.fail(f"timed out; switch log:\n{self.log()}")

    def log(self):
        try:
            return self.state.with_suffix(".log").read_text()
        except OSError:
            return "(no log)"

    def finished(self):
        return any(self.state.with_suffix(s).exists() for s in (".received", ".closed", ".cancelled")) and (
            "Continuation received" in self.log() or self.state.with_suffix(".closed").exists())

    def helper(self, *args):
        env = dict(self.env, CODEX_THREAD_ID="fresh")
        done = subprocess.run([sys.executable, str(SCRIPT), "--helper", str(self.data), *args], capture_output=True,
                              text=True, env=env, timeout=30)
        return done

    def assert_only_fake_herdr(self):
        world = self.world()
        self.assertTrue(world["calls"])
        self.assertTrue(all(isinstance(call, list) for call in world["calls"]))

    def test_clean_handoff_then_restore_with_worker_review(self):
        self.capture()
        self.wait_for(self.finished)
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n", self.log())
        world = self.world()
        self.assertEqual([a["session"] for a in world["accepted"]], ["fresh"], json.dumps(world["hook_runs"])[-1500:])
        self.assertTrue(world["accepted"][0]["prompt"].startswith(f"Codex Context Handoff: src turn {NONCE}"))
        manifest = json.loads(self.state.with_suffix(".workers.json").read_text())
        self.assertEqual([w["address"] for w in manifest["workers"]], ["t2"])  # not t0, t9 or itself
        # The receiving chat runs the restore checklist through the helpers.
        (self.data / "fresh" / ".hook-script").write_text(f"{SCRIPT.resolve()}\n{self.pid}\n")
        inspect = self.helper("workers")
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        self.assertEqual(json.loads(inspect.stdout)["undecided"], ["t2"])
        refused = self.helper("restored", "--token", "x")
        self.assertEqual(refused.returncode, 2)
        closed = self.helper("workers", "decide", "t2", "close", "--reason", "review read and summarized",
                             "--result", "notes/review.md")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertEqual(json.loads(closed.stdout)["decision"]["outcome"], "closed")
        self.assertEqual(self.world()["terminals"]["t2"]["state"], "finished")
        self.assertEqual(self.helper("workers", "done").returncode, 0)
        note = self.state.with_suffix(".md").read_text()
        token = [line for line in note.splitlines() if line.startswith("Read token")][-1].split(": ")[1]
        restored = self.helper("restored", "--token", token)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        status = json.loads(self.helper("status").stdout)
        self.assertTrue(status["restored"])
        for stage in ("note-valid", "cleared", "continuation-sent", "delivered", "workers-reviewed", "restored"):
            self.assertIn(stage, status["ledger"]["stages"])
        self.assertEqual(self.world()["terminals"]["t9"]["state"], "live")  # the user's own session is untouched
        self.assert_only_fake_herdr()

    def test_a_worker_message_in_the_grace_window_is_held_and_the_switch_recovers(self):
        self.capture()
        self.wait_for(lambda: "old-idle" in self.ledger().get("stages", {}))
        self.herdr_send(WORKER)  # blocked by the plugin's hook (Codex still records an empty turn)
        self.wait_for(self.finished)
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n", self.log())
        attempts = self.ledger()["attempts"]
        self.assertEqual([a["kind"] for a in attempts], ["request", "switch"], self.log())
        world = self.world()
        self.assertEqual(sum(1 for call in world["calls"] if call[0] == "keys"), 1)
        self.assertEqual([a["session"] for a in world["accepted"]], ["fresh"])  # the worker text reached no chat
        self.assertIn("## Held worker message 1 (from t2", self.state.with_suffix(".md").read_text())
        # Codex recorded an empty turn for the blocked prompt in the old chat, as it does natively.
        rows = [json.loads(line) for line in Path(self.transcripts["src"]).read_text().splitlines()]
        held_turn = json.loads(self.state.with_suffix(".held.jsonl").read_text().splitlines()[0])["turn"]
        self.assertIn({"type": "task_complete", "turn_id": held_turn, "last_agent_message": None},
                      [row["payload"] for row in rows if row["type"] == "event_msg"])
        self.assert_only_fake_herdr()

    def test_a_worker_message_after_clear_is_held_without_a_second_clear(self):
        self.capture()
        self.wait_for(lambda: "cleared" in self.ledger().get("stages", {}))
        self.herdr_send(WORKER)
        self.wait_for(self.finished)
        self.assertEqual(self.state.with_suffix(".received").read_text(), "fresh\n", self.log())
        world = self.world()
        self.assertEqual(sum(1 for call in world["calls"] if call[0] == "keys"), 1)
        held = [json.loads(line) for line in self.state.with_suffix(".held.jsonl").read_text().splitlines()]
        self.assertEqual(held[0]["session"], "fresh")
        # The fresh chat reported done after the held message's empty turn, so the continuation went
        # without `--if idle` after the switch checked the empty composer itself.
        sends = [call for call in world["calls"] if call[0] == "send" and "Codex Context Handoff:" in call[-1]]
        self.assertEqual(len(sends), 1)
        self.assertNotIn("--if", sends[0])
        self.assert_only_fake_herdr()

    def test_a_person_in_the_grace_window_wins_and_nothing_is_retried(self):
        self.capture()
        self.wait_for(lambda: "old-idle" in self.ledger().get("stages", {}))
        self.assertTrue(self.herdr_send("wait, I have one more thing")["started"])
        self.wait_for(self.finished)
        self.assertTrue(self.state.with_suffix(".cancelled").is_file())
        self.assertFalse(self.state.with_suffix(".received").exists())
        world = self.world()
        self.assertFalse(any(call[0] == "keys" for call in world["calls"]))
        self.assertEqual(len(self.ledger()["attempts"]), 1)
        self.assert_only_fake_herdr()


if __name__ == "__main__":
    unittest.main()
