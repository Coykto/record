"""Long-running per-capture supervisor process.

Legacy entrypoint for the foreground ``python -m record.supervisor`` path that
specs 001 and 002 stood up. Slice 2 of spec 003 dissolved the supervisor's
per-capture orchestration into :class:`record.capture.CaptureSession` so the
daemon can reuse it; this module is now a thin synchronous wrapper that
drives one ``CaptureSession`` through start → wait-for-stopped → stop, plus
the SIGTERM-forwards-to-stop behavior the spec-001 integration suite expects.

The ``record start`` / ``record stop`` CLI commands no longer spawn this
module — spec 003 slice 2 routes them through the daemon's control socket
instead. ``python -m record.supervisor`` remains reachable as an offline test
path and is exercised by the integration suite.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import threading
from pathlib import Path

from . import paths, state
from .capture import CaptureFailedToStart, CaptureSession, _utcnow_iso
from .logging_setup import configure_logging, get_logger

_EXIT_OK = 0
_EXIT_BINARY_MISSING = 10
_EXIT_BINARY_ABNORMAL = 11

_log = get_logger("record.supervisor")


async def _run_session(
    *,
    output_path: Path,
    sample_rate: int,
    bit_depth: int,
    channels: int,
    video_output_path: Path | None,
    daemon_log_path: Path,
) -> int:
    """Drive one :class:`CaptureSession` for the foreground supervisor path.

    Returns the desired process exit code so :func:`main` can sys.exit it.
    """
    session = CaptureSession(
        output_path=output_path,
        video_output_path=video_output_path,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=channels,
        daemon_log_path=daemon_log_path,
        owner_pid=os.getpid(),
    )

    # SIGTERM/SIGINT handler: forward a ``stop`` via the session. Mirrors the
    # legacy supervisor's behavior — the only signal-handling site in this
    # process is here. We use a threading.Event so the handler is signal-safe
    # (asyncio.Event.set is not), and a small bridge task to forward into the
    # event loop.
    stop_requested = threading.Event()
    loop = asyncio.get_running_loop()
    stop_future: asyncio.Future[None] = loop.create_future()

    def _handle_signal(signum: int, _frame: object) -> None:
        if stop_requested.is_set():
            return
        stop_requested.set()
        _log.info("sigterm_received", signal=signum)
        # Schedule the future-resolve on the loop thread.
        try:
            loop.call_soon_threadsafe(_finalize_stop_future)
        except RuntimeError:  # pragma: no cover - defensive
            pass

    def _finalize_stop_future() -> None:
        if not stop_future.done():
            stop_future.set_result(None)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        await session.start()
    except CaptureFailedToStart as exc:
        warnings = exc.final_state.get("warnings", [])
        binary_missing = any(
            isinstance(w, dict)
            and (
                "capture binary missing" in (w.get("message") or "").lower()
                or "binary missing" in (w.get("message") or "").lower()
            )
            for w in warnings
        )
        if binary_missing:
            return _EXIT_BINARY_MISSING
        return _EXIT_BINARY_ABNORMAL

    # Wait for either a user-initiated stop (SIGTERM) or the binary stopping
    # on its own (system-event-triggered shutdown).
    stopped_wait = asyncio.create_task(session.stopped_event.wait())
    sig_wait = asyncio.create_task(stop_future)
    try:
        await asyncio.wait(
            {stopped_wait, sig_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for t in (stopped_wait, sig_wait):
            if not t.done():
                t.cancel()

    final = await session.stop()

    # The session set final=True. If the binary exited abnormally (its read
    # loop saw no `stopped`), translate that into the legacy exit code so the
    # spec-001 integration suite continues to see the same signal.
    if any(
        isinstance(w, dict) and "abnormally" in (w.get("message") or "")
        for w in final.get("warnings", [])
    ):
        return _EXIT_BINARY_ABNORMAL
    return _EXIT_OK


def run(
    output_path: Path,
    sample_rate: int,
    bit_depth: int,
    channels: int,
    video_output_path: Path | None = None,
) -> int:
    """Synchronous entry point used by ``python -m record.supervisor``."""
    configure_logging()
    resolved = paths.ensure_dirs()

    _log.info(
        "supervisor_starting",
        output_path=str(output_path),
        video_output_path=str(video_output_path) if video_output_path else None,
        pid=os.getpid(),
    )

    try:
        rc = asyncio.run(
            _run_session(
                output_path=output_path,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
                channels=channels,
                video_output_path=video_output_path,
                daemon_log_path=resolved.daemon_log,
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.error(
            "supervisor_crashed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        # Best-effort: leave a final state file behind so `record stop`
        # (legacy path) doesn't hang on a missing snapshot.
        try:
            state.write_state(
                {
                    "pid": os.getpid(),
                    "final": True,
                    "warnings": [
                        {
                            "timestamp": _utcnow_iso(),
                            "source": None,
                            "message": f"supervisor crashed: {exc}",
                        }
                    ],
                    "last_event_at": _utcnow_iso(),
                }
            )
        except Exception:
            pass
        return _EXIT_BINARY_ABNORMAL

    _log.info("supervisor_exiting", return_code=rc)
    return rc


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record.supervisor",
        description="Legacy foreground supervisor for the Swift capture binary.",
    )
    parser.add_argument(
        "--output-path",
        required=True,
        type=Path,
        help="Absolute path of the WAV file the binary will eventually write.",
    )
    parser.add_argument(
        "--video-output-path",
        required=False,
        default=None,
        type=Path,
        help=(
            "Absolute path of the MP4 file the binary will eventually write. "
            "When omitted, video capture is skipped entirely."
        ),
    )
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--bit-depth", type=int, default=16)
    parser.add_argument("--channels", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_path = args.output_path
    if not output_path.is_absolute():
        output_path = output_path.resolve()
    video_output_path: Path | None = args.video_output_path
    if video_output_path is not None and not video_output_path.is_absolute():
        video_output_path = video_output_path.resolve()
    return run(
        output_path=output_path,
        sample_rate=args.sample_rate,
        bit_depth=args.bit_depth,
        channels=args.channels,
        video_output_path=video_output_path,
    )


if __name__ == "__main__":
    sys.exit(main())
