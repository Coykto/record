#!/usr/bin/env python3
"""
Sequential `claude -p` runner for `/implement` with stall detection.

For each slice in the queue, issues exactly one `claude -p` call whose prompt
instructs the fresh session to implement *every* unchecked sub-item under
Slice #N. The orchestrator monitors the call for liveness — combining
stdout/stderr activity and changes to the slice's unchecked count in
tasks.md — and kills the call if neither has progressed for `--stall-timeout`
seconds. If a call dies (either by exiting non-zero or being killed on stall)
without draining the slice, the script retries once in another fresh session;
if the retry also fails to drain, the script emits a structured stall and
exits.

Context discipline: subprocess stdout/stderr is captured to per-call log files
under `LOG_DIR` (see constant below) so the parent agent's context isn't
polluted by the subagent's full transcript. Only compact JSON events are
emitted on the script's stdout. On stall/timeout the status JSON includes the
log file path AND a short tail of the log so the parent can surface it to the
user without re-reading the whole transcript.

Authentication: subprocess `claude -p` calls inherit the user's logged-in CLI
session. ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are explicitly stripped from
the child env so a stray shell-level key cannot override the subscription.

Exit codes:
  0 — every slice in the queue drained to zero unchecked sub-items
  1 — invocation error (bad args, missing tasks file)
  2 — stall: a slice did not drain even after one retry
  3 — hard wall-clock timeout on a `claude -p` call
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
# All per-call subprocess transcripts land here. Grouped per orchestrator run
# into a subdirectory `{spec-dir-name}-{run-ts}/`. Not in .gitignore by default
# — the sibling .gitignore in .claude/skills/implement/ handles that.
LOG_DIR = SCRIPT_DIR.parent / "logs"

SLICE_HEADING = re.compile(
    r"^#{2,}\s+(?:-\s+\[[ xX]\]\s+)?(?:\*\*)?Slice\s+(\d+)\b",
    re.MULTILINE,
)
SUB_UNCHECKED = re.compile(
    r"^\s*-\s+\[\s\](?!\s*\*\*Slice\s+\d)",
    re.MULTILINE,
)


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def parse_slice_unchecked(text: str) -> dict[int, int]:
    """Return {slice_num: count_of_unchecked_sub_items}.

    Counts sub-item checkboxes (with or without leading indentation); the slice
    rollup line (`- [ ] **Slice N: ...**`) is excluded via negative lookahead.
    Supports both `## Slice N — ...` and `### - [ ] **Slice N: ...**` headings.
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


def build_command(tasks_file: Path, slice_num: int) -> list[str]:
    prompt = (
        f"/awos:implement @{tasks_file} Slice #{slice_num} — "
        f"implement every unchecked sub-item under this slice in this session, "
        f"not just the next one. Don't stop until the slice is fully checked off."
    )
    # --output-format=stream-json (with --verbose, required for stream-json under -p)
    # makes the subagent emit one JSON line per event (system init, assistant turn,
    # tool use, tool result). Without it, `claude -p` only prints the final answer at
    # exit, leaving the orchestrator's stall detector blind for the entire run — a
    # long slice would look identical to a hang.
    return [
        "claude",
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the whole process group so child processes
    (claude's tools, shells it spawned, etc.) die too — not just the top-level
    `claude` binary. Requires the Popen to have been started with
    `start_new_session=True`."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _tail(log_path: Path, n_lines: int = 40, max_bytes: int = 4000) -> str:
    """Return the last n_lines of a log file, capped at max_bytes, for
    surfacing to the parent agent on stall/timeout. We intentionally keep this
    small — full transcripts stay in the log file on disk."""
    try:
        text = log_path.read_text(errors="replace")
    except Exception as e:
        return f"<could not read log: {e}>"
    lines = text.splitlines()[-n_lines:]
    tail = "\n".join(lines)
    if len(tail) > max_bytes:
        tail = "...\n" + tail[-max_bytes:]
    return tail


def run_call(
    cmd: list[str],
    *,
    log_path: Path,
    stall_timeout: int,
    hard_timeout: int,
    poll_interval: int,
    tasks_file: Path,
    slice_num: int,
    phase: str,
) -> tuple[Optional[int], str]:
    """Run a single `claude -p` invocation with stall detection.

    Captures subprocess stdout & stderr into `log_path`. Monitors liveness by
    combining output activity with changes to the slice's unchecked count in
    tasks.md. Kills the process group if no signal of either kind for
    `stall_timeout` seconds, or if elapsed exceeds `hard_timeout` seconds.

    Returns (exit_code, termination_reason):
      * `exit_code` is the process return code when termination_reason ∈
        {"exit", "stall_kill"} (negative for signals).
      * `exit_code` is None when termination_reason == "hard_timeout".
      * `termination_reason` ∈ {"exit", "stall_kill", "hard_timeout"}.
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("w", encoding="utf-8")
    log_fh.write(
        f"# claude -p log\n"
        f"# slice={slice_num} phase={phase}\n"
        f"# cmd={cmd}\n"
        f"# started_at={datetime.now(timezone.utc).isoformat()}\n"
        f"# stall_timeout_s={stall_timeout} hard_timeout_s={hard_timeout} "
        f"poll_interval_s={poll_interval}\n\n"
    )
    log_fh.flush()

    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
        start_new_session=True,  # own process group; lets _kill_process_group reap children
    )

    last_activity = time.monotonic()
    activity_lock = threading.Lock()

    def pump(stream, label: str) -> None:
        nonlocal last_activity
        prefix = "" if label == "stdout" else "[stderr] "
        for line in iter(stream.readline, ""):
            with activity_lock:
                last_activity = time.monotonic()
                log_fh.write(prefix + line)
                log_fh.flush()
        try:
            stream.close()
        except Exception:
            pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)
    t_out.start()
    t_err.start()

    start = time.monotonic()
    last_unchecked = unchecked_for(tasks_file, slice_num)
    last_progress_time = start
    stall_warned = False  # emit stall_warning at most once per silent stretch

    try:
        while True:
            try:
                rc = proc.wait(timeout=poll_interval)
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                log_fh.write(f"\n# exited_at={datetime.now(timezone.utc).isoformat()} rc={rc}\n")
                return rc, "exit"
            except subprocess.TimeoutExpired:
                pass

            now = time.monotonic()
            elapsed = now - start

            if elapsed > hard_timeout:
                emit({
                    "event": "hard_timeout",
                    "slice": slice_num,
                    "phase": phase,
                    "elapsed_s": int(elapsed),
                    "hard_timeout_s": hard_timeout,
                    "log_file": str(log_path),
                })
                _kill_process_group(proc)
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                log_fh.write(
                    f"\n# killed_at={datetime.now(timezone.utc).isoformat()} "
                    f"reason=hard_timeout elapsed_s={int(elapsed)}\n"
                )
                return None, "hard_timeout"

            current_unchecked = unchecked_for(tasks_file, slice_num)
            if current_unchecked != last_unchecked:
                emit({
                    "event": "progress",
                    "slice": slice_num,
                    "phase": phase,
                    "unchecked_remaining": current_unchecked,
                    "delta": last_unchecked - current_unchecked,
                    "elapsed_s": int(elapsed),
                })
                last_unchecked = current_unchecked
                last_progress_time = now
                stall_warned = False

            with activity_lock:
                since_stdout = now - last_activity
            since_file = now - last_progress_time
            silent_for = min(since_stdout, since_file)

            if not stall_warned and silent_for > stall_timeout / 2:
                emit({
                    "event": "stall_warning",
                    "slice": slice_num,
                    "phase": phase,
                    "silent_for_s": int(silent_for),
                    "stall_timeout_s": stall_timeout,
                    "elapsed_s": int(elapsed),
                })
                stall_warned = True

            if silent_for > stall_timeout:
                emit({
                    "event": "kill_on_stall",
                    "slice": slice_num,
                    "phase": phase,
                    "silent_for_s": int(silent_for),
                    "stall_timeout_s": stall_timeout,
                    "elapsed_s": int(elapsed),
                    "log_file": str(log_path),
                })
                _kill_process_group(proc)
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                log_fh.write(
                    f"\n# killed_at={datetime.now(timezone.utc).isoformat()} "
                    f"reason=stall_kill silent_for_s={int(silent_for)}\n"
                )
                return proc.returncode, "stall_kill"
    finally:
        try:
            log_fh.close()
        except Exception:
            pass


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
        "--stall-timeout",
        type=int,
        default=300,
        help="Kill a `claude -p` call if no stdout/stderr AND no tasks.md progress for this "
             "many seconds (default: 300 = 5 min). A stall warning is emitted at half this value.",
    )
    ap.add_argument(
        "--hard-timeout",
        type=int,
        default=7200,
        help="Absolute wall-clock cap per `claude -p` call, even if it's still emitting output "
             "(default: 7200 = 2 h). Safety net; stall detection is the primary mechanism.",
    )
    ap.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="How often (seconds) the orchestrator checks the running call for stall/progress "
             "(default: 30). Also the max latency between subprocess exit and event emission.",
    )
    ap.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help=f"Override the per-run log directory parent (default: {LOG_DIR})",
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

    if args.stall_timeout <= 0 or args.hard_timeout <= 0 or args.poll_interval <= 0:
        emit({"status": "error", "message": "timeouts and poll-interval must be positive"})
        return 1
    if args.poll_interval >= args.stall_timeout:
        emit({"status": "error", "message": "--poll-interval must be less than --stall-timeout"})
        return 1

    initial_counts = parse_slice_unchecked(tasks_file.read_text())

    log_root = (args.log_dir or LOG_DIR).expanduser().resolve()

    if args.dry_run:
        slice_plan = [
            {
                "slice": s,
                "unchecked_in_slice_now": initial_counts.get(s, 0),
                "sample_command": build_command(tasks_file, s),
            }
            for s in queue
        ]
        slices_with_work = sum(1 for s in queue if initial_counts.get(s, 0) > 0)
        emit(
            {
                "status": "dry-run",
                "tasks_file": str(tasks_file),
                "queue": queue,
                "stall_timeout_s": args.stall_timeout,
                "hard_timeout_s": args.hard_timeout,
                "poll_interval_s": args.poll_interval,
                "log_dir_root": str(log_root),
                "expected_calls_min": slices_with_work,
                "expected_calls_max": slices_with_work * 2,
                "note": "one `claude -p` call per slice; up to one retry per slice if it doesn't drain on the first call",
                "slice_plan": slice_plan,
            }
        )
        return 0

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_log_dir = log_root / f"{tasks_file.parent.name}-{run_ts}"

    emit(
        {
            "event": "run_start",
            "tasks_file": str(tasks_file),
            "queue": queue,
            "stall_timeout_s": args.stall_timeout,
            "hard_timeout_s": args.hard_timeout,
            "poll_interval_s": args.poll_interval,
            "log_dir": str(run_log_dir),
        }
    )

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

        cmd = build_command(tasks_file, slice_num)

        total_calls += 1
        first_log = run_log_dir / f"slice-{slice_num:03d}-attempt-1.log"
        emit(
            {
                "event": "call",
                "slice": slice_num,
                "phase": "first",
                "total_calls": total_calls,
                "unchecked_in_slice_before_call": before_slice,
                "log_file": str(first_log),
            }
        )

        rc, reason = run_call(
            cmd,
            log_path=first_log,
            stall_timeout=args.stall_timeout,
            hard_timeout=args.hard_timeout,
            poll_interval=args.poll_interval,
            tasks_file=tasks_file,
            slice_num=slice_num,
            phase="first",
        )

        if reason == "hard_timeout":
            emit(
                {
                    "status": "timeout",
                    "slice": slice_num,
                    "phase": "first",
                    "total_calls": total_calls,
                    "drained_slices": drained_slices,
                    "skipped_slices": skipped_slices,
                    "log_file": str(first_log),
                    "log_tail": _tail(first_log),
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
                "termination_reason": reason,
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
        retry_log = run_log_dir / f"slice-{slice_num:03d}-attempt-2.log"
        emit(
            {
                "event": "retry",
                "slice": slice_num,
                "unchecked_remaining": after_call,
                "total_calls": total_calls,
                "log_file": str(retry_log),
            }
        )
        rc2, reason2 = run_call(
            cmd,
            log_path=retry_log,
            stall_timeout=args.stall_timeout,
            hard_timeout=args.hard_timeout,
            poll_interval=args.poll_interval,
            tasks_file=tasks_file,
            slice_num=slice_num,
            phase="retry",
        )
        if reason2 == "hard_timeout":
            emit(
                {
                    "status": "timeout",
                    "slice": slice_num,
                    "phase": "retry",
                    "total_calls": total_calls,
                    "drained_slices": drained_slices,
                    "skipped_slices": skipped_slices,
                    "log_file": str(retry_log),
                    "log_tail": _tail(retry_log),
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
                "termination_reason": reason2,
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
                "first_attempt_termination": reason,
                "retry_exit_code": rc2,
                "retry_termination": reason2,
                "first_log_file": str(first_log),
                "first_log_tail": _tail(first_log),
                "retry_log_file": str(retry_log),
                "retry_log_tail": _tail(retry_log),
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
            "log_dir": str(run_log_dir),
            "remaining_unchecked_per_slice": parse_slice_unchecked(tasks_file.read_text()),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
