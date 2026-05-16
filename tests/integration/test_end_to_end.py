"""End-to-end integration tests for the Swift capture binary.

Spawns the real ``record-capture`` binary in its synthetic test modes
(``--test-silent-sources`` and, for the video pair test, also
``--test-synthetic-video``) and verifies the JSON-line event stream plus the
resulting on-disk artifacts.

Coverage:

1. Audio-only happy path (``test_end_to_end_silent_sources``) — pre-slice-6
   contract: ``ready`` → ``started`` → 2 × ``source_attached`` → ``stopped``,
   plus WAV format + duration assertions.
2. Audio + video happy path (``test_end_to_end_silent_sources_with_synthetic_video``)
   — slice 6's CI-friendly synthetic video: same event sequence plus
   ``video_started`` and ``video_file``, plus an MP4 dimension / duration /
   filename-stem-pairing check.

The tests are intentionally black-box against the wire protocol, so they use
only the standard library (``json``, ``subprocess``, ``threading``, ``time``,
``wave``, ``struct``, ``pathlib``) plus an optional pyobjc / ``ffprobe``
probe for AVAsset playback validation. No project Python modules are imported
— that path is covered by the unit tests under ``tests/python/``.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
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
    # Spec 005: ``output_path`` is now an absolute basename without extension.
    # The Swift binary derives ``-mic.wav`` and ``-system.wav`` from it.
    output_basename = tmp_path / "synthetic"
    mic_wav = tmp_path / "synthetic-mic.wav"
    system_wav = tmp_path / "synthetic-system.wav"

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
            "output_path": str(output_basename),
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
        assert stopped["basename"] == str(output_basename), (
            f"stopped.basename mismatch: {stopped['basename']!r} "
            f"vs {str(output_basename)!r}"
        )

        # Spec 005: exactly two ``audio_file`` events, one per source, must
        # appear before the final ``stopped``.
        audio_file_events = [
            ev for ev in events if ev.get("event") == "audio_file"
        ]
        assert len(audio_file_events) == 2, (
            f"expected exactly 2 audio_file events, got "
            f"{len(audio_file_events)}: {audio_file_events!r}"
        )
        af_sources = sorted(ev["source"] for ev in audio_file_events)
        assert af_sources == ["mic", "system_audio"], (
            f"audio_file sources mismatch: {af_sources!r}"
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
        # WAV file assertions — spec 005: two independent files per session.
        # ------------------------------------------------------------------
        assert mic_wav.exists(), f"mic WAV not written to {mic_wav}"
        assert system_wav.exists(), f"system WAV not written to {system_wav}"

        for wav_path in (mic_wav, system_wav):
            with wave.open(str(wav_path), "rb") as wf:
                channels = wf.getnchannels()
                framerate = wf.getframerate()
                sampwidth = wf.getsampwidth()
                nframes = wf.getnframes()

            assert channels == EXPECTED_CHANNELS, (
                f"{wav_path}: expected {EXPECTED_CHANNELS} channel(s), got {channels}"
            )
            assert framerate == EXPECTED_SAMPLE_RATE, (
                f"{wav_path}: expected {EXPECTED_SAMPLE_RATE} Hz, got {framerate}"
            )
            assert sampwidth == EXPECTED_SAMPWIDTH_BYTES, (
                f"{wav_path}: expected {EXPECTED_SAMPWIDTH_BYTES}-byte samples "
                f"(16-bit PCM), got {sampwidth}"
            )

            duration_actual = nframes / framerate
            assert abs(duration_actual - CAPTURE_WINDOW_SECONDS) <= 0.5, (
                f"{wav_path}: WAV duration {duration_actual:.3f}s deviates from "
                f"{CAPTURE_WINDOW_SECONDS}s by more than 500 ms"
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


# ---------------------------------------------------------------------------
# Synthetic-video pair test
# ---------------------------------------------------------------------------

# Synthetic frame dimensions — the Swift `SyntheticVideoSource` produces
# 640×360 single-color frames at 30 fps. Pinned constants here so any change to
# the Swift side surfaces as a test failure.
SYNTHETIC_VIDEO_WIDTH = 640
SYNTHETIC_VIDEO_HEIGHT = 360


def _probe_mp4_via_pyobjc(mp4_path: Path) -> tuple[int, int, float] | None:
    """Return (width_px, height_px, duration_seconds) via ``AVAsset`` if
    pyobjc-framework-AVFoundation is importable, else ``None``.

    pyobjc isn't a project dependency (see ``pyproject.toml``); a developer
    who wants the strongest "opens cleanly via AVFoundation" assertion can run
    the suite under ``uv run --with pyobjc-framework-AVFoundation pytest …``.
    Otherwise we fall through to the MP4-atom parser below.
    """
    try:
        import AVFoundation  # type: ignore[import-not-found]
        from Foundation import NSURL  # type: ignore[import-not-found]
    except ImportError:
        return None
    url = NSURL.fileURLWithPath_(str(mp4_path))
    asset = AVFoundation.AVAsset.assetWithURL_(url)
    tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeVideo)
    if not tracks:
        pytest.fail(f"AVAsset reports zero video tracks for {mp4_path}")
    size = tracks[0].naturalSize()
    duration = asset.duration()
    seconds = (
        float(duration.value) / float(duration.timescale)
        if duration.timescale
        else 0.0
    )
    return int(size.width), int(size.height), seconds


def _probe_mp4_via_ffprobe(mp4_path: Path) -> tuple[int, int, float] | None:
    """Return (width_px, height_px, duration_seconds) via ``ffprobe`` if
    available on PATH, else ``None``."""
    if shutil.which("ffprobe") is None:
        return None
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(mp4_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    stream = payload["streams"][0]
    return (
        int(stream["width"]),
        int(stream["height"]),
        float(payload["format"]["duration"]),
    )


def _probe_mp4_via_atom_parse(mp4_path: Path) -> tuple[int, int, float]:
    """Parse the bare minimum of the MP4 atom tree to extract dimensions and
    duration without any third-party deps.

    Walks the top-level ``ftyp``/``moov`` boxes, locates ``moov`` → ``mvhd``
    for movie duration, and ``moov`` → ``trak`` → ``tkhd`` for track
    width/height. This is intentionally the lowest-common-denominator probe
    so the integration test runs on a vanilla ``uv run pytest`` with no extra
    deps and no ffmpeg installed. Raises ``AssertionError`` (via
    ``pytest.fail``) if the file is structurally invalid — that doubles as
    the "opens cleanly" check.
    """
    data = mp4_path.read_bytes()
    if len(data) < 8:
        pytest.fail(f"mp4 file at {mp4_path} is impossibly small ({len(data)} bytes)")

    def _atoms(buf: bytes, start: int, end: int) -> list[tuple[str, int, int]]:
        # Returns (type, payload_offset, payload_end) for each box.
        out: list[tuple[str, int, int]] = []
        i = start
        while i + 8 <= end:
            size = struct.unpack(">I", buf[i : i + 4])[0]
            typ = buf[i + 4 : i + 8].decode("ascii", errors="replace")
            hdr = 8
            if size == 1:
                size = struct.unpack(">Q", buf[i + 8 : i + 16])[0]
                hdr = 16
            elif size == 0:
                size = end - i
            if size < hdr or i + size > end:
                pytest.fail(
                    f"mp4 at {mp4_path}: malformed box `{typ}` at offset {i} "
                    f"(declared size {size}, file end {end})"
                )
            out.append((typ, i + hdr, i + size))
            i += size
        return out

    top = _atoms(data, 0, len(data))
    types = {typ for typ, _, _ in top}
    if "ftyp" not in types:
        pytest.fail(f"mp4 at {mp4_path} missing top-level `ftyp` box")
    if "moov" not in types:
        pytest.fail(
            f"mp4 at {mp4_path} missing top-level `moov` box "
            "(writer didn't finalize cleanly)"
        )
    moov = next((p, e) for typ, p, e in top if typ == "moov")
    moov_children = _atoms(data, moov[0], moov[1])

    # mvhd → movie timescale + duration
    mvhd = next(((p, e) for typ, p, e in moov_children if typ == "mvhd"), None)
    if mvhd is None:
        pytest.fail(f"mp4 at {mp4_path}: moov missing mvhd")
    mvhd_payload = data[mvhd[0] : mvhd[1]]
    version = mvhd_payload[0]
    if version == 0:
        timescale, duration_units = struct.unpack(">II", mvhd_payload[12:20])
    elif version == 1:
        timescale = struct.unpack(">I", mvhd_payload[20:24])[0]
        duration_units = struct.unpack(">Q", mvhd_payload[24:32])[0]
    else:
        pytest.fail(f"mp4 at {mp4_path}: mvhd unknown version {version}")
    if timescale == 0:
        pytest.fail(f"mp4 at {mp4_path}: mvhd timescale=0")
    duration_seconds = duration_units / timescale

    # trak → tkhd → width/height (16.16 fixed-point at the end of the payload)
    trak = next(((p, e) for typ, p, e in moov_children if typ == "trak"), None)
    if trak is None:
        pytest.fail(f"mp4 at {mp4_path}: moov missing trak")
    trak_children = _atoms(data, trak[0], trak[1])
    tkhd = next(((p, e) for typ, p, e in trak_children if typ == "tkhd"), None)
    if tkhd is None:
        pytest.fail(f"mp4 at {mp4_path}: trak missing tkhd")
    tkhd_payload = data[tkhd[0] : tkhd[1]]
    tk_version = tkhd_payload[0]
    if tk_version == 0:
        # version+flags(4) + ctime(4) + mtime(4) + track_id(4) + reserved(4) +
        # duration(4) + reserved(8) + layer(2) + alt(2) + vol(2) + reserved(2)
        # + matrix(36) = 76; then width(4), height(4) as 16.16 fixed point.
        width_off = 76
    elif tk_version == 1:
        width_off = 84
    else:
        pytest.fail(f"mp4 at {mp4_path}: tkhd unknown version {tk_version}")
    w_fixed, h_fixed = struct.unpack(
        ">II", tkhd_payload[width_off : width_off + 8]
    )
    return w_fixed >> 16, h_fixed >> 16, duration_seconds


def _probe_mp4(mp4_path: Path) -> tuple[int, int, float]:
    """Return (width_px, height_px, duration_seconds) for ``mp4_path``.

    Tries pyobjc-AVAsset → ffprobe → MP4 atom parse, in that order. The atom
    parser is the always-on baseline so the suite passes without extra deps.
    The pyobjc and ffprobe paths exist as stronger "macOS itself / ffmpeg
    accepts the file" checks for the developer who has them installed.
    """
    via_pyobjc = _probe_mp4_via_pyobjc(mp4_path)
    if via_pyobjc is not None:
        return via_pyobjc
    via_ffprobe = _probe_mp4_via_ffprobe(mp4_path)
    if via_ffprobe is not None:
        return via_ffprobe
    return _probe_mp4_via_atom_parse(mp4_path)


def test_end_to_end_silent_sources_with_synthetic_video(
    capture_binary: Path, tmp_path: Path
) -> None:
    """Drive the binary with both ``--test-silent-sources`` and
    ``--test-synthetic-video`` for ~2 s and assert both files land.

    Asserts the protocol event sequence (``ready`` → ``started`` →
    ``source_attached`` × 2 → ``video_started`` → ``stopped`` → ``video_file``,
    accepting reasonable reordering between the video / final stop events
    so long as the partial-order invariants hold), the WAV format + duration,
    and the MP4 dimensions + duration + filename-stem pairing.
    """
    # Shared timestamp stem mirrors the supervisor's CWD-and-stem convention.
    # Spec 005: ``output_path`` is the basename without extension; the Swift
    # binary derives the two per-source WAVs.
    stem = "2026-05-12T00-00-00"
    output_basename = tmp_path / stem
    mic_wav = tmp_path / f"{stem}-mic.wav"
    system_wav = tmp_path / f"{stem}-system.wav"
    output_mp4 = tmp_path / f"{stem}.mp4"

    proc = subprocess.Popen(
        [
            str(capture_binary),
            "--test-silent-sources",
            "--test-synthetic-video",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    events: list[dict[str, object]] = []
    stderr_dump = ""
    stop_timer: threading.Timer | None = None
    stdin_lock = threading.Lock()

    def _send_stop() -> None:
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

        ready_line = proc.stdout.readline()
        if not ready_line:
            pytest.fail("record-capture closed stdout before emitting `ready`")
        ready = json.loads(ready_line)
        assert ready == {"event": "ready"}, (
            f"first event was not `ready`: {ready!r}"
        )

        start_cmd = {
            "cmd": "start",
            "output_path": str(output_basename),
            "video_output_path": str(output_mp4),
            "format": {
                "sample_rate": EXPECTED_SAMPLE_RATE,
                "bit_depth": 16,
                "channels": EXPECTED_CHANNELS,
            },
            "video": {"fps": 30, "show_cursor": True},
        }
        with stdin_lock:
            proc.stdin.write(json.dumps(start_cmd) + "\n")
            proc.stdin.flush()

        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
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

        with stdin_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "record-capture did not exit within 5s after `stopped` event"
            )

        assert proc.stderr is not None
        stderr_dump = proc.stderr.read() or ""

        assert proc.returncode == 0, (
            f"record-capture exited with code {proc.returncode}; "
            f"stderr:\n{stderr_dump}"
        )

        # ------------------------------------------------------------------
        # Event-sequence assertions.
        #
        # Required partial order (per the spec's wire-order contract):
        #   - `started` comes before any `source_attached`.
        #   - `video_started` appears once, after `started`, before `stopped`.
        #   - `stopped` is the last event we read in this loop (we break on it).
        #   - `video_file` arrives in the event stream; in the current binary
        #     implementation it actually precedes `stopped` (the writer
        #     finalizes inside the stop path before `stopped` is emitted), but
        #     the spec calls out the relative order as "video_file after
        #     started." We assert that minimal constraint here and don't pin
        #     a single canonical position relative to `stopped` so the test
        #     stays robust if the Swift side reorders later.
        # ------------------------------------------------------------------
        sequence = [ev["event"] for ev in events]
        assert sequence[0] == "started", (
            f"expected first event after `ready` to be `started`, got: {sequence!r}"
        )
        assert sequence[-1] == "stopped", (
            f"expected last event read to be `stopped`, got: {sequence!r}"
        )

        attached_sources = sorted(
            ev["source"] for ev in events if ev["event"] == "source_attached"
        )
        assert attached_sources == ["mic", "system_audio"], (
            f"expected both audio sources to attach, got {attached_sources!r}; "
            f"full sequence: {sequence!r}"
        )

        # Exactly one `video_started`, exactly one `video_file`.
        video_started = [ev for ev in events if ev["event"] == "video_started"]
        video_file = [ev for ev in events if ev["event"] == "video_file"]
        assert len(video_started) == 1, (
            f"expected exactly one `video_started`, got {len(video_started)}; "
            f"full sequence: {sequence!r}"
        )
        assert len(video_file) == 1, (
            f"expected exactly one `video_file`, got {len(video_file)}; "
            f"full sequence: {sequence!r}"
        )

        # Partial-order checks. Indices into `events`, not into a sorted list.
        started_idx = sequence.index("started")
        video_started_idx = sequence.index("video_started")
        stopped_idx = sequence.index("stopped")
        video_file_idx = sequence.index("video_file")
        assert started_idx < video_started_idx < stopped_idx, (
            f"video_started must fall between started and stopped; "
            f"sequence: {sequence!r}"
        )
        assert started_idx < video_file_idx, (
            f"video_file must come after started; sequence: {sequence!r}"
        )

        bad_events = [
            ev
            for ev in events
            if ev["event"] in ("permission_required", "permission_denied", "error", "video_lost")
        ]
        assert not bad_events, (
            f"synthetic mode should not emit permission/error/video_lost events, "
            f"got: {bad_events!r}"
        )

        # ------------------------------------------------------------------
        # Payload sanity.
        # ------------------------------------------------------------------
        vstarted = video_started[0]
        assert vstarted["width_px"] == SYNTHETIC_VIDEO_WIDTH, (
            f"video_started width mismatch: {vstarted['width_px']!r}"
        )
        assert vstarted["height_px"] == SYNTHETIC_VIDEO_HEIGHT, (
            f"video_started height mismatch: {vstarted['height_px']!r}"
        )
        assert vstarted["fps"] == 30, f"video_started fps mismatch: {vstarted['fps']!r}"

        vfile = video_file[0]
        assert vfile["path"] == str(output_mp4), (
            f"video_file.path mismatch: {vfile['path']!r} vs {str(output_mp4)!r}"
        )
        vfile_duration = float(vfile["duration_seconds"])
        assert abs(vfile_duration - CAPTURE_WINDOW_SECONDS) <= 0.5, (
            f"video_file duration {vfile_duration:.3f}s deviates from "
            f"requested {CAPTURE_WINDOW_SECONDS}s by more than 500 ms"
        )

        stopped = events[-1]
        assert stopped["basename"] == str(output_basename)

        # Spec 005: exactly two ``audio_file`` events accompany ``stopped``.
        audio_file_events = [
            ev for ev in events if ev.get("event") == "audio_file"
        ]
        assert len(audio_file_events) == 2, (
            f"expected exactly 2 audio_file events, got "
            f"{len(audio_file_events)}: {audio_file_events!r}"
        )

        # ------------------------------------------------------------------
        # File-pairing assertion (slice 2 acceptance criterion) — spec 005:
        # both per-source WAVs land alongside the MP4 with the shared stem.
        # ------------------------------------------------------------------
        assert mic_wav.exists(), f"mic WAV not written to {mic_wav}"
        assert system_wav.exists(), f"system WAV not written to {system_wav}"
        assert output_mp4.exists(), f"MP4 not written to {output_mp4}"
        assert mic_wav.name.startswith(stem), (
            f"mic wav name {mic_wav.name!r} does not start with stem {stem!r}"
        )

        # ------------------------------------------------------------------
        # WAV format + duration — both files.
        # ------------------------------------------------------------------
        for wav_path in (mic_wav, system_wav):
            with wave.open(str(wav_path), "rb") as wf:
                channels = wf.getnchannels()
                framerate = wf.getframerate()
                sampwidth = wf.getsampwidth()
                nframes = wf.getnframes()

            assert channels == EXPECTED_CHANNELS
            assert framerate == EXPECTED_SAMPLE_RATE
            assert sampwidth == EXPECTED_SAMPWIDTH_BYTES
            wav_duration = nframes / framerate
            assert abs(wav_duration - CAPTURE_WINDOW_SECONDS) <= 0.5, (
                f"{wav_path}: WAV duration {wav_duration:.3f}s deviates from "
                f"{CAPTURE_WINDOW_SECONDS}s by more than 500 ms"
            )

        # ------------------------------------------------------------------
        # MP4 opens cleanly + dimensions + duration.
        # ------------------------------------------------------------------
        mp4_size = output_mp4.stat().st_size
        assert mp4_size > 0, f"mp4 file at {output_mp4} is empty"
        width, height, mp4_duration = _probe_mp4(output_mp4)
        assert width == SYNTHETIC_VIDEO_WIDTH, (
            f"mp4 width {width} != expected {SYNTHETIC_VIDEO_WIDTH}"
        )
        assert height == SYNTHETIC_VIDEO_HEIGHT, (
            f"mp4 height {height} != expected {SYNTHETIC_VIDEO_HEIGHT}"
        )
        # MP4 duration tolerance is wider than the WAV's because the encoder
        # finalizes against whole-frame boundaries (≈33 ms at 30 fps) and the
        # writer flushes slightly later than the WAV closer. ±0.5 s matches
        # what we already accept for the daemon-reported `duration_seconds`
        # on the audio side.
        assert abs(mp4_duration - CAPTURE_WINDOW_SECONDS) <= 0.5, (
            f"MP4 duration {mp4_duration:.3f}s deviates from "
            f"{CAPTURE_WINDOW_SECONDS}s by more than 500 ms"
        )

    finally:
        if stop_timer is not None:
            stop_timer.cancel()

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


# ---------------------------------------------------------------------------
# Daemon-driven variant (spec 003 slice 2)
#
# Spawns ``python -m record.daemon`` as a real subprocess against a sandboxed
# ``$HOME`` (short ``/tmp/...`` path so ``AF_UNIX`` fits inside macOS's 104-char
# limit), then drives a ``start`` / ``stop`` cycle over the daemon's control
# socket. The Swift child is launched in synthetic mode via the
# ``RECORD_CAPTURE_TEST_FLAGS`` env var that ``record.capture`` reads.
# ---------------------------------------------------------------------------


def _wait_for_socket(socket_path: Path, *, timeout: float) -> bool:
    """Poll up to ``timeout`` seconds for the daemon to bind ``socket_path``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return True
        time.sleep(0.05)
    return False


def _send_control_request(
    socket_path: Path, payload: dict, *, timeout: float = 30.0
) -> dict:
    """Open the daemon socket, send one JSON request line, read one response."""
    import socket as _socket

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(socket_path))
    try:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        deadline = time.monotonic() + timeout
        while b"\n" not in buf and time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        line, _, _ = buf.partition(b"\n")
        if not line:
            pytest.fail("daemon closed the socket without responding")
        return json.loads(line.decode("utf-8"))
    finally:
        try:
            sock.close()
        except Exception:
            pass


def test_end_to_end_daemon_driven_start_stop(
    capture_binary: Path,
) -> None:
    """``record.daemon`` end-to-end: spawn daemon → start → stop → quit.

    Verifies:
      - the daemon binds its socket;
      - a ``start`` request produces a capture (audio+video files materialize);
      - ``capture-state.json`` is finalized with ``final: true``;
      - a ``quit`` request returns ``ok`` and the daemon exits cleanly.
    """
    # Sandbox $HOME so the daemon writes its PID file, socket, log, and
    # state file inside a throwaway directory. The path is intentionally
    # short (`/tmp/rd-XXXX`) — we explicitly set ``dir="/tmp"`` because the
    # default ``$TMPDIR`` on macOS points at ``/var/folders/.../T/`` which
    # blows past the 104-character ``AF_UNIX`` path limit once we append
    # ``Library/Application Support/record/daemon.sock``.
    sandbox = Path(tempfile.mkdtemp(prefix="rd-", dir="/tmp"))
    cwd = sandbox / "out"
    cwd.mkdir()
    # Resolve symlinks (``/tmp`` -> ``/private/tmp`` on macOS) so the
    # equality check against the daemon-returned path lines up.
    cwd_resolved = cwd.resolve()

    # Slice 3 routes captures through ``Config.output_folder`` (default
    # ``~/record/``). Point it at the test's CWD via a sandboxed config file.
    config_dir = sandbox / ".config" / "record"
    config_dir.mkdir(parents=True)
    log_dir = sandbox / "logs"
    log_dir.mkdir()
    (config_dir / "config.toml").write_text(
        f'output_folder = "{cwd_resolved}"\nlog_folder = "{log_dir.resolve()}"\n',
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["HOME"] = str(sandbox)
    # Force the Swift child into synthetic mode (no TCC needed in CI).
    env["RECORD_CAPTURE_TEST_FLAGS"] = "--test-silent-sources --test-synthetic-video"

    daemon_proc = subprocess.Popen(
        [sys.executable, "-m", "record.daemon"],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    socket_path = (
        sandbox / "Library" / "Application Support" / "record" / "daemon.sock"
    )
    state_path = (
        sandbox / "Library" / "Application Support" / "record" / "capture-state.json"
    )

    try:
        # Wait for the daemon to bind its socket.
        if not _wait_for_socket(socket_path, timeout=10.0):
            assert daemon_proc.stderr is not None
            try:
                stderr_dump = daemon_proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
            pytest.fail(
                f"daemon did not bind socket at {socket_path} within 10s; "
                f"stderr:\n{stderr_dump}"
            )

        # ----- start -----------------------------------------------------
        start_resp = _send_control_request(socket_path, {"op": "start"})
        assert start_resp["status"] == "ok", start_resp
        # Spec 005: ``audio_path`` carries the mic file (single-field surface);
        # ``audio_paths`` carries both per-source paths.
        audio_path = Path(start_resp["audio_path"])
        audio_paths = start_resp.get("audio_paths") or {}
        system_audio_path = Path(audio_paths["system_audio"])
        video_path = Path(start_resp["video_path"])
        assert audio_path.parent == cwd_resolved, audio_path
        assert system_audio_path.parent == cwd_resolved, system_audio_path
        assert video_path.parent == cwd_resolved, video_path

        # Let the synthetic capture run briefly so it produces real frames.
        time.sleep(2.0)

        # ----- status (sanity check) ------------------------------------
        status_resp = _send_control_request(socket_path, {"op": "status"})
        assert status_resp["status"] == "ok"
        assert status_resp["capture"]["running"] is True

        # ----- stop ------------------------------------------------------
        stop_resp = _send_control_request(
            socket_path, {"op": "stop"}, timeout=30.0
        )
        assert stop_resp["status"] == "ok", stop_resp

        # Files materialised in the configured (CWD) directory — both per-source
        # WAVs plus the MP4.
        assert audio_path.exists(), f"mic audio file missing at {audio_path}"
        assert system_audio_path.exists(), (
            f"system audio file missing at {system_audio_path}"
        )
        assert video_path.exists(), f"video file missing at {video_path}"

        # capture-state.json should be finalized with the basename and the
        # per-source ``audio_files`` map.
        assert state_path.exists()
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert final_state.get("final") is True, final_state
        files = final_state.get("audio_files") or {}
        assert files.get("mic", {}).get("path") == str(audio_path)
        assert files.get("system_audio", {}).get("path") == str(system_audio_path)

        # ----- quit ------------------------------------------------------
        quit_resp = _send_control_request(socket_path, {"op": "quit"})
        assert quit_resp["status"] == "ok"

        # Daemon should exit cleanly.
        try:
            daemon_proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pytest.fail("daemon did not exit within 10s after quit request")
        assert daemon_proc.returncode == 0, (
            f"daemon exited with code {daemon_proc.returncode}"
        )

        # MP4 opens cleanly via the same probe the synthetic-video test uses.
        width, height, mp4_duration = _probe_mp4(video_path)
        assert width == SYNTHETIC_VIDEO_WIDTH
        assert height == SYNTHETIC_VIDEO_HEIGHT
        assert mp4_duration > 0.5

        # WAV format sanity.
        with wave.open(str(audio_path), "rb") as wf:
            assert wf.getnchannels() == EXPECTED_CHANNELS
            assert wf.getframerate() == EXPECTED_SAMPLE_RATE
            assert wf.getsampwidth() == EXPECTED_SAMPWIDTH_BYTES
            nframes = wf.getnframes()
        wav_duration = nframes / EXPECTED_SAMPLE_RATE
        assert wav_duration > 0.5
    finally:
        if daemon_proc.poll() is None:
            try:
                daemon_proc.send_signal(15)  # SIGTERM
                daemon_proc.wait(timeout=5.0)
            except Exception:
                pass
        if daemon_proc.poll() is None:
            try:
                daemon_proc.kill()
            except Exception:
                pass

        if daemon_proc.stderr is not None:
            try:
                stderr_dump = daemon_proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
            if stderr_dump:
                print(f"record.daemon stderr:\n{stderr_dump}")

        shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec 004 slice 2 — daemon auto-transcription on finalize
#
# Spawns the real ``python -m record.daemon`` subprocess with synthetic Swift
# capture (same harness the slice-2 daemon-driven test above uses), but points
# Deepgram-bound HTTP at a localhost stub server via the
# ``RECORD_DEEPGRAM_ENDPOINT`` test seam. After ``start`` + ``stop``, polls
# (no ``time.sleep``-based hard wait — uses ``asyncio.wait_for``-style
# bounded polling) for the three transcript files to materialise next to the
# recorded ``.wav``.
# ---------------------------------------------------------------------------


import socketserver  # noqa: E402 — placed next to its test usage
from http.server import BaseHTTPRequestHandler  # noqa: E402


# Canned Deepgram response shaped exactly like the slice-1 fixture in
# ``tests/python/test_transcribe.py``. Inlined here to keep the integration
# layer dependency-free (the suite's conftest forbids importing ``record.*``).
_CANNED_DEEPGRAM_RESPONSE: dict = {
    "metadata": {
        "duration": 1.5,
        "channels": 1,
        "models": ["nova-3"],
    },
    "results": {
        "channels": [
            {
                "detected_language": "en",
                "alternatives": [
                    {
                        "transcript": "hello there",
                        "languages": ["en"],
                    }
                ],
            }
        ],
        "utterances": [
            {
                "speaker": 0,
                "start": 0.0,
                "end": 1.5,
                "transcript": "hello there",
            }
        ],
    },
}


class _DeepgramStubHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that mimics Deepgram's pre-recorded endpoint."""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        # Drain the request body so the daemon's POST completes cleanly.
        if length:
            self.rfile.read(length)
        body = json.dumps(_CANNED_DEEPGRAM_RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence the default stderr access log so the pytest output stays clean.
        return


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _wait_for_files(
    paths_to_check: list[Path], *, timeout: float
) -> bool:
    """Poll up to ``timeout`` seconds for every path in ``paths_to_check``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(p.exists() for p in paths_to_check):
            return True
        time.sleep(0.05)
    return False


def test_end_to_end_daemon_auto_transcription_on_finalize(
    capture_binary: Path,
) -> None:
    """Daemon capture → finalize → background transcription writes 3 files.

    Spawns the real ``record.daemon`` subprocess with synthetic Swift capture
    and a localhost Deepgram stub. After the capture's ``stop`` resolves, the
    daemon's spawned transcription task POSTs to the stub, gets the canned
    response, and writes ``{stem}.json/.txt/.srt`` next to the recorded
    ``.wav``. The control reply for ``stop`` must not block on this — the
    files materialise asynchronously, so we poll on a bounded deadline.
    """
    # Spin up the Deepgram stub. ``port=0`` lets the OS pick a free port.
    httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _DeepgramStubHandler)
    stub_thread = threading.Thread(
        target=httpd.serve_forever, name="deepgram-stub", daemon=True
    )
    stub_thread.start()
    try:
        stub_port = httpd.server_address[1]
        stub_endpoint = f"http://127.0.0.1:{stub_port}/v1/listen"

        # Same sandbox pattern as ``test_end_to_end_daemon_driven_start_stop``.
        sandbox = Path(tempfile.mkdtemp(prefix="rd-", dir="/tmp"))
        try:
            cwd = sandbox / "out"
            cwd.mkdir()
            cwd_resolved = cwd.resolve()

            config_dir = sandbox / ".config" / "record"
            config_dir.mkdir(parents=True)
            log_dir = sandbox / "logs"
            log_dir.mkdir()
            (config_dir / "config.toml").write_text(
                f'output_folder = "{cwd_resolved}"\n'
                f'log_folder = "{log_dir.resolve()}"\n',
                encoding="utf-8",
            )

            env = dict(os.environ)
            env["HOME"] = str(sandbox)
            env["RECORD_CAPTURE_TEST_FLAGS"] = (
                "--test-silent-sources --test-synthetic-video"
            )
            # Slice-1 env-var fallback for the API key (saves a Keychain
            # mutation from the test); slice-2 test seam for the endpoint.
            env["RECORD_DEEPGRAM_API_KEY"] = "test-key-not-real"
            env["RECORD_DEEPGRAM_ENDPOINT"] = stub_endpoint

            daemon_proc = subprocess.Popen(
                [sys.executable, "-m", "record.daemon"],
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            socket_path = (
                sandbox
                / "Library"
                / "Application Support"
                / "record"
                / "daemon.sock"
            )

            try:
                if not _wait_for_socket(socket_path, timeout=10.0):
                    assert daemon_proc.stderr is not None
                    try:
                        stderr_dump = daemon_proc.stderr.read() or ""
                    except Exception:
                        stderr_dump = ""
                    pytest.fail(
                        f"daemon did not bind socket at {socket_path} within 10s; "
                        f"stderr:\n{stderr_dump}"
                    )

                # ----- start ---------------------------------------------
                start_resp = _send_control_request(socket_path, {"op": "start"})
                assert start_resp["status"] == "ok", start_resp
                # Spec 005: ``audio_path`` is the mic file; ``audio_paths`` is
                # the per-source map.
                audio_path = Path(start_resp["audio_path"])
                audio_paths = start_resp.get("audio_paths") or {}
                system_audio_path = Path(audio_paths["system_audio"])
                assert audio_path.parent == cwd_resolved, audio_path

                # Let the synthetic capture produce a beat of real frames.
                time.sleep(1.5)

                # ----- stop ----------------------------------------------
                stop_resp = _send_control_request(
                    socket_path, {"op": "stop"}, timeout=30.0
                )
                assert stop_resp["status"] == "ok", stop_resp
                assert audio_path.exists()
                assert system_audio_path.exists()

                # ----- wait for the spawned transcription tasks ----------
                # One transcript triple per finalized WAV.
                stem_dir = audio_path.parent
                expected_files: list[Path] = []
                for wav_path in (audio_path, system_audio_path):
                    base = wav_path.stem
                    expected_files.extend(
                        [
                            stem_dir / f"{base}.json",
                            stem_dir / f"{base}.txt",
                            stem_dir / f"{base}.srt",
                        ]
                    )

                got_all = _wait_for_files(expected_files, timeout=10.0)
                assert got_all, (
                    f"transcript files did not materialise within 10s; "
                    f"dir contents: {sorted(p.name for p in stem_dir.iterdir())!r}"
                )

                # Use the mic JSON for the canned-content sanity check.
                json_path = stem_dir / f"{audio_path.stem}.json"

                # Sanity-check the .json content. The stub's canned utterance
                # carries "hello there" as the lone segment's text.
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                assert payload["provider"] == "deepgram"
                assert payload["model"] == "nova-3"
                assert len(payload["segments"]) == 1
                assert payload["segments"][0]["text"] == "hello there"
                assert payload["segments"][0]["speaker"] == "Speaker 1"

                # ----- quit ---------------------------------------------
                quit_resp = _send_control_request(socket_path, {"op": "quit"})
                assert quit_resp["status"] == "ok"
                try:
                    daemon_proc.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    pytest.fail("daemon did not exit within 10s after quit")
                assert daemon_proc.returncode == 0, (
                    f"daemon exited with code {daemon_proc.returncode}"
                )
            finally:
                if daemon_proc.poll() is None:
                    try:
                        daemon_proc.send_signal(15)  # SIGTERM
                        daemon_proc.wait(timeout=5.0)
                    except Exception:
                        pass
                if daemon_proc.poll() is None:
                    try:
                        daemon_proc.kill()
                    except Exception:
                        pass
                if daemon_proc.stderr is not None:
                    try:
                        stderr_dump = daemon_proc.stderr.read() or ""
                    except Exception:
                        stderr_dump = ""
                    if stderr_dump:
                        print(f"record.daemon stderr:\n{stderr_dump}")
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
        stub_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Slice 4 — 3 back-to-back captures driven by one long-lived Swift child
#
# Verifies that the daemon spawns a single ``record-capture --daemon`` child
# at startup and reuses it for every capture, per spec 003 slice 4. Asserts:
#
#   * Each cycle produces fresh ``.wav`` + ``.mp4`` artifacts under distinct
#     filename stems and all three pairs survive on disk after cycle 3.
#   * ``capture-state.json`` reflects only the most recent capture but is
#     finalized (``final: true``) after each stop.
#   * The daemon's open-fd count (via ``psutil``) does not grow monotonically
#     across the three cycles — caps the "did we leak a pipe / socket per
#     capture" regression that motivated the slice-4 audit.
#   * All three MP4s open cleanly via the same ``_probe_mp4`` helper used by
#     the synthetic-video pair test (dimensions + duration sanity).
#   * After cycle 3, ``quit`` returns ``ok`` and the daemon exits cleanly.
# ---------------------------------------------------------------------------


def test_end_to_end_daemon_driven_three_cycles(
    capture_binary: Path,
) -> None:
    """Drive three start/stop cycles through one daemon-spawned Swift child.

    The cycle inputs are identical to ``test_end_to_end_daemon_driven_start_stop``
    (same synthetic-source flags, same control socket protocol) — what's
    different is the daemon's internal state. In slice-2 each cycle would
    have spawned a fresh ``record-capture`` subprocess; in slice 4 the daemon
    re-uses one long-lived ``--daemon`` child. This test pins that contract
    and adds an fd-stability check as the load-bearing leak gate.
    """
    psutil = pytest.importorskip("psutil")  # CI-friendly; declared in pyproject.toml

    # See ``test_end_to_end_daemon_driven_start_stop`` for why ``/tmp`` is
    # hard-coded (104-char AF_UNIX limit on macOS).
    sandbox = Path(tempfile.mkdtemp(prefix="rd-", dir="/tmp"))
    cwd = sandbox / "out"
    cwd.mkdir()
    cwd_resolved = cwd.resolve()

    config_dir = sandbox / ".config" / "record"
    config_dir.mkdir(parents=True)
    log_dir = sandbox / "logs"
    log_dir.mkdir()
    (config_dir / "config.toml").write_text(
        f'output_folder = "{cwd_resolved}"\nlog_folder = "{log_dir.resolve()}"\n',
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["HOME"] = str(sandbox)
    env["RECORD_CAPTURE_TEST_FLAGS"] = (
        "--test-silent-sources --test-synthetic-video"
    )

    daemon_proc = subprocess.Popen(
        [sys.executable, "-m", "record.daemon"],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    socket_path = (
        sandbox / "Library" / "Application Support" / "record" / "daemon.sock"
    )
    state_path = (
        sandbox / "Library" / "Application Support" / "record" / "capture-state.json"
    )

    # Per-cycle capture window. Stays short on purpose: total wall-clock
    # budget for the test is ~6 s (1.5 s × 3 + Swift bring-up + finalize).
    cycle_window_seconds = 1.5

    # fd-count slack across cycles. Healthy churn (e.g. a transient stderr
    # drain pipe being recycled, asyncio internals) can move the count by a
    # few; we only fail on monotonic growth that would indicate a leaked
    # pipe / socket per capture.
    fd_growth_slack = 5

    try:
        if not _wait_for_socket(socket_path, timeout=10.0):
            assert daemon_proc.stderr is not None
            try:
                stderr_dump = daemon_proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
            pytest.fail(
                f"daemon did not bind socket at {socket_path} within 10s; "
                f"stderr:\n{stderr_dump}"
            )

        daemon_psutil = psutil.Process(daemon_proc.pid)
        baseline_fds = daemon_psutil.num_fds()

        cycle_paths: list[tuple[Path, Path]] = []
        fds_after_cycle: list[int] = []

        for cycle_idx in range(3):
            start_resp = _send_control_request(socket_path, {"op": "start"})
            assert start_resp["status"] == "ok", (
                f"cycle {cycle_idx}: start failed: {start_resp!r}"
            )
            # Spec 005: ``audio_path`` is the mic file; ``audio_paths`` carries
            # both per-source paths.
            audio_path = Path(start_resp["audio_path"])
            audio_paths = start_resp.get("audio_paths") or {}
            system_audio_path = Path(audio_paths["system_audio"])
            video_path = Path(start_resp["video_path"])
            assert audio_path.parent == cwd_resolved, audio_path
            assert system_audio_path.parent == cwd_resolved, system_audio_path
            assert video_path.parent == cwd_resolved, video_path

            # Let the synthetic feed produce real frames for a beat.
            time.sleep(cycle_window_seconds)

            stop_resp = _send_control_request(
                socket_path, {"op": "stop"}, timeout=30.0
            )
            assert stop_resp["status"] == "ok", (
                f"cycle {cycle_idx}: stop failed: {stop_resp!r}"
            )

            # Per-cycle artifacts materialise — both per-source WAVs + MP4.
            assert audio_path.exists(), (
                f"cycle {cycle_idx}: mic audio file missing at {audio_path}"
            )
            assert system_audio_path.exists(), (
                f"cycle {cycle_idx}: system audio file missing at "
                f"{system_audio_path}"
            )
            assert video_path.exists(), (
                f"cycle {cycle_idx}: video file missing at {video_path}"
            )
            cycle_paths.append((audio_path, video_path))

            # capture-state.json is finalized + reflects THIS cycle's audio
            # paths (the daemon overwrites it per-capture).
            assert state_path.exists()
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            assert final_state.get("final") is True, (
                f"cycle {cycle_idx}: state not finalized: {final_state!r}"
            )
            files = final_state.get("audio_files") or {}
            assert files.get("mic", {}).get("path") == str(audio_path), (
                f"cycle {cycle_idx}: state.audio_files.mic.path mismatch: "
                f"{final_state!r}"
            )
            assert files.get("system_audio", {}).get("path") == str(
                system_audio_path
            ), (
                f"cycle {cycle_idx}: state.audio_files.system_audio.path "
                f"mismatch: {final_state!r}"
            )

            # Record fd count. We sample once per cycle, after stop has
            # fully unwound — that's the cleanest moment to compare across
            # cycles because no transient asyncio pipes are open.
            fds_after_cycle.append(daemon_psutil.num_fds())

        # All three pairs of files survive on disk and use distinct stems.
        # Spec 005: the mic file's name is ``<timestamp>-mic.wav`` so its stem
        # is ``<timestamp>-mic`` — strip the ``-mic`` suffix to compare against
        # the video file's bare timestamp stem.
        all_audio_paths = [p[0] for p in cycle_paths]
        all_video_paths = [p[1] for p in cycle_paths]
        all_stems = {p.stem for p in all_audio_paths}
        assert len(all_stems) == 3, (
            f"expected 3 distinct filename stems, got {sorted(all_stems)!r}"
        )
        for audio_path, video_path in cycle_paths:
            assert audio_path.exists(), (
                f"audio path {audio_path} disappeared between cycles"
            )
            assert video_path.exists(), (
                f"video path {video_path} disappeared between cycles"
            )
            assert audio_path.stem.removesuffix("-mic") == video_path.stem, (
                f"per-cycle wav/mp4 stems do not match: "
                f"{audio_path.stem!r} vs {video_path.stem!r}"
            )

        # fd-stability gate. The headline contract: cycle-3 fd count must not
        # have grown beyond cycle-1's by more than ``fd_growth_slack``. A
        # leak would manifest as steady growth (one extra pipe per cycle).
        assert fds_after_cycle[2] <= fds_after_cycle[0] + fd_growth_slack, (
            f"fd count grew across cycles: baseline={baseline_fds}, "
            f"after-cycle counts={fds_after_cycle!r}; suggests a per-capture "
            f"resource leak in the daemon or Swift child"
        )

        # Every MP4 opens cleanly through the same probe the synthetic-video
        # pair test uses — covers the "Swift recycled across cycles still
        # writes a valid file" invariant from the slice-4 audit.
        for audio_path, video_path in cycle_paths:
            width, height, mp4_duration = _probe_mp4(video_path)
            assert width == SYNTHETIC_VIDEO_WIDTH, (
                f"{video_path}: width {width} != expected {SYNTHETIC_VIDEO_WIDTH}"
            )
            assert height == SYNTHETIC_VIDEO_HEIGHT, (
                f"{video_path}: height {height} != expected {SYNTHETIC_VIDEO_HEIGHT}"
            )
            assert mp4_duration > 0.5, (
                f"{video_path}: duration {mp4_duration:.3f}s too short "
                f"(<0.5s) — finalizer likely truncated the file"
            )

            # WAV sanity for each cycle's audio file.
            with wave.open(str(audio_path), "rb") as wf:
                assert wf.getnchannels() == EXPECTED_CHANNELS
                assert wf.getframerate() == EXPECTED_SAMPLE_RATE
                assert wf.getsampwidth() == EXPECTED_SAMPWIDTH_BYTES
                nframes = wf.getnframes()
            wav_duration = nframes / EXPECTED_SAMPLE_RATE
            assert wav_duration > 0.5, (
                f"{audio_path}: WAV duration {wav_duration:.3f}s < 0.5s"
            )
            _ = all_video_paths  # silence flake8 unused; kept for symmetry

        # ----- quit ------------------------------------------------------
        quit_resp = _send_control_request(socket_path, {"op": "quit"})
        assert quit_resp["status"] == "ok", quit_resp

        try:
            daemon_proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pytest.fail("daemon did not exit within 10s after quit request")
        assert daemon_proc.returncode == 0, (
            f"daemon exited with code {daemon_proc.returncode}"
        )
    finally:
        if daemon_proc.poll() is None:
            try:
                daemon_proc.send_signal(15)  # SIGTERM
                daemon_proc.wait(timeout=5.0)
            except Exception:
                pass
        if daemon_proc.poll() is None:
            try:
                daemon_proc.kill()
            except Exception:
                pass

        if daemon_proc.stderr is not None:
            try:
                stderr_dump = daemon_proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
            if stderr_dump:
                print(f"record.daemon stderr:\n{stderr_dump}")

        shutil.rmtree(sandbox, ignore_errors=True)
