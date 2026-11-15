# Codex Context Handoff

When an opted-in Codex CLI chat crosses 40% of its effective context window, the plugin checks for work that can continue. It records a handoff and opens a fresh chat in the same Herdr terminal. The current worktree and the terminal's existing Herdr child workers stay available, and the fresh chat confirms that it read the note and reviewed those workers (0.6.0).

## Install and enable

Requires Codex CLI, Herdr CLI (`hc`), and Python 3. The terminal automation uses the English Codex CLI prompt and Herdr's turn counter and status time (`hc get` fields `turn` and `status_ms`, present in Herdr 0.1.20). Tested on macOS with Codex CLI 0.167.2 and Herdr 0.1.20.

```bash
codex plugin marketplace add gunkr-com/gun-marketplace
codex plugin add codex-context-handoff@gun-marketplace
```

Open a new Codex CLI session. Review the plugin's `Stop`, `PreToolUse`, `PostToolUse`, and `UserPromptSubmit` hooks in `/hooks` before trusting them. A plugin update that changes the hooks must be trusted again; 0.4.0 adds `PreToolUse`, and until it is trusted the tool-call gate below does not run. Trust 0.6.0 again after updating.

Install changes as a new plugin version and trust its hooks again. Do not edit the installed copy under `~/.codex/plugins/cache/`: Codex then treats the plugin as changed ("hook approval required") and skips its hooks until they are trusted again, so a handoff can lose its note silently (its `Stop` hook never runs). A chat keeps the hooks it loaded at start; new behavior applies to chats started (or resumed) after the update is installed and trusted.

## Start Codex through the launcher

The hooks act only when Codex runs in the foreground of a Herdr terminal with `--no-daemon` and `CODEX_CONTEXT_HANDOFF=1`. The plugin ships a launcher, `bin/codex`, that adds both. It is the same launcher as the one installed in `~/.local/bin/codex` since 2026-11-11, including its full-access default, plus the transfer helper described under Goals. Put it ahead of the real `codex` on your `PATH`, for example:

```bash
ln -s "$PWD/plugins/codex/codex-context-handoff/bin/codex" ~/.local/bin/codex
hash -r   # zsh and bash cache the old codex path until a rehash or a new shell
```

Inside a Herdr terminal (`HC_TERMINAL_ID` is set), the launcher changes only interactive launches and sets `CODEX_CONTEXT_HANDOFF=1` for them:

- `codex [options] [prompt]` gets `--dangerously-bypass-approvals-and-sandbox --no-daemon` before the other arguments.
- `codex [options] resume|fork ...` gets them right after the subcommand, which has its own flags.
- Full access is the existing default for Herdr agent terminals. An explicit permission choice on the command line wins and the bypass flag is not added: `-s`/`--sandbox`, `-a`/`--ask-for-approval`, `--approve-for-me`, `-p`/`--profile`, or `-c` with `sandbox_mode`, `approval_policy`, `approvals_reviewer`, `profile`, `default_permissions` or `permissions[.*]`. A bypass flag already present (or `--yolo`) is not repeated.
- Your model and other flags are kept. An explicit `--no-daemon` is not repeated in the same place.
- `CODEX_CONTEXT_HANDOFF=0` turns off the handoff only; full access and `--no-daemon` still apply.
- `CODEX_HERDR_FULL_ACCESS=force` (set by `codex-herdr`) applies the same rules outside Herdr and is removed before Codex starts.

Everything else runs the real `codex` unchanged: `exec`, `review`, `app-server`, `plugin`, `agents`, and other subcommands; `--help`, `--version`, and `--remote` anywhere before `--`; an `-i` image list followed by a subcommand name; and launches outside Herdr. The launcher finds the real `codex` by skipping itself and any copy of itself on `PATH`. The one exception is `codex context-handoff request-transfer`, the plugin's own helper (see Goals), which never starts Codex.

Without the launcher, start Codex with `CODEX_CONTEXT_HANDOFF=1 codex --no-daemon` (or `codex resume --no-daemon <id>`).

The launcher cannot change a Codex process that is already running. A chat started with plain `codex` runs its hooks in the Codex daemon, whose environment can name another Herdr terminal, and the plugin does not guess the right one. Exit that chat and reopen it with `codex resume <session-id>` in the Herdr terminal. `handoff status` tells you when this is needed.

## Turn it off or on for one chat

Submit one of these as the whole prompt. The hook blocks it, so it never reaches the model, and shows the result:

| Prompt | Effect |
| --- | --- |
| `handoff off` (also `handoff cancel`, `핸드오프 끄기`, `핸드오프 취소`) | Turns the handoff off for this chat and cancels a handoff that is armed or waiting to switch. |
| `handoff on` (also `핸드오프 켜기`) | Removes this chat's off setting. A cancelled handoff stays cancelled. |
| `handoff status` (also `핸드오프 상태`) | Shows the requested setting, whether the hooks can act in this process, and any pending handoff. |

The setting belongs to the chat's session ID; there is no global switch. Case and extra spaces are ignored, but quotes or any other words make it an ordinary prompt. These commands also work when the process was not started through the launcher, so `handoff status` can say that a restart is needed.

`handoff goal transfer on` (`핸드오프 목표 이전 켜기`) and `handoff goal transfer off` (`핸드오프 목표 이전 끄기`) turn Goal transfer on or off for the whole plugin; see below. `handoff status` also shows `Goal transfer: on since <time>` or `off`.

A prompt of exactly `end session`, `wrap up`, `세션 종료`, or `마무리` goes to the model as usual and skips the handoff for that turn only.

A prompt of exactly `handoff now` (`핸드오프 지금`) also goes to the model, and arms a handoff for that turn whatever the context use, with the request added to the model's context. It carries an active Goal along like the automatic transfer (Goal transfer must be on), leaves a Goal that is not active alone, and is refused with the reason when the chat is off, has a pending handoff, or cannot hand off.

## How it works

1. Before each tool call, a hook checks the latest token count of the turn against the reported context window. The calculation includes the tokens counted by the runtime, so it can differ from the CLI's display. Once usage strictly exceeds the threshold (40% by default), the `PreToolUse` gate arms a handoff at that tool call; see "Long turns" below. For testing, set `CODEX_CONTEXT_HANDOFF_THRESHOLD` to a number between 0 and 100, exclusive.
2. When the gate does not arm (a put-off attempt waiting for more context use, attempts used up, a Goal that is not active), the `PostToolUse` hook adds one note per turn to the model's context instead (not in Goal turns): reach a safe checkpoint and end the turn if work will remain. It does not change the tool result or interrupt a running tool.
3. When a turn past the threshold is about to finish, Codex gets one request to prepare a short handoff if the work can continue. Completed work, work waiting on the user or an approval, and a user ending the session finish normally. This also works when another plugin's `Stop` hook has already continued the turn. The hook saves the note locally under the plugin's data directory.
4. After the turn completes and the terminal shows an empty prompt, the plugin waits through a cancellation window of 10 seconds (`CODEX_CONTEXT_HANDOFF_GRACE` accepts 0 to 60). Typing anything, submitting any prompt, a status change, or `handoff off` stops the switch. A verified Herdr worker message does not: it is held for the fresh chat (see "Herdr worker messages"). It checks again immediately before sending `/clear`. The "ready" message only means the note was saved; the switch reports a failure itself (see "Recovery").
5. After `/clear`, Codex can report itself busy while it starts MCP servers for the new chat, with only the empty welcome prompt on screen. The plugin waits for that startup, up to 150 seconds (`CODEX_CONTEXT_HANDOFF_STARTUP_WAIT` accepts 12 to 600), but only after it has seen this chat's `UserPromptSubmit` hook run in the same Codex process; otherwise it waits at most 12 seconds. During the wait, text in the prompt, any submitted prompt other than a verified worker message (including `handoff status`), a dialog, an error, a finished turn, or a second busy period stops the switch. A change that a worker message could explain waits 2 seconds for the prompt hook's record first. The new chat must then stay idle at its empty prompt for 2 seconds.
6. After a last check, the plugin submits the continuation prompt and verifies that the new chat receives it in the original worktree. The new chat runs it only if it arrives exactly as sent, before any other prompt, and before the switch stopped; otherwise it is blocked and the note is left for manual recovery. The continuation never changes after the note is written, so held worker messages cannot make it differ. The old chat remains accessible via `/resume`.
7. The receipt (`.received`) means the continuation arrived. The handoff counts as restored only when the fresh chat completes the note's restore checklist (see "Restore checklist").
8. The chat that receives the continuation is checked like any other from its first turn (0.3.0 skipped that whole turn, so a long first turn never handed off). If its first request was already past the threshold (a long note, say), it hands off again only after another 10 percentage points of context use (`CODEX_CONTEXT_HANDOFF_MIN_PROGRESS`, 0 to 90), so handoffs cannot loop; otherwise the usual threshold applies.

The hooks act only in an interactive root chat whose hook process descends from the foreground Codex process of the Herdr terminal, in the same worktree. The session source can be `cli`, or `vscode` for a chat first started through the daemon and later resumed with `--no-daemon`. The process check excludes IDE, daemon, `exec`, and subagent hooks.

The handoff includes the task, decisions, worktree, validation, next action, and the terminal's Herdr workers (see "Herdr workers"). The plugin stores its handoff notes and switch logs locally in `$PLUGIN_DATA/<old-session-id>/` (a `.goal.json` beside a note records a transferred Goal). If `$PLUGIN_DATA` is a symlink, the plugin uses the resolved directory, so two installs that share one data directory refer to the same note paths. The notes can contain project context. Review them before sharing. If a Codex update changes its transcript or prompt, the plugin can fall back to manual recovery.

## Long turns (0.5.0)

A turn can run for an hour without ending, so a handoff requested only at Stop can come far too late (one chat with no Goal went from 40% to 52% in a single turn after the note). Since 0.5.0 the `PreToolUse` gate holds every chat, not only Goal chats:

- **No Goal:** at the first tool call past the threshold the gate denies that call (it never runs) and gives the handoff request, with its nonce, as the reason. No Goal transfer consent is needed and no Goal tool is called or mentioned.
- **Active Goal:** the Goal transfer described under Goals (consent needed).
- **Goal not active** (paused, blocked, limited, complete) or a Goal state that cannot be read: the gate does not arm; the Stop request and the note above apply as before.

Until the turn ends, a held ordinary handoff allows at most 3 tool calls to reach a safe checkpoint (`CODEX_CONTEXT_HANDOFF_CHECKPOINT_CALLS`, 0 to 10). Every tool call takes one, including each call nested in a code-mode `exec` script; each `exec` wrapper also takes one of 3 scripts and runs only while a checkpoint call is left, so scripts with no nested call are bounded too. Goal tools are denied. After 12 denied calls the gate gives up for the turn. The hold ends early, and the attempt is put off with a message, when a prompt, a steer, or a Herdr message that counts as your input arrives after the arm (even if the model then writes the note; a verified worker message does not count, see "Herdr worker messages"), when auto-compaction brings the context back under the threshold, when a Goal appears in the chat or the Goal state can no longer be read, or when the turn ends without the note (an interrupt). A turn is armed at most once: after a release it is not held again, however far its context grows. A later turn is held again only after 5 more percentage points of context use, three put-off attempts per chat; after that only the Stop request applies. If the turn ends without the note and without input after the arm, Stop asks once more with the same nonce (one of the handoff's 3 attempts, see "Recovery").

`handoff now` and the transfer helper hold their turn the same way, without the compaction release. A handoff armed with no Goal is never switched while a Goal exists in the old chat: capture, the grace window and the last check before the continuation all read the Goal state again, and an unreadable state stops the switch too. The Goal itself is never touched.

## Goals

A Codex Goal starts its next turn as soon as a turn ends, so a chat with an active Goal never reaches the idle prompt the switch needs. The plugin reads the Goal state from `<CODEX_HOME>/goals_1.sqlite` read-only (found from the transcript path) and never writes it or calls any Goal API. A Goal state it cannot read stops the handoff for that chat with one message; it is never treated as "no Goal".

- **No Goal:** the ordinary handoff, held at a tool call since 0.5.0 (see Long turns).
- **Goal not active** (paused, blocked, usage- or budget-limited, complete): the usual handoff, asking the model to leave the Goal alone. If the Goal changes during the handoff turn, nothing is handed off.
- **Active Goal, transfer off:** no handoff request. The chat shows once that it is waiting and Codex auto-compaction handles context. If transfer is turned on later, the chat's next Goal turn past the threshold gets the transfer request.
- **Active Goal, transfer on, at a tool call (0.4.0):** a Goal turn can run for hours, and a note in the model's context was not enough (one chat went from 40% to 82% in one turn). So the `PreToolUse` hook arms the transfer at the first tool call past the threshold: it denies that call (it never runs) and gives the request below as the reason. Until the turn ends, it allows `get_goal`, one `update_goal` with status `paused`, and at most 3 other tool calls to reach a safe checkpoint (`CODEX_CONTEXT_HANDOFF_CHECKPOINT_CALLS`, 0 to 10); `create_goal`, any other `update_goal`, and further work are denied. It reads the Goal DB at every call, so once the armed Goal is no longer active it denies everything but `get_goal` (code mode's `exec` included) until the model gives its final answer. A running tool is never interrupted. The hold ends early, and the attempt is put off with a message, when a prompt, a steer, or a Herdr message (verified worker messages included, by default) arrives before the pause (your input wins), when auto-compaction brings the context back under the threshold, or after 12 denied calls. A put-off attempt is retried after 5 more percentage points of context use, three attempts per chat at most; one that ends after the pause fails closed as described below. If the turn then ends without the note, its Stop hook asks once more with the same nonce. A handoff armed in a turn that ended without its note and without Stop (an interrupt) is settled the same way at the next hook check, so it never stays pending.
- **Active Goal, transfer on, at Stop:** the request also asks the model to call `get_goal`, then `update_goal` with `paused`, and print both results. The plugin checks the transcript after its own request (no prompt, steer or `/goal` change from you after it; exactly one `update_goal` and no `create_goal`; an active then a paused result for the same Goal from `update_goal`) and that the DB shows the same paused Goal (same Goal ID, budget, token count and time). It then saves the objective, the remaining token budget (if any) and the counters with the note, and switches. The fresh chat is asked to call `create_goal` with exactly that objective and `token_budget`; its next Stop checks the DB once, reminds at most once, and reports a mismatch without fixing it. Counters restart in the fresh chat; the note and the plugin data keep the per-chat and total counters.

For a request armed at a tool call, `handoff now` or the helper, the evidence is everything the transcript records after the arm (and the tool call that was running at the arm, if its output came after it), and the Goal read as active from the DB at the arm stands in for the printed active result. A Goal paused before the arm is never transferred: it is not active at the arm, so the handoff leaves it alone.

**Asked in words.** When you ask the model in words to pause the Goal, move it to a fresh chat and continue, the model runs `codex context-handoff request-transfer` from a tool call in that chat, as the only call in that tool call, before it touches the Goal. The launcher finds the chat's plugin data from `CODEX_THREAD_ID` (or `CODEX_SESSION_ID`) and the chat's own record `<chat>/.hook-script`, which the 0.4.0 `UserPromptSubmit` hook writes with its script path and the chat's Codex process. It runs that script only if that process is one of its own ancestors; the script checks the record again and then applies the hooks' own checks (opted-in foreground Herdr Codex, interactive root chat, `.off`, a turn still running, a user message in that turn, Goal transfer on for an active Goal), arms the transfer and prints the request with its nonce. The model then pauses the Goal and ends the turn with the note; the switch and the restore in the fresh chat are the same as above. The helper is a provenance step for a request you made, not a permission of its own: a refusal exits 2 with the reason, and the Goal must then be left untouched. The proof reads tool inputs as text: a script after the arm that mentions `update_goal` or `create_goal` more than the one pause (even in a comment or string) fails the transfer closed, so the helper call and each Goal call go in tool calls of their own. It needs the 0.4.0 hooks to have run once in that chat's current process (any prompt does it) and a chat that can write to the plugin data directory. A chat whose process still runs older hooks has no record and is refused before its Goal is touched; continue in a new chat started through the launcher.

Transfer needs two things: the plugin consent from `handoff goal transfer on` (stored in `$PLUGIN_DATA/goal-transfer.json`), and a standing instruction from you, for example in `~/.codex/AGENTS.md`, that the model may pause and recreate a Goal when this plugin's request contains a "GOAL TRANSFER" section. Since 0.4.0 that request can arrive as a Stop hook continuation, as the reason a tool call was denied, as hook-added context, or as the helper's output, and the instruction should say when to run the helper. For example:

> Codex Context Handoff may ask you to transfer the active Goal, in a Stop hook continuation, in the reason a tool call was denied, in hook-added context, or in the output of `codex context-handoff request-transfer`. When that message has a GOAL TRANSFER section with a HANDOFF READY nonce, call get_goal, pause the Goal with update_goal status paused, make no other Goal change, and end the turn with the marker line and the handoff note. In the fresh chat, call create_goal with exactly the arguments the plugin gives. When I ask in words to pause the Goal, move it to a fresh chat and continue, first run `codex context-handoff request-transfer` as the only call in its own tool call, then follow its output; do not ask me again. `handoff on` alone is not consent. `CODEX_CONTEXT_HANDOFF_GOAL=off` disables transfer whatever the file says, and `handoff off` disables everything for its chat.

Turning transfer off (or the environment kill switch) at any point before the fresh chat accepts the continuation stops the handoff: the capture, the grace window, the startup wait, the final check before the continuation and the fresh chat itself all check it again. In the fresh chat, a Goal with the exact objective and budget counts as recreated whatever its native status (it may already be complete). Any prompt submitted there before the Goal is recreated ends the check, except a verified worker message or a `handoff` control (0.6.0): the plugin does not remind the model later, and says that the old Goal stays paused.

Each chat gets at most one transfer that reaches the pause (a put-off attempt does not count; see above). If the model does not pause, or the pause cannot be verified, nothing is switched: an active Goal keeps running in the same chat, and a paused one stays paused with a message to resume it with `/goal`. A handoff cancelled after the pause also leaves the old Goal paused; the plugin never resumes a Goal. `handoff status` lists a Goal paused for a handoff that did not reach a fresh chat.

Live checks for 0.4.0 (isolated Codex home, shipped 40% threshold with a reduced context window) covered a nested `exec_command` denied by the gate with the full request, the native `get_goal` and `update_goal` that followed, and a below-threshold transfer asked for in words through the helper, recreated in the fresh chat with the exact remaining budget and continued natively to completion. Earlier live checks cover a 20-second MCP startup, draft and submitted-prompt takeover, a 0.2.0-to-0.3.0 handoff through shared plugin data, and an active Goal's pause, exact remaining-budget recreation and native continuation to completion. Goal checks used a test-only 1% threshold; the shipped default remains 40%. Concurrent Stop-hook provenance and long transcripts also have automated regression coverage.

## Herdr worker messages (0.6.0)

Herdr types a worker's message into its parent terminal like a person would, and Codex records it as user input with no sender information. Before 0.6.0 every such message after the arm put the handoff off, so one worker report could cancel it (a live chat lost its handoff to two worker reports this way). Now:

- **What counts as a worker message.** The whole prompt is exactly one envelope that herdr-core writes, `<hc from="t7[ name]"[ reach="..."]>...</hc>`, and Herdr's own record shows the sender as a descendant of this terminal (its `parent` chain, which Herdr sets once when a terminal starts), with the same name if one is given. Herdr's own notes about a direct child (`<hc>t7 name exited (exit 0). Read: hc read t7</hc>`, `is blocked`, `hit an error`) count when they fit that child's state, or, for a child removed since, its entry in this handoff's worker manifest. The hook asks Herdr once per run (`hc ls --json --all`, 3-second limit) and keeps its verdicts per handoff.
- **What still counts as your input.** Anything typed, a message mixed with other text or with more than one envelope, a message from this terminal's parent or an unrelated terminal (those give directions), an unknown or another machine's sender, and any message when Herdr cannot be read. `handoff off`, drafts and prompts keep stopping the handoff at once.
- **During a turn** (after the arm, before the note), verified worker messages do not put an ordinary handoff off; the model sees them and puts them in the note. For a Goal transfer they still count until the Goal is paused, matching the standing instruction that worker messages are intervening input. After the verified pause they no longer fail the transfer. A plugin file `$PLUGIN_DATA/goal-worker-input.json` with `{"policy": "exempt"}` lets them through before the pause too, once your instructions say so.
- **While the switch is pending** (after the note, until the fresh chat receives the continuation), a verified worker message is held: the hook blocks it, so it reaches neither chat, after appending it under a lock to `<state>.held.jsonl` and to the note under "Held worker message N" (long messages are cut in the note; the full text stays in the log). Each held message adds a new read token line to the note. Repeats are kept once in the note but still count as deliveries for the switch. After the receipt, worker messages reach the fresh chat as usual.
- **Never a request.** A Herdr message never authorizes `codex context-handoff request-transfer`, and it does not end the fresh chat's Goal restore or restore checklist.

**Limits.** This is evidence, not proof. Herdr pastes messages into the terminal and keeps no delivery log, and its `caller` is self-declared, so a person typing a well-formed envelope that names a real descendant, or a process inside a worker terminal, cannot be told apart from Herdr. A message from an unrelated terminal is caught by the ownership check. With `notes = false` in herdr-core's config, Herdr sends no envelopes and every message counts as your input. The cost of a misjudged message is bounded: the switch is not stopped by it, and its text is kept for the fresh chat.

## Recovery (0.6.0)

Each handoff has a ledger, `<state>.attempts.json`, with its phase, stages and attempts. A handoff gets 3 attempts in all: the first request, then each note re-prompt or switch retry takes one.

- **Note.** The final answer must start with the marker line and carry a note of at most 12,000 characters. A marker on a later line, another handoff's nonce, a repeated marker, an empty or oversized note gets one specific re-prompt with the same nonce (each reason once); so does a held turn that ended without a note. A Stop request answered with no marker at all is the model declining (the work is complete or needs you), as before. Your input after the arm always wins over a re-prompt. A note that is still missing closes the handoff with a visible message.
- **Before `/clear`.** If the old chat does not reach an empty, idle prompt in time, Herdr does not answer, or a held worker message changes the screen during the cancellation window, the switch waits for the next idle boundary (up to 60 seconds) and runs the whole cancellation window again. It does not wait for more context use.
- **`/clear`.** The phase is written before `/clear` is typed. If `hc keys` fails, the switch polls the screen through Codex's redraw: a fresh chat continues, an unchanged old chat retries, anything else stops. `/clear` is never typed twice for one handoff.
- **After `/clear`.** A slow start or a held worker message in the fresh chat is retried in the same fresh chat, with no second `/clear`.
- **Continuation.** The phase is written before it is sent. Without a receipt the switch waits 20 more seconds, then looks at the evidence. Text in the composer, the continuation in the chat or in a new chat's transcript (the old chat's transcript is excluded), or a moved Herdr turn mean it may have arrived: it is not sent again and the switch stops. Only a continuation that provably never arrived (empty prompt, same turn, no transcript) is sent again.
- **Never retried:** your prompt or `handoff off`, a terminal that changed hands, a dialog or error, a new turn in the old chat, Goal or consent changes. A Goal paused for the handoff stays paused; resume it with `/goal`.
- **When it stops.** `.closed` records why, with the ledger summary. The notice is shown in the terminal at once if it is safe: the switch submits a fixed prompt, `Codex Context Handoff notice <id>`, once, with `hc send --if idle`, and only when the same Codex process is idle at an empty composer, its prompt hook is known to run, and no `/clear` or continuation is in flight. The plugin's own hook blocks that prompt (it never reaches the model) and shows the notice. Otherwise the notice waits for the next hook in that terminal. `handoff status` shows the last outcome. After a failed switch the chat does not hand off automatically again until 5 more percentage points of context are used; `handoff now` tries at once.

## Restore checklist (0.6.0)

The note ends with a checklist and a read token that appears only in the note file:

1. A transferred Goal is recreated first with `create_goal`; the helpers below refuse until it is.
2. Read the whole note, including held worker messages.
3. Review the workers (below).
4. Run `codex context-handoff restored --token <the last Read token in the note>`. A token from before a held message was added is refused, so the fresh chat must read the final note. No message is held after the receipt, so the note is final by then.

The fresh chat's `Stop` hook reminds once if the restore is not confirmed, then reports once. Your own prompt in the fresh chat ends the reminder; a worker message or a `handoff` control does not. `codex context-handoff status` shows the stages, attempts and what is missing.

## Herdr workers (0.6.0)

Before the cancellation window the switch saves a manifest, `<state>.workers.json`: every live descendant of the terminal (nested workers included) and the descendants that finished during the old chat (newest 20), with address, parent, name, labels, working directory, program and pid, status, and a result location from a `result`, `report` or `output` label. Terminals the terminal did not start, its parent and siblings are never listed.

The fresh chat reviews them with these helpers, run from a tool call (the launcher finds the chat as for `request-transfer`, and the chat must be the one that received the handoff in the same Herdr terminal). Output is JSON; a refusal exits 2 with the reason.

| Command | Effect |
| --- | --- |
| `codex context-handoff workers` | Compares the manifest with Herdr now (live, finished, gone, or stale when the address now holds another terminal or ownership changed), saves each worker's screen and recent history to `<state>.workers-evidence/`, and lists undecided workers and newer workers outside the review. A running worker's history read is incomplete in Herdr; its current screen is saved instead and marked `scope: screen`. |
| `codex context-handoff workers decide <id> keep\|reuse --reason "..."` | Records the decision; nothing is done to the worker. |
| `codex context-handoff workers decide <id> close --reason "..." --result <where its result is>` | Ends a worker only if it is in the manifest, still owned with the same start time, pid and program, idle or done (`--cancel-working` for a busy, blocked or erroring worker after you decided its work can be dropped), has no live workers of its own, and has saved evidence. Right before the kill it saves a complete screen and history read and checks everything again; it then confirms the worker ended. A finished worker is recorded as `already-finished`. Running it again never kills twice. |
| `codex context-handoff workers done` | Needs saved evidence and a decision for every manifest worker that is live or finished, and no worker still running that was to be closed; writes `<state>.workers-report.json` and `.md`. |
| `codex context-handoff restored --token <token>` | Confirms the restore (see above). |
| `codex context-handoff status` | Stages, attempts, held messages and what is missing. |

Workers that keep running are kept and reviewed; the fresh chat does not wait for their results. Finished workers are not removed from Herdr (`hc rm`), so their screens and history stay available; cleanup means ending workers that are no longer needed, not deleting their history.

## Recover a handoff

If the plugin cannot confirm the active terminal or an empty prompt, it leaves the handoff note available for manual recovery. Find the latest `.md` and its matching `.log` and `.attempts.json` in the plugin data directory. A `.received` file names the chat that received the continuation, `.restored` that it completed the checklist, `.cancelled` means you turned the handoff off or a prompt was submitted before the continuation, and `.closed` means no note was written or the switch stopped (the reason is in `.closed`; the `.log` records each step with timestamps, including the status, Herdr turn, and prompt state seen after `/clear`). `.held.jsonl` keeps worker messages that never reached a chat. Check whether the continuation already started, then read the note in a fresh chat in the same worktree if needed. Do not retry blindly when the terminal shows a prompt or approval dialog.

## Run the tests

From the repository root:

```bash
python3 -B -m unittest discover -s plugins/codex/codex-context-handoff/tests
```

`tests/test_e2e.py` runs whole handoffs with the real hook script and a fake `hc` (`tests/fake_hc.py`) first on `PATH`, in temporary plugin data and Codex home directories; it never talks to a real Herdr core.
