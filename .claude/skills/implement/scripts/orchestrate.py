#!/usr/bin/env python3
"""
Sequential `claude -p` runner for `/implement`.

Receives a queue of slice numbers (one entry per slice) and a tasks.md path.
For each slice it issues exactly one `claude -p` call whose prompt instructs
the fresh session to implement *every* unchecked sub-item under Slice #N and
mark each `[x]`. After the call the script checks whether the slice's
unchecked count reached zero. If not, the script issues exactly one retry
in another fresh session; if the retry also fails to drain the slice, the
script emits a structured stall and exits.

Authentication: subprocess `claude -p` calls inherit the user's logged-in CLI
session. ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are explicitly stripped from
the child env so a stray shell-level key cannot override the subscription.

Exit codes:
  0 — every slice in the queue drained to zero unchecked sub-items
  1 — invocation error (bad args, missing tasks file)
  2 — stall: a slice did not drain even after one retry
  3 — timeout on a `claude -p` call
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

SLICE_HEADING = re.compile(r"^##\s+Slice\s+(\d+)\b", re.MULTILINE)
SUB_UNCHECKED = re.compile(r"^\s+-\s+\[\s\]", re.MULTILINE)


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def parse_slice_unchecked(text: str) -> dict[int, int]:
    """Return {slice_num: count_of_unchecked_sub_items}.

    Counts only indented (sub-item) checkboxes; the slice rollup line
    (`- [ ] **Slice N: ...**` with no leading whitespace) is excluded.
    """
    lines = text.splitlines()
    starts: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = SLICE_HEADING.match(line)
        if m:
            starts.append((int(m.group(1)), i))

    result: dict[int, int] = {}
    for idx, (num, start) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start:end])
        result[num] = len(SUB_UNCHECKED.findall(body))
    return result


def unchecked_for(tasks_file: Path, slice_num: int) -> int:
    return parse_slice_unchecked(tasks_file.read_text()).get(slice_num, 0)


def build_command(tasks_file: Path, slice_num: int, permission_mode: str) -> list[str]:
    prompt = (
        f"/awos:implement @{tasks_file} Slice #{slice_num} — "
        f"implement every unchecked sub-item under this slice in this session, "
        f"not just the next one. Don't stop until the slice is fully checked off."
    )
    return ["claude", "-p", prompt, "--permission-mode", permission_mode]


def run_once(
    cmd: list[str],
    timeout: int,
) -> tuple[Optional[int], str]:
    """Run a `claude -p` invocation. stdout is inherited (streams to the
    parent terminal so the user sees progress). stderr is captured for
    stall reporting. Returns (exit_code_or_None_on_timeout, captured_stderr).
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    try:
        proc = subprocess.run(
            cmd,
            env=env,
            timeout=timeout,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        captured = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return None, captured

    return proc.returncode, proc.stderr or ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Sequential claude -p runner for /implement.")
    ap.add_argument("--tasks-file", required=True, help="Absolute path to the spec's tasks.md")
    ap.add_argument(
        "--queue",
        required=True,
        help="Comma-separated slice numbers — one entry per slice. The script issues one "
             "`claude -p` call per slice; if the slice does not fully drain, it retries once "
             "in another fresh session, then stalls.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print the planned slice list and exit")
    ap.add_argument(
        "--per-call-timeout",
        type=int,
        default=1800,
        help="Per-`claude -p` wall-clock timeout in seconds (default: 1800)",
    )
    ap.add_argument(
        "--permission-mode",
        default="acceptEdits",
        help="Permission mode passed to each `claude -p` (default: acceptEdits)",
    )
    args = ap.parse_args()

    tasks_file = Path(args.tasks_file).expanduser().resolve()
    if not tasks_file.is_file():
        emit({"status": "error", "message": f"tasks file not found: {tasks_file}"})
        return 1

    try:
        queue = [int(x.strip()) for x in args.queue.split(",") if x.strip()]
    except ValueError as e:
        emit({"status": "error", "message": f"invalid --queue: {e}"})
        return 1

    if not queue:
        emit({"status": "error", "message": "queue is empty"})
        return 1

    initial_counts = parse_slice_unchecked(tasks_file.read_text())

    if args.dry_run:
        slice_plan = [
            {
                "slice": s,
                "unchecked_in_slice_now": initial_counts.get(s, 0),
                "sample_command": build_command(tasks_file, s, args.permission_mode),
            }
            for s in queue
        ]
        slices_with_work = sum(1 for s in queue if initial_counts.get(s, 0) > 0)
        emit(
            {
                "status": "dry-run",
                "tasks_file": str(tasks_file),
                "queue": queue,
                "permission_mode": args.permission_mode,
                "per_call_timeout": args.per_call_timeout,
                "expected_calls_min": slices_with_work,
                "expected_calls_max": slices_with_work * 2,
                "note": "one `claude -p` call per slice; up to one retry per slice if it doesn't drain on the first call",
                "slice_plan": slice_plan,
            }
        )
        return 0

    total_calls = 0
    drained_slices: list[int] = []
    skipped_slices: list[int] = []

    for slice_idx, slice_num in enumerate(queue, start=1):
        before_slice = unchecked_for(tasks_file, slice_num)
        if before_slice == 0:
            skipped_slices.append(slice_num)
            emit(
                {
                    "event": "skip_slice",
                    "queue_position": slice_idx,
                    "slice": slice_num,
                    "reason": "no unchecked sub-items in this slice",
                }
            )
            continue

        emit(
            {
                "event": "start_slice",
                "queue_position": slice_idx,
                "queue_size": len(queue),
                "slice": slice_num,
                "unchecked_at_slice_start": before_slice,
            }
        )

        cmd = build_command(tasks_file, slice_num, args.permission_mode)

        total_calls += 1
        emit(
            {
                "event": "call",
                "slice": slice_num,
                "phase": "first",
                "total_calls": total_calls,
                "unchecked_in_slice_before_call": before_slice,
                "command": cmd,
            }
        )

        rc, stderr_tail = run_once(cmd, args.per_call_timeout)
        if rc is None:
            emit(
                {
                    "status": "timeout",
                    "slice": slice_num,
                    "phase": "first",
                    "total_calls": total_calls,
                    "drained_slices": drained_slices,
                    "skipped_slices": skipped_slices,
                    "stderr_tail": stderr_tail[-1000:],
                }
            )
            return 3

        after_call = unchecked_for(tasks_file, slice_num)
        emit(
            {
                "event": "after_call",
                "slice": slice_num,
                "phase": "first",
                "unchecked_in_slice_before_call": before_slice,
                "unchecked_in_slice_after_call": after_call,
                "exit_code": rc,
            }
        )

        if after_call == 0:
            drained_slices.append(slice_num)
            emit(
                {
                    "event": "drained_slice",
                    "slice": slice_num,
                    "calls_in_slice": 1,
                    "total_calls": total_calls,
                }
            )
            continue

        total_calls += 1
        emit(
            {
                "event": "retry",
                "slice": slice_num,
                "unchecked_remaining": after_call,
                "total_calls": total_calls,
                "command": cmd,
            }
        )
        rc2, stderr_tail2 = run_once(cmd, args.per_call_timeout)
        if rc2 is None:
            emit(
                {
                    "status": "timeout",
                    "slice": slice_num,
                    "phase": "retry",
                    "total_calls": total_calls,
                    "drained_slices": drained_slices,
                    "skipped_slices": skipped_slices,
                    "stderr_tail": stderr_tail2[-1000:],
                }
            )
            return 3

        after_retry = unchecked_for(tasks_file, slice_num)
        emit(
            {
                "event": "after_retry",
                "slice": slice_num,
                "unchecked_in_slice_after_retry": after_retry,
                "exit_code": rc2,
            }
        )

        if after_retry == 0:
            drained_slices.append(slice_num)
            emit(
                {
                    "event": "drained_slice",
                    "slice": slice_num,
                    "calls_in_slice": 2,
                    "total_calls": total_calls,
                }
            )
            continue

        remaining_queue = queue[slice_idx:]
        emit(
            {
                "status": "stall",
                "slice": slice_num,
                "reason": "slice not fully drained after one retry",
                "unchecked_at_slice_start": before_slice,
                "unchecked_after_first_call": after_call,
                "unchecked_after_retry": after_retry,
                "first_attempt_exit_code": rc,
                "retry_exit_code": rc2,
                "first_stderr_tail": stderr_tail[-500:],
                "retry_stderr_tail": stderr_tail2[-500:],
                "total_calls": total_calls,
                "drained_slices": drained_slices,
                "skipped_slices": skipped_slices,
                "remaining_queue": remaining_queue,
            }
        )
        return 2

    emit(
        {
            "status": "ok",
            "total_calls": total_calls,
            "drained_slices": drained_slices,
            "skipped_slices": skipped_slices,
            "remaining_unchecked_per_slice": parse_slice_unchecked(tasks_file.read_text()),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
