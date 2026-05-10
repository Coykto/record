"""End-to-end integration test for the Swift capture binary.

Spawns the real ``record-capture`` binary with ``--test-silent-sources`` for
~2 seconds and verifies:

1. The JSON-line event sequence on stdout:
   ``ready`` -> ``started`` -> 2 x ``source_attached`` -> ``stopped``.
2. No ``permission_required`` / ``permission_denied`` / ``error`` events leak
   through (synthetic mode must not touch TCC).
3. The resulting WAV file is mono / 16 kHz / 16-bit and its duration is within
   +/-100 ms of the requested capture window.

The test is intentionally a black-box check against the wire protocol, so it
uses only the standard library (``json``, ``subprocess``, ``threading``,
``time``, ``wave``, ``pathlib``). No project Python modules are imported --
that path is covered by the unit tests under ``tests/python/``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import wave
from pathlib import Path

import pytest

# How long (wall-clock seconds) to leave the capture running between
# ``started`` and ``stop``. The WAV duration assertion below uses +/-100 ms
# tolerance against this value.
CAPTURE_WINDOW_SECONDS = 2.0

# Hard ceiling on the whole test (subprocess spawn -> ``stopped`` event). The
# capture window itself is ~2 s; the rest is Swift runloop startup, IO
# buffering, and the wait for the ``stopped`` event after we send ``stop``.
TEST_DEADLINE_SECONDS = 15.0

# Format the orchestrator always negotiates per architecture / Slice 3.
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPWIDTH_BYTES = 2  # 16-bit signed PCM


def test_end_to_end_silent_sources(
    capture_binary: Path, tmp_path: Path
) -> None:
    output_wav = tmp_path / "synthetic.wav"

    proc = subprocess.Popen(
        [str(capture_binary), "--test-silent-sources"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered on the Python side
    )

    events: list[dict[str, object]] = []
    stderr_dump = ""
    stop_timer: threading.Timer | None = None
    # Guard concurrent stdin writes by the main thread (the ``start`` command)
    # and the timer thread (the ``stop`` command).
    stdin_lock = threading.Lock()

    def _send_stop() -> None:
        # Best-effort; if stdin is already closed the daemon will see the
        # absence as EOF and tear down. The main thread asserts on the
        # ``stopped`` event we receive, not on this write succeeding.
        try:
            with stdin_lock:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
                    proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        deadline = time.monotonic() + TEST_DEADLINE_SECONDS

        # 1. Daemon emits ``ready`` on startup, before any command is sent.
        ready_line = proc.stdout.readline()
        if not ready_line:
            pytest.fail("record-capture closed stdout before emitting `ready`")
        ready = json.loads(ready_line)
        assert ready == {"event": "ready"}, (
            f"first event was not `ready`: {ready!r}"
        )

        # 2. Send ``start`` with the format the real orchestrator negotiates.
        start_cmd = {
            "cmd": "start",
            "output_path": str(output_wav),
            "format": {
                "sample_rate": EXPECTED_SAMPLE_RATE,
                "bit_depth": 16,
                "channels": EXPECTED_CHANNELS,
            },
        }
        with stdin_lock:
            proc.stdin.write(json.dumps(start_cmd) + "\n")
            proc.stdin.flush()

        # 3. Collect events until ``stopped``.
        #
        # The stop write has to be issued from a separate thread because the
        # main thread spends most of the capture window blocked inside
        # ``readline()`` waiting for events that won't arrive until *after*
        # we ask the daemon to stop. We arm a one-shot ``threading.Timer``
        # as soon as we see ``started`` so the capture window is measured
        # from the daemon's own start point, not from process spawn.
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                # EOF without ``stopped`` -- daemon crashed or closed stdout.
                pytest.fail(
                    "record-capture closed stdout before emitting `stopped`; "
                    f"events so far: {events!r}"
                )
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON line on daemon stdout: {line!r}")
            events.append(ev)

            if ev.get("event") == "started" and stop_timer is None:
                stop_timer = threading.Timer(CAPTURE_WINDOW_SECONDS, _send_stop)
                stop_timer.daemon = True
                stop_timer.start()

            if ev.get("event") == "stopped":
                break
        else:
            pytest.fail(
                f"never received `stopped` event within {TEST_DEADLINE_SECONDS}s; "
                f"events so far: {events!r}"
            )

        # 4. Drain stdin so the daemon's command loop hits EOF and exits.
        with stdin_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

        # 5. Wait for clean exit.
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "record-capture did not exit within 5s after `stopped` event"
            )

        # Drain stderr for diagnostic context.
        assert proc.stderr is not None
        stderr_dump = proc.stderr.read() or ""

        assert proc.returncode == 0, (
            f"record-capture exited with code {proc.returncode}; "
            f"stderr:\n{stderr_dump}"
        )

        # ------------------------------------------------------------------
        # Event-sequence assertions.
        # ------------------------------------------------------------------
        # ``events`` does not include the initial ``ready`` (we consumed that
        # before sending ``start``). After ``start`` we expect, in order:
        #   started, source_attached(*), source_attached(*), stopped.
        sequence = [ev["event"] for ev in events]
        assert sequence[0] == "started", (
            f"expected first event after `ready` to be `started`, got: {sequence!r}"
        )
        assert sequence[-1] == "stopped", (
            f"expected last event to be `stopped`, got: {sequence!r}"
        )

        # Both sources must attach. ``--test-silent-sources`` emits ``mic``
        # before ``system_audio`` in the current implementation, but order is
        # not load-bearing on the wire (the real-audio path also has a race
        # between SCStream and AVAudioEngine startup) -- sort to be robust.
        attached_sources = sorted(
            ev["source"] for ev in events if ev["event"] == "source_attached"
        )
        assert attached_sources == ["mic", "system_audio"], (
            f"expected both sources to attach, got {attached_sources!r}; "
            f"full sequence: {sequence!r}"
        )

        bad_events = [
            ev
            for ev in events
            if ev["event"] in ("permission_required", "permission_denied", "error")
        ]
        assert not bad_events, (
            f"--test-silent-sources should not emit permission/error events, "
            f"got: {bad_events!r}"
        )

        # ------------------------------------------------------------------
        # ``stopped`` payload sanity.
        # ------------------------------------------------------------------
        stopped = events[-1]
        assert stopped["output_path"] == str(output_wav), (
            f"stopped.output_path mismatch: {stopped['output_path']!r} "
            f"vs {str(output_wav)!r}"
        )
        # The reported duration is best-effort from the daemon's perspective.
        # We give it generous slack (+/-0.5 s) because the round-trip latency
        # between Python's Timer firing and Swift seeing the ``stop`` on
        # stdin is real. The strict +/-100 ms tolerance from the tasks.md
        # acceptance criterion is applied to the WAV file below -- that's
        # the artifact the user actually consumes.
        duration_reported = float(stopped["duration_seconds"])
        assert abs(duration_reported - CAPTURE_WINDOW_SECONDS) <= 0.5, (
            f"daemon-reported duration {duration_reported:.3f}s deviates from "
            f"requested {CAPTURE_WINDOW_SECONDS}s by more than 500 ms"
        )

        # ------------------------------------------------------------------
        # WAV file assertions.
        # ------------------------------------------------------------------
        assert output_wav.exists(), f"WAV not written to {output_wav}"

        with wave.open(str(output_wav), "rb") as wf:
            channels = wf.getnchannels()
            framerate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            nframes = wf.getnframes()

        assert channels == EXPECTED_CHANNELS, (
            f"expected {EXPECTED_CHANNELS} channel(s), got {channels}"
        )
        assert framerate == EXPECTED_SAMPLE_RATE, (
            f"expected {EXPECTED_SAMPLE_RATE} Hz, got {framerate}"
        )
        assert sampwidth == EXPECTED_SAMPWIDTH_BYTES, (
            f"expected {EXPECTED_SAMPWIDTH_BYTES}-byte samples (16-bit PCM), "
            f"got {sampwidth}"
        )

        duration_actual = nframes / framerate
        assert abs(duration_actual - CAPTURE_WINDOW_SECONDS) <= 0.1, (
            f"WAV duration {duration_actual:.3f}s deviates from "
            f"{CAPTURE_WINDOW_SECONDS}s by more than 100 ms"
        )

    finally:
        if stop_timer is not None:
            stop_timer.cancel()

        # Drain any remaining stderr so failure reports have the daemon's
        # diagnostic output. Don't shadow the original exception.
        if proc.stderr is not None and not stderr_dump:
            try:
                stderr_dump = proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
        if stderr_dump:
            print(f"record-capture stderr:\n{stderr_dump}")

        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
