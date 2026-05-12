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
import shutil
import struct
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
    stem = "2026-05-12T00-00-00"
    output_wav = tmp_path / f"{stem}.wav"
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
            "output_path": str(output_wav),
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
        assert stopped["output_path"] == str(output_wav)

        # ------------------------------------------------------------------
        # File-pairing assertion (slice 2 acceptance criterion).
        # ------------------------------------------------------------------
        assert output_wav.exists(), f"WAV not written to {output_wav}"
        assert output_mp4.exists(), f"MP4 not written to {output_mp4}"
        assert output_wav.stem == output_mp4.stem, (
            f"wav stem {output_wav.stem!r} != mp4 stem {output_mp4.stem!r}"
        )

        # ------------------------------------------------------------------
        # WAV format + duration.
        # ------------------------------------------------------------------
        with wave.open(str(output_wav), "rb") as wf:
            channels = wf.getnchannels()
            framerate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            nframes = wf.getnframes()

        assert channels == EXPECTED_CHANNELS
        assert framerate == EXPECTED_SAMPLE_RATE
        assert sampwidth == EXPECTED_SAMPWIDTH_BYTES
        wav_duration = nframes / framerate
        assert abs(wav_duration - CAPTURE_WINDOW_SECONDS) <= 0.1, (
            f"WAV duration {wav_duration:.3f}s deviates from "
            f"{CAPTURE_WINDOW_SECONDS}s by more than 100 ms"
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
