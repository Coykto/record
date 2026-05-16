"""Helpers for the real-capture end-to-end test (opt-in).

This module is intentionally named with a leading underscore so pytest does
not collect it during test discovery. It contains only helper functions and
must not define any ``test_*`` symbols.

Slice 3 scope: pre-flight checks that fail loudly with actionable messages
when any host-side prerequisite (BlackHole 2ch virtual audio device,
SwitchAudioSource CLI, capture-binary TCC grants) is missing. Per
functional-spec section 2.7, missing prerequisites are real failures (not
skips) and the failure message must be distinct from a regression in
capture — it must point the engineer at the install/grant step.
"""

from __future__ import annotations

import array
import json
import math
import shutil
import subprocess
import sys
import wave
from contextlib import contextmanager
from pathlib import Path

import pytest


# Verbatim install / grant commands. These must match the prerequisite comment
# block in the Makefile (lines 36-40); functional-spec section 2.9 requires
# the failure messages to match the documented setup steps.
_INSTALL_SWITCHAUDIO = "brew install switchaudio-osx"
_INSTALL_BLACKHOLE = "brew install --cask blackhole-2ch"
_GRANT_SCREEN_RECORDING = (
    "Grant Screen Recording permission to record-capture in "
    "System Settings -> Privacy & Security -> Screen Recording"
)
_GRANT_MICROPHONE = (
    "Grant Microphone permission to record-capture in "
    "System Settings -> Privacy & Security -> Microphone"
)


def switchaudio_available() -> bool:
    """Return True iff the ``SwitchAudioSource`` CLI is on PATH."""
    return shutil.which("SwitchAudioSource") is not None


def blackhole_available() -> bool:
    """Return True iff ``BlackHole 2ch`` appears in the macOS input-device list.

    Returns ``False`` (does not raise) if ``SwitchAudioSource`` is missing or
    the invocation errors for any reason — the caller distinguishes "tool
    missing" from "device missing" via :func:`switchaudio_available`.
    """
    if not switchaudio_available():
        return False
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-a", "-t", "input"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "BlackHole 2ch" in result.stdout


def check_capture_permissions(binary: Path) -> dict[str, bool]:
    """Invoke ``<binary> --check-permissions`` and parse the permission events.

    The Swift capture binary emits two JSON-line events on stdout:
    ``{"event":"permission","name":"screen_recording","granted":bool}`` and
    ``{"event":"permission","name":"microphone","granted":bool}``. Returns a
    dict keyed by permission name; missing permissions default to ``False``.
    """
    result: dict[str, bool] = {"screen_recording": False, "microphone": False}
    try:
        completed = subprocess.run(
            [str(binary), "--check-permissions"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return result

    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") != "permission":
            continue
        name = event.get("name")
        granted = event.get("granted")
        if isinstance(name, str) and isinstance(granted, bool):
            if name in result:
                result[name] = granted
    return result


def assert_prereqs_or_fail(binary: Path) -> None:
    """Verify every host-side prerequisite for the real-capture test.

    Raises via :func:`pytest.fail` on the first missing prerequisite. The
    failure message names the prerequisite by its plain name and quotes the
    install or grant command verbatim, so the engineer can act on it without
    re-reading the spec. Per functional-spec 2.7, this is a failure (not a
    skip) so missing setup cannot masquerade as a green run.
    """
    if not switchaudio_available():
        pytest.fail(
            "Real-capture prerequisite missing: SwitchAudioSource CLI is not "
            "installed. This is a setup problem, not a capture regression. "
            f"Install it with: {_INSTALL_SWITCHAUDIO}"
        )

    if not blackhole_available():
        pytest.fail(
            "Real-capture prerequisite missing: BlackHole 2ch virtual audio "
            "device is not installed (not present in the macOS input-device "
            "list). This is a setup problem, not a capture regression. "
            f"Install it with: {_INSTALL_BLACKHOLE}"
        )

    permissions = check_capture_permissions(binary)
    if not permissions.get("screen_recording", False):
        pytest.fail(
            "Real-capture prerequisite missing: the record-capture binary "
            "does not have Screen Recording permission. This is a setup "
            f"problem, not a capture regression. {_GRANT_SCREEN_RECORDING}."
        )
    if not permissions.get("microphone", False):
        pytest.fail(
            "Real-capture prerequisite missing: the record-capture binary "
            "does not have Microphone permission. This is a setup problem, "
            f"not a capture regression. {_GRANT_MICROPHONE}."
        )


@contextmanager
def temporary_input_device(name: str):
    """Switch the macOS default input device for the duration of the block.

    Reads the current default input via ``SwitchAudioSource -t input -c``,
    switches to ``name`` for the duration of the ``with`` block, and restores
    the prior device in ``finally``. The prior device name is logged to
    stderr before the switch so the engineer can recover manually if the
    process dies catastrophically before the ``finally`` runs (per
    technical-considerations section 3, risk: "System default input is not
    restored after a crashing test").

    Restore is best-effort: if the restore command fails, the failure is
    logged to stderr and swallowed so it does not mask the original test
    failure.
    """
    prior_result = subprocess.run(
        ["SwitchAudioSource", "-t", "input", "-c"],
        capture_output=True,
        text=True,
        check=True,
    )
    prior = prior_result.stdout.rstrip("\n")
    print(
        f"[temporary_input_device] saving prior input device: {prior!r}",
        file=sys.stderr,
    )
    subprocess.run(
        ["SwitchAudioSource", "-t", "input", "-s", name],
        check=True,
    )
    try:
        yield
    finally:
        try:
            subprocess.run(
                ["SwitchAudioSource", "-t", "input", "-s", prior],
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(
                f"[temporary_input_device] FAILED to restore prior input "
                f"device {prior!r}: {exc}. Restore it manually with: "
                f"SwitchAudioSource -t input -s {prior!r}",
                file=sys.stderr,
            )


def compute_rms_dbfs(wav_path: Path) -> float:
    """Compute the RMS loudness of a 16-bit PCM WAV file in dBFS.

    Opens the file with stdlib :mod:`wave`, decodes all frames as signed
    16-bit little-endian PCM, and returns ``20 * log10(rms / 32768.0)``.
    Returns ``-math.inf`` for an all-zero (digitally silent) file so the
    caller can compare against a finite threshold without a divide-by-zero.
    """
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    if not frames:
        return -math.inf
    samples = array.array("h")
    samples.frombytes(frames)
    if len(samples) == 0:
        return -math.inf
    # Use floats to avoid overflow on the squared accumulator.
    mean_square = sum(float(s) * float(s) for s in samples) / len(samples)
    rms = math.sqrt(mean_square)
    if rms == 0.0:
        return -math.inf
    return 20.0 * math.log10(rms / 32768.0)


def assert_non_silent(
    wav_path: Path, threshold_dbfs: float = -40.0
) -> None:
    """Assert the WAV at ``wav_path`` has RMS loudness above ``threshold_dbfs``.

    Computes the file's dBFS via :func:`compute_rms_dbfs` and raises an
    ``AssertionError`` (including the measured value and the wav path) when
    the file is at or below the threshold. The failure-message format is
    load-bearing: the spec's verification step greps for it.
    """
    dbfs = compute_rms_dbfs(wav_path)
    assert dbfs > threshold_dbfs, (
        f"RMS = {dbfs} dBFS, expected > {threshold_dbfs} dBFS "
        f"({wav_path})"
    )


def play_audio_async(
    wav_path: Path, device: str | None = None
) -> subprocess.Popen:
    """Spawn ``afplay`` on ``wav_path`` and return the Popen handle.

    Does not block: the caller is responsible for ``terminate()`` /
    ``wait()`` once the capture window closes. When ``device`` is provided
    the audio is routed to that named output via ``afplay -d <device>``;
    otherwise it lands on the default output. Both stdout and stderr are
    redirected to ``DEVNULL`` so afplay's chatter does not pollute pytest's
    captured output.
    """
    cmd: list[str] = ["afplay"]
    if device is not None:
        cmd.extend(["-d", device])
    cmd.append(str(wav_path))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
