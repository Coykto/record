"""End-to-end integration tests for spec 008 slice 3: session-folder renaming.

Spawns the real ``record.daemon`` against the real ``record-capture`` Swift
binary (synthetic-source flags so no TCC grants are required), drives a
``start`` / ``stop`` cycle, and verifies the orchestrator's post-transcription
rename behaviour against a **fake** ``claude`` CLI dropped onto a tmp ``PATH``
directory.

Two cases:

* **Happy path** — the fake ``claude`` prints a known kebab-case description on
  stdout. After the detached transcription task lands the three transcript
  files, ``naming.try_rename_session_folder`` shells out, validates the
  description, and renames the session folder in place. The test asserts the
  original timestamp-only folder is gone, the renamed folder exists, and the
  three source artifacts (``mic.wav``, ``system.wav``, ``combined.wav``) still
  live inside.
* **Failure path** — the fake ``claude`` exits non-zero. The rename fails, the
  exception is swallowed, and the session folder stays at its timestamp-only
  name with all files intact and no ``<timestamp>-*`` sibling created.

Mechanics intentionally mirror
``test_end_to_end_daemon_auto_transcription_on_finalize`` (Spec 004 slice 2):
the same Deepgram HTTP stub feeds the canned non-silent transcript, the same
sandboxed ``$HOME`` boilerplate keeps ``AF_UNIX`` paths short, and the same
``_wait_for_socket`` / ``_send_control_request`` helpers drive the daemon
control socket. We add the fake ``claude`` script via the ``PATH`` env var the
daemon subprocess inherits — ``asyncio.create_subprocess_exec`` honours that
``PATH``, so the stub wins over any real ``claude`` on the developer's machine.

Driving the transcript content: the canned Deepgram response in
``test_end_to_end.py`` already yields a non-silent transcript (``"hello there"``),
which is what we want for the description-generation path. We let the daemon's
natural code path run end-to-end — start → stop → background transcription →
``try_rename_session_folder`` — and only the ``claude`` shell-out is faked.
This is the more realistic of the two driving options the slice 3 task lists.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from tests.integration.test_end_to_end import (
    _send_control_request,
    _wait_for_socket,
)


# Canned non-silent Deepgram response: yields a transcript with one segment
# carrying the text "hello there". This drives the daemon into the
# description-generation branch (not the silent-rename branch).
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
        if length:
            self.rfile.read(length)
        body = json.dumps(_CANNED_DEEPGRAM_RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _install_fake_claude(parent: Path, script_body: str) -> Path:
    """Write a fake ``claude`` script under ``parent/bin/`` and return its dir.

    The script body is wrapped in a bash shebang. Caller is responsible for
    prepending the returned directory to the daemon subprocess's ``PATH``.
    """
    bin_dir = parent / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(f"#!/bin/bash\n{script_body}\n", encoding="utf-8")
    script.chmod(
        script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )
    return bin_dir


def _wait_until(predicate, *, timeout: float, interval: float = 0.1) -> bool:
    """Poll ``predicate`` up to ``timeout`` seconds; return True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _run_naming_capture(
    capture_binary: Path,
    fake_claude_script_body: str,
) -> tuple[Path, Path, Path, Path, Path]:
    """Shared driver: spawn daemon, start/stop, return key paths.

    Returns ``(cwd_resolved, session_dir, mic_path, system_path, combined_path)``
    after the rename has had a chance to run. The caller asserts the
    rename outcome (folder presence / absence).

    All sandbox bookkeeping (tmp dir, HTTP stub, daemon subprocess) is cleaned
    up before this function returns; the returned paths point at files the
    test must inspect *after* cleanup is structurally complete (the session
    directory and its contents live under ``cwd``, which we deliberately do
    NOT delete here — the caller's tmp parent owns it).
    """
    # The HTTP stub is the seam that injects a non-silent transcript without
    # needing a real audio source; same pattern Spec 004 slice 2 uses.
    httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _DeepgramStubHandler)
    stub_thread = threading.Thread(
        target=httpd.serve_forever, name="deepgram-stub", daemon=True
    )
    stub_thread.start()

    sandbox = Path(tempfile.mkdtemp(prefix="rd-", dir="/tmp"))
    daemon_proc: subprocess.Popen[str] | None = None
    try:
        stub_port = httpd.server_address[1]
        stub_endpoint = f"http://127.0.0.1:{stub_port}/v1/listen"

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

        # Fake ``claude`` on a tmp ``PATH`` directory. The bin dir is
        # prepended to ``PATH`` for the daemon subprocess only (the surrounding
        # pytest process is not touched, so other tests are unaffected).
        bin_dir = _install_fake_claude(sandbox, fake_claude_script_body)

        env = dict(os.environ)
        env["HOME"] = str(sandbox)
        env["RECORD_CAPTURE_TEST_FLAGS"] = (
            "--test-silent-sources --test-synthetic-video"
        )
        env["RECORD_DEEPGRAM_API_KEY"] = "test-key-not-real"
        env["RECORD_DEEPGRAM_ENDPOINT"] = stub_endpoint
        # Prepend the fake-``claude`` bin dir so it wins over any real
        # ``claude`` installed on the developer's machine. We keep the rest of
        # ``PATH`` so the daemon still finds ``afplay``, ``sw_vers``, etc.
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

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

        if not _wait_for_socket(socket_path, timeout=10.0):
            stderr_dump = ""
            try:
                if daemon_proc.stderr is not None:
                    stderr_dump = daemon_proc.stderr.read() or ""
            except Exception:
                pass
            pytest.fail(
                f"daemon did not bind socket at {socket_path} within 10s; "
                f"stderr:\n{stderr_dump}"
            )

        # ----- start -----------------------------------------------------
        start_resp = _send_control_request(socket_path, {"op": "start"})
        assert start_resp["status"] == "ok", start_resp
        audio_path = Path(start_resp["audio_path"])
        audio_paths = start_resp.get("audio_paths") or {}
        system_audio_path = Path(audio_paths["system_audio"])
        session_dir = audio_path.parent
        assert session_dir.parent == cwd_resolved, session_dir

        # Let the synthetic capture produce real frames.
        time.sleep(1.5)

        # ----- stop ------------------------------------------------------
        stop_resp = _send_control_request(
            socket_path, {"op": "stop"}, timeout=30.0
        )
        assert stop_resp["status"] == "ok", stop_resp

        combined_path = session_dir / "combined.wav"

        # ----- wait for transcription + rename to complete ---------------
        # The detached transcription task runs in the daemon's event loop; the
        # rename fires immediately after ``write_transcript`` returns. The
        # success branch removes ``session_dir`` (replaced by the renamed
        # sibling); the failure branch leaves it in place but the transcript
        # files are the last thing written before the rename attempt, so
        # waiting for them is the right barrier in both cases.
        transcript_json = session_dir / "transcript.json"

        renamed_dir_appeared = _wait_until(
            lambda: any(
                p.is_dir()
                and p.name.startswith(session_dir.name + "-")
                for p in cwd_resolved.iterdir()
            ),
            timeout=15.0,
        )

        if not renamed_dir_appeared:
            # Failure case: the rename did not happen. Confirm the session
            # folder still exists with its transcript files, then return.
            assert session_dir.is_dir(), (
                f"session folder vanished without being renamed: {session_dir}; "
                f"cwd contents: {sorted(p.name for p in cwd_resolved.iterdir())!r}"
            )
            # Make sure transcription itself ran to completion before we
            # declare the rename "failed" — otherwise the assertion would race.
            assert _wait_until(
                lambda: transcript_json.exists(), timeout=10.0
            ), (
                f"transcript.json never materialised at {transcript_json}; "
                f"cwd contents: {sorted(p.name for p in cwd_resolved.iterdir())!r}"
            )

        # ----- quit ------------------------------------------------------
        try:
            quit_resp = _send_control_request(socket_path, {"op": "quit"})
            assert quit_resp["status"] == "ok"
        except Exception:
            # If the daemon already exited, that's also fine.
            pass
        try:
            daemon_proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pytest.fail("daemon did not exit within 10s after quit request")

        return (
            cwd_resolved,
            session_dir,
            audio_path,
            system_audio_path,
            combined_path,
        )
    finally:
        if daemon_proc is not None:
            if daemon_proc.poll() is None:
                try:
                    daemon_proc.send_signal(15)
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

        httpd.shutdown()
        httpd.server_close()
        stub_thread.join(timeout=2.0)

        # Intentionally do NOT remove ``sandbox`` here — the caller's
        # assertions need to inspect the on-disk files. The caller cleans up
        # via a finally block.


def test_session_naming_happy_path_renames_folder(capture_binary: Path) -> None:
    """Non-silent transcript + cooperative fake ``claude`` → folder renamed.

    Drives the full real-binary path. After stop, the daemon's detached
    transcription task writes ``transcript.{json,txt,srt}`` and then shells
    out to ``claude -p``. Our fake ``claude`` script drains stdin and prints
    a fixed, regex-valid description; the orchestrator renames the session
    folder in place. The test asserts:

    * the original ``<timestamp>/`` folder is gone,
    * a ``<timestamp>-pricing-call-with-acme/`` sibling exists,
    * the three source artifacts (``mic.wav``, ``system.wav``, ``combined.wav``)
      and the transcript files all live inside the renamed folder.
    """
    sandboxes_to_clean: list[Path] = []
    try:
        (
            cwd_resolved,
            session_dir,
            audio_path,
            system_audio_path,
            combined_path,
        ) = _run_naming_capture(
            capture_binary,
            # Drain stdin so the parent's write never SIGPIPEs us, then emit
            # the exact description we expect the orchestrator to accept.
            'cat > /dev/null\necho -n "pricing-call-with-acme"',
        )
        # The sandbox is the cwd's parent (sandbox/out/); track its great-
        # grandparent for cleanup.
        sandboxes_to_clean.append(cwd_resolved.parent)

        expected_renamed = cwd_resolved / f"{session_dir.name}-pricing-call-with-acme"

        # Allow a brief grace period for the rename to land if it hasn't yet —
        # _run_naming_capture already waited on its appearance, but the
        # filesystem rename and our directory scan can race in unfriendly ways.
        assert _wait_until(lambda: expected_renamed.is_dir(), timeout=5.0), (
            f"renamed folder {expected_renamed} did not appear; "
            f"cwd contents: {sorted(p.name for p in cwd_resolved.iterdir())!r}"
        )

        # Original folder must be gone.
        assert not session_dir.exists(), (
            f"original session folder still exists at {session_dir}; "
            f"cwd contents: {sorted(p.name for p in cwd_resolved.iterdir())!r}"
        )

        # Source artifacts moved with the folder.
        assert (expected_renamed / "mic.wav").exists()
        assert (expected_renamed / "system.wav").exists()
        assert (expected_renamed / "combined.wav").exists()
        # Sanity: the transcript triple also went along for the ride.
        assert (expected_renamed / "transcript.json").exists()
        assert (expected_renamed / "transcript.txt").exists()
        assert (expected_renamed / "transcript.srt").exists()

        # The paths returned from the start response are now stale (the
        # orchestrator's CLI surface explicitly accepts this — functional spec
        # §2.7). Pin that the staleness manifests the way we expect: the
        # *old* paths do not exist anymore.
        assert not audio_path.exists()
        assert not system_audio_path.exists()
        assert not combined_path.exists()
    finally:
        for sandbox in sandboxes_to_clean:
            shutil.rmtree(sandbox, ignore_errors=True)


def test_session_naming_claude_nonzero_keeps_folder(capture_binary: Path) -> None:
    """Fake ``claude`` exits non-zero → folder keeps its timestamp-only name.

    Same setup as the happy path, but the fake script writes to stderr and
    exits 1. ``generate_description`` raises ``RuntimeError``; the rename
    function catches it, logs ``session_rename_failed``, and returns. The
    session folder stays at its timestamp-only name with every file intact.
    """
    sandboxes_to_clean: list[Path] = []
    try:
        (
            cwd_resolved,
            session_dir,
            audio_path,
            system_audio_path,
            combined_path,
        ) = _run_naming_capture(
            capture_binary,
            'cat > /dev/null\necho "boom" >&2\nexit 1',
        )
        sandboxes_to_clean.append(cwd_resolved.parent)

        # Give the orchestrator a moment to complete the (failing) rename
        # attempt. The failure path is fast — no rename actually occurs — so
        # a short poll on the *absence* of any renamed sibling is enough.
        time.sleep(0.5)

        # The original session folder still exists at its timestamp-only name.
        assert session_dir.is_dir(), (
            f"session folder vanished even though claude failed: {session_dir}; "
            f"cwd contents: {sorted(p.name for p in cwd_resolved.iterdir())!r}"
        )

        # No sibling with the ``<timestamp>-<suffix>`` shape was created.
        siblings = [p for p in cwd_resolved.iterdir() if p.name != session_dir.name]
        renamed_candidates = [
            p for p in siblings if p.name.startswith(session_dir.name + "-")
        ]
        assert not renamed_candidates, (
            f"unexpected renamed sibling(s) {renamed_candidates} created "
            f"despite claude failure"
        )

        # All session files still inside the original folder.
        assert audio_path.exists(), f"mic.wav missing from {session_dir}"
        assert system_audio_path.exists(), (
            f"system.wav missing from {session_dir}"
        )
        assert combined_path.exists(), (
            f"combined.wav missing from {session_dir}"
        )
        assert (session_dir / "transcript.json").exists()
        assert (session_dir / "transcript.txt").exists()
        assert (session_dir / "transcript.srt").exists()
    finally:
        for sandbox in sandboxes_to_clean:
            shutil.rmtree(sandbox, ignore_errors=True)
