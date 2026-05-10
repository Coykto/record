---
description: Use when the user wants to drive a tasks.md spec to completion by repeatedly invoking the per-slice implementation flow in fresh Claude sessions (e.g. "/implement context/spec/.../tasks.md", "run all pending slices to completion", "burn down the remaining tasks list"). Wraps a queue of `claude -p` subprocess calls with stall detection.
argument-hint: <path to tasks.md> [--dry-run]
---

Drive the slice-by-slice implementation of a `tasks.md` file to completion by invoking `claude -p` in fresh subprocess sessions — one invocation per slice — using `scripts/orchestrate.py`. Each call's prompt instructs the fresh session to implement the entire slice in one go; if the slice does not fully drain, the script retries once in another fresh session, then stalls.

The user-provided argument is:

$ARGUMENTS

If `$ARGUMENTS` is empty, ask the user for the path to the spec's `tasks.md` and stop.

## Constraints

- You do **not** write code or edit `tasks.md` yourself. The actual implementation is performed by the subprocess `claude -p` calls — each of which is a fresh Claude session that delegates the per-slice work via the existing `/awos:implement` flow.
- All `claude -p` calls must use the user's Claude subscription. The orchestrator script strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the child env so a stray shell-level key cannot override the subscription. Do not set those variables yourself.
- Cap the work at **3 outer passes** through the rebuild-queue-and-run loop. If pending sub-items remain after 3 passes, stop and surface the situation to the user — never let this run unbounded.

## Steps

### 1. Validate input

- Parse `$ARGUMENTS`. The first positional token is the path to `tasks.md`. The literal flag `--dry-run` may appear anywhere in `$ARGUMENTS`; if present, capture it and pass it through to the script.
- Resolve the path to absolute. If the file does not exist, stop and tell the user the resolved path that was not found.

### 2. Parse `tasks.md` and build the queue

- Read the file.
- Each slice begins with a `## Slice <N> — ...` heading. Within a slice, count only the **indented sub-item checkboxes** (lines matching `^\s+- \[ \]`) — exclude the slice rollup line `- [ ] **Slice N: ...**` (which has no leading whitespace). The orchestrator counts the same way for stall detection.
- Build the queue: in slice-number order, list every slice that still has at least one unchecked sub-item — **one entry per slice**, regardless of how many sub-items it contains.
  - Example: slice 1 has 1 unchecked, slice 2 has 3, slice 3 has 0, slice 4 has 2 → queue is `1,2,4`.
  - Rationale: the prompt sent to each `claude -p` call (built by `orchestrate.py`) explicitly tells the fresh session to implement *every* unchecked sub-item under the slice in that single session, so one call should drain a slice. The retry exists for the case where the session ran out of context, hit an error, or otherwise stopped short.
- If the queue is empty, tell the user "Nothing to implement — all sub-items are checked." and stop.
- If the queue is non-empty, briefly tell the user: the slice list and the expected call count — minimum `N` (one call per slice if every slice drains on the first try) and maximum `2N` (every slice retries once). Example: "Queue: slices 1, 2, 4 — 3 to 6 `claude -p` calls expected."

### 3. Run the orchestrator

A single orchestrator run can span **many hours** — the script issues one `claude -p` call per slice, each with a 2 h hard cap by default. Claude Code's `Bash` tool has a 10-minute wall-clock maximum (`timeout` ≤ 600000 ms), so a foreground invocation would be killed long before the run completes, orphaning the `claude -p` children. **You MUST launch the orchestrator in background mode and stream its events with `Monitor`.**

Step 3a — launch in background. Invoke from the repo root via the `Bash` tool with `run_in_background: true`:

```
python3 .claude/skills/implement/scripts/orchestrate.py \
  --tasks-file <absolute path to tasks.md> \
  --queue <comma-separated slice numbers from step 2> \
  [--dry-run]
```

The `Bash` call returns a shell ID. Do **not** sleep, poll, or call `BashOutput` in a loop.

Step 3b — stream events with `Monitor`. Pass the shell ID to `Monitor` so each JSON event line emitted by the orchestrator becomes a notification. Keep reading until you see a line whose JSON has a `status` field (one of `ok | stall | timeout | error`) — that is the terminal event. While streaming, you do not need to do anything for routine events (`event: run_start | start_slice | call | progress | after_call | retry | drained_slice | skip_slice`); just collect them so the final report has accurate totals. React immediately to `event: stall_warning` (informational — note the silence duration), `event: kill_on_stall`, and `event: hard_timeout` (the orchestrator will continue with the per-slice retry or surface a status terminator).

The script's events are compact by design. Subprocess `claude -p` stdout/stderr is **not** in the event stream — it is captured into per-call log files under `.claude/skills/implement/logs/{spec-dir}-{run-ts}/slice-{NNN}-attempt-{1|2}.log` (path included in every `event: call` and in the final status JSON). This keeps your context clean. The user can `tail -f` a log in another terminal to watch a call live; on stall/timeout the script includes the log path **and a ~4 KB tail of the log** in the status object so you can surface both to the user.

Liveness is monitored every `--poll-interval` seconds (default 30 s) by combining two signals: (a) the time since the last stdout/stderr line from `claude -p`, and (b) the time since the slice's unchecked count in `tasks.md` last changed. If the more-recent of those exceeds `--stall-timeout` (default 300 s = 5 min), the orchestrator kills the call's whole process group and the per-slice retry fires one more fresh session. A separate `--hard-timeout` (default 7200 s = 2 h) is a safety net that fires even if the subagent is still emitting output.

If `--dry-run` was set, the script prints a single JSON object containing the queue, per-slice currently-unchecked counts, and `expected_calls_min` / `expected_calls_max`, then exits 0 within milliseconds. For a dry-run you may invoke synchronously (no `run_in_background`). Show the queue + range to the user, then stop — do not proceed to a real run unless the user asks.

### 4. Handle the script's outcome

- **Exit 0 (`status: "ok"`)**: re-read `tasks.md`. If any indented sub-item is still unchecked, you may rebuild the queue (step 2) and invoke the script again — but only up to **3 outer passes total** for this skill invocation. If passes are exhausted while work remains, stop and report.
- **Exit 2 (`status: "stall"`)**: the last JSON object on stdout has `slice`, `unchecked_at_slice_start`, `unchecked_after_first_call`, `unchecked_after_retry`, exit codes and termination reasons from both attempts (`exit` / `stall_kill` / `hard_timeout`), and `first_log_file` / `retry_log_file` paths plus `first_log_tail` / `retry_log_tail`. Surface a concise summary to the user via the `AskUserQuestion` tool — include the unchecked-count progression (e.g. "Slice 4: started with 4 unchecked, first call left 2 (killed on stall), retry left 2 (exited normally) — no further progress") **and the log file paths** so the user can inspect what the subagent was doing. Options:
  - **Skip this slice and continue** — rebuild the queue excluding the stalled slice, invoke the script again with that smaller queue.
  - **Retry once more** — rebuild the queue from current state (which still includes the stalled slice) and invoke the script again. This costs two more `claude -p` calls before stalling again.
  - **Stop** — report and let the user investigate manually using the log file paths.
- **Exit 3 (`status: "timeout"`)**: the call exceeded the hard wall-clock cap (`--hard-timeout`, default 2 h) even though it was still emitting output. The status object includes `log_file` and `log_tail`. Report the slice + log path and offer the user the same three options as a stall.
- **Exit 1 or other non-zero**: print the script's last JSON line to the user (it will contain a `message` field) and stop.

### 5. Final report

When the skill ends — whether by completion, exhaustion of outer passes, stall after user said stop, or hard error — report:

- Total `claude -p` calls executed (sum of `total_calls` across passes).
- Outer passes used (1, 2, or 3).
- Slices fully drained (zero unchecked sub-items remain).
- Slices with remaining work, with their pending sub-item counts.

## Notes

- Do **not** try to bypass the 3-outer-pass cap, even if the user asks mid-run — instead, end the skill and let them re-invoke it. The cap exists so that a misbehaving `/awos:implement` flow cannot burn the user's subscription quota in a tight loop.
- Do **not** set `ANTHROPIC_API_KEY` for the subprocess. The script strips it; respect that.
- Do **not** use the `Task` tool to delegate the implementation work — that would run inside this same session, defeating the point of fresh `/clear`-equivalent sessions per invocation.
- Do **not** launch the orchestrator as a foreground `Bash` call. The 10-minute `Bash` tool cap will kill the script and orphan its `claude -p` children mid-slice. Always use `run_in_background: true` + `Monitor` (see step 3).
- For multiple-choice prompts to the user (stall handling), use the `AskUserQuestion` tool, not free-text prompts.
