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

Invoke from the repo root:

```
python3 .claude/skills/implement/scripts/orchestrate.py \
  --tasks-file <absolute path to tasks.md> \
  --queue <comma-separated slice numbers from step 2> \
  [--dry-run]
```

The script emits one JSON event per major step on stdout (e.g. `event: start_slice`, `event: call`, `event: after_call`, `event: retry`, `event: after_retry`, `event: drained_slice`, `status: ok|stall|timeout|error`). The subprocess `claude -p` calls inherit the terminal's stdout, so the user sees their progress live; the script's own JSON events are interleaved.

If `--dry-run` was set, the script prints a single JSON object containing the queue, per-slice currently-unchecked counts, and `expected_calls_min` / `expected_calls_max`, then exits 0. Show the queue + range to the user, then stop — do not proceed to a real run unless the user asks.

### 4. Handle the script's outcome

- **Exit 0 (`status: "ok"`)**: re-read `tasks.md`. If any indented sub-item is still unchecked, you may rebuild the queue (step 2) and invoke the script again — but only up to **3 outer passes total** for this skill invocation. If passes are exhausted while work remains, stop and report.
- **Exit 2 (`status: "stall"`)**: the last JSON object on stdout has `slice`, `unchecked_at_slice_start`, `unchecked_after_first_call`, `unchecked_after_retry`, exit codes from both attempts, and short stderr tails. Surface a concise summary to the user via the `AskUserQuestion` tool — include the unchecked-count progression (e.g. "Slice 4: started with 4 unchecked, first call left 2, retry left 2 — no further progress"). Options:
  - **Skip this slice and continue** — rebuild the queue excluding the stalled slice, invoke the script again with that smaller queue.
  - **Retry once more** — rebuild the queue from current state (which still includes the stalled slice) and invoke the script again. This costs two more `claude -p` calls before stalling again.
  - **Stop** — report and let the user investigate manually.
- **Exit 3 (`status: "timeout"`)**: report the slice and offer the user the same three options as a stall.
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
- For multiple-choice prompts to the user (stall handling), use the `AskUserQuestion` tool, not free-text prompts.
