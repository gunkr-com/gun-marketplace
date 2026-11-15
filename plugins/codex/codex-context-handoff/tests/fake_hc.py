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
    if out is None:
        result, run = run_hook(context, args[-1])
        with World() as world:
            world["hook_runs"].append(run)
            out, code = apply_send(world, args[-1], result), 0
    print(json.dumps(out))
    return code


def run_hook(context, prompt):
    session = context["session"]
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "turn_id": f"u{context['call']}",
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
    if result.get("decision") == "block":
        world["screen"] = screen_with(world["screen"], text, result.get("reason", "blocked"))
        return {"started": False, "status": "idle", "turn": codex["turn"]}
    world["screen"] = screen_with(world["screen"], text, "ok")
    codex["turn"] += 1
    codex["status_ms"] = int(time.time() * 1000)
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
            world["screen"] = FRESH
            codex["status"] = {"app": "codex", "state": "idle"}
            codex["status_ms"] = int(time.time() * 1000)
        return before, 0
    if command == "kill":
        terminals[args[1]]["state"] = "finished"
        terminals[args[1]]["reason"] = "killed"
        return terminals[args[1]], 0
    return {"error": f"unsupported {command}"}, 2


if __name__ == "__main__":
    sys.exit(main())
