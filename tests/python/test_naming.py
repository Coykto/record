"""Tests for :mod:`record.naming` — silent + description-driven renaming (spec 008).

Exercises ``is_silent``, ``atomic_rename``, ``validate_description``,
``generate_description``, and ``try_rename_session_folder`` against a real
filesystem under ``tmp_path``. The ``claude`` CLI is faked by dropping a tiny
script into a tmp ``PATH`` directory per-test so the subprocess plumbing is
real but deterministic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path

import pytest

from record import naming
from record.transcribe import Segment, Transcript


def _transcript(*texts: str) -> Transcript:
    """Build a Transcript whose segments carry the given ``text`` values."""
    return Transcript(
        provider="deepgram",
        model="nova-3",
        duration_seconds=float(len(texts)),
        segments=[
            Segment(speaker="Speaker 1", start=float(i), end=float(i + 1), text=t)
            for i, t in enumerate(texts)
        ],
    )


def _install_fake_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script_body: str,
) -> Path:
    """Write a fake ``claude`` shell script and prepend its dir to ``PATH``.

    The script body is wrapped with a bash shebang. Returns the script path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(f"#!/bin/bash\n{script_body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return script


# ---------------------------------------------------------------------------
# is_silent
# ---------------------------------------------------------------------------


def test_is_silent_empty_segments() -> None:
    assert naming.is_silent(_transcript()) is True


def test_is_silent_all_whitespace_segments() -> None:
    assert naming.is_silent(_transcript("", "   \n\t  ")) is True


def test_is_silent_one_nonempty_among_silent() -> None:
    assert naming.is_silent(_transcript("", "   ", "hi")) is False


def test_is_silent_single_nonempty_segment() -> None:
    assert naming.is_silent(_transcript("hello")) is False


# ---------------------------------------------------------------------------
# atomic_rename
# ---------------------------------------------------------------------------


def test_atomic_rename_happy_path(tmp_path: Path) -> None:
    src = tmp_path / "2026-05-16T10-00-00"
    src.mkdir()
    (src / "marker").write_text("x", encoding="utf-8")

    new_path = naming.atomic_rename(src, naming.SILENT_SUFFIX)

    expected = tmp_path / "2026-05-16T10-00-00-silent"
    assert new_path == expected
    assert not src.exists()
    assert expected.is_dir()
    assert (expected / "marker").read_text(encoding="utf-8") == "x"


def test_atomic_rename_target_collision_raises_and_preserves_source(
    tmp_path: Path,
) -> None:
    src = tmp_path / "2026-05-16T10-00-00"
    src.mkdir()
    collision = tmp_path / "2026-05-16T10-00-00-silent"
    collision.mkdir()

    with pytest.raises(FileExistsError):
        naming.atomic_rename(src, naming.SILENT_SUFFIX)

    assert src.is_dir()
    assert collision.is_dir()


def test_atomic_rename_missing_source_raises(tmp_path: Path) -> None:
    src = tmp_path / "does-not-exist"
    target = tmp_path / "does-not-exist-silent"

    with pytest.raises(OSError):
        naming.atomic_rename(src, naming.SILENT_SUFFIX)

    assert not target.exists()


# ---------------------------------------------------------------------------
# validate_description
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "pricing-call-with-acme",
        "weekly-1-1-with-anna",
        "q3-review",
        "a-b",
    ],
)
def test_validate_description_accepts(value: str) -> None:
    assert naming.validate_description(value) == value


def test_validate_description_strips_single_trailing_newline() -> None:
    assert naming.validate_description("pricing-call-with-acme\n") == (
        "pricing-call-with-acme"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "hello",                       # single token
        "Hello-world",                 # uppercase
        "hello-world ",                # trailing space
        "hello world",                 # embedded space
        "hello!-world",                # punctuation
        '"hello-world"',               # quotes
        "foo/bar",                     # slash
        "../foo",                      # path traversal
        "hello.world",                 # dot in token
        # 7 tokens × 9 chars + 6 hyphens = 69 chars → >60 *and* too-many tokens.
        "-".join(["abcdefghi"] * 7),
        # 7 tokens of length 1, well under 60 chars, but >6 tokens.
        "a-b-c-d-e-f-g",
        "café-call",                   # accents
    ],
)
def test_validate_description_rejects(value: str) -> None:
    with pytest.raises(ValueError):
        naming.validate_description(value)


# ---------------------------------------------------------------------------
# generate_description
# ---------------------------------------------------------------------------


def test_generate_description_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        # Drain stdin so the parent's write doesn't hit SIGPIPE, then emit
        # a fixed kebab-case description.
        'cat > /dev/null\necho -n "pricing-call-with-acme"',
    )

    out = asyncio.run(naming.generate_description("some transcript text"))

    assert out == "pricing-call-with-acme"


def test_generate_description_nonzero_exit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        'cat > /dev/null\necho "something went wrong" >&2\nexit 7',
    )

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(naming.generate_description("hi"))

    assert "something went wrong" in str(excinfo.value)


def test_generate_description_empty_stdout_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        "cat > /dev/null\nexit 0",
    )

    with pytest.raises(RuntimeError):
        asyncio.run(naming.generate_description("hi"))


def test_generate_description_timeout_raises_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Record the PID as the first action so the timeout never races past it.
    pid_file = tmp_path / "child.pid"
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        f'echo -n "$$" > "{pid_file}"\nsleep 10',
    )
    monkeypatch.setattr(naming, "TIMEOUT_S", 0.5)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(naming.generate_description("hi"))

    assert pid_file.exists(), "stub did not run far enough to record its pid"
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    # Poll briefly: SIGKILL delivery vs. OS reaping is asynchronous.
    import time as _time

    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.02)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_generate_description_truncates_stdin_to_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count_file = tmp_path / "stdin_bytes.txt"
    # The stub records the byte count of stdin to a side file, then emits a
    # valid-looking marker so the function returns successfully.
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        f'wc -c > "{count_file}"\necho -n "ok-marker-here"',
    )

    big = "a" * (naming.MAX_TRANSCRIPT_CHARS + 5000)
    out = asyncio.run(naming.generate_description(big))

    assert out == "ok-marker-here"
    stdin_bytes = int(count_file.read_text(encoding="utf-8").strip())
    assert stdin_bytes == naming.MAX_TRANSCRIPT_CHARS


def test_generate_description_missing_cli_propagates_filenotfound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Empty PATH → ``claude`` cannot be located → FileNotFoundError.
    monkeypatch.setenv("PATH", str(tmp_path / "nonexistent-bin"))

    with pytest.raises(FileNotFoundError):
        asyncio.run(naming.generate_description("hi"))


# ---------------------------------------------------------------------------
# try_rename_session_folder
# ---------------------------------------------------------------------------


def test_try_rename_silent_transcript_renames_folder(tmp_path: Path) -> None:
    session = tmp_path / "2026-05-16T11-00-00"
    session.mkdir()
    (session / "combined.wav").write_bytes(b"RIFF")

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("", "   ")
        )
    )

    renamed = tmp_path / "2026-05-16T11-00-00-silent"
    assert not session.exists()
    assert renamed.is_dir()
    assert (renamed / "combined.wav").read_bytes() == b"RIFF"


def test_try_rename_nonsilent_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "2026-05-16T12-00-00"
    session.mkdir()
    (session / "combined.wav").write_bytes(b"RIFF")
    (session / "transcript.txt").write_text(
        "team sync about quarterly planning", encoding="utf-8"
    )
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        'cat > /dev/null\necho -n "team-sync-with-bob"',
    )

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("team sync with bob")
        )
    )

    renamed = tmp_path / "2026-05-16T12-00-00-team-sync-with-bob"
    assert not session.exists()
    assert renamed.is_dir()
    assert (renamed / "combined.wav").read_bytes() == b"RIFF"
    assert (renamed / "transcript.txt").exists()


def _assert_one_rename_failed_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    matching = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and rec.name == "record.naming"
        and "session_rename_failed" in rec.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one `session_rename_failed` WARNING; "
        f"saw: {[(r.name, r.levelname, r.getMessage()) for r in caplog.records]!r}"
    )


def test_try_rename_nonsilent_cli_missing_keeps_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = tmp_path / "2026-05-16T13-00-00"
    session.mkdir()
    (session / "combined.wav").write_bytes(b"RIFF")
    (session / "transcript.txt").write_text("hello world", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    caplog.set_level(logging.WARNING, logger="record.naming")

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("hello world")
        )
    )

    assert session.is_dir()
    assert (session / "transcript.txt").exists()
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == ["2026-05-16T13-00-00"]
    _assert_one_rename_failed_warning(caplog)


def test_try_rename_nonsilent_cli_nonzero_keeps_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = tmp_path / "2026-05-16T14-00-00"
    session.mkdir()
    (session / "transcript.txt").write_text("hello world", encoding="utf-8")
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        'cat > /dev/null\necho "boom" >&2\nexit 9',
    )
    caplog.set_level(logging.WARNING, logger="record.naming")

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("hi")
        )
    )

    assert session.is_dir()
    _assert_one_rename_failed_warning(caplog)


def test_try_rename_nonsilent_timeout_keeps_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = tmp_path / "2026-05-16T15-00-00"
    session.mkdir()
    (session / "transcript.txt").write_text("hello world", encoding="utf-8")
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        'cat > /dev/null\nsleep 5\necho -n "too-late"',
    )
    monkeypatch.setattr(naming, "TIMEOUT_S", 0.2)
    caplog.set_level(logging.WARNING, logger="record.naming")

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("hi")
        )
    )

    assert session.is_dir()
    _assert_one_rename_failed_warning(caplog)


def test_try_rename_nonsilent_invalid_output_keeps_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = tmp_path / "2026-05-16T16-00-00"
    session.mkdir()
    (session / "transcript.txt").write_text("hello world", encoding="utf-8")
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        'cat > /dev/null\necho -n "Hello World"',
    )
    caplog.set_level(logging.WARNING, logger="record.naming")

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("hi")
        )
    )

    assert session.is_dir()
    _assert_one_rename_failed_warning(caplog)


def test_try_rename_nonsilent_collision_keeps_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = tmp_path / "2026-05-16T17-00-00"
    session.mkdir()
    (session / "transcript.txt").write_text("hello world", encoding="utf-8")
    # A folder at the rename target already exists → collision.
    collision = tmp_path / "2026-05-16T17-00-00-team-sync-with-bob"
    collision.mkdir()
    _install_fake_claude(
        tmp_path,
        monkeypatch,
        'cat > /dev/null\necho -n "team-sync-with-bob"',
    )
    caplog.set_level(logging.WARNING, logger="record.naming")

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("hi")
        )
    )

    assert session.is_dir()
    assert collision.is_dir()
    _assert_one_rename_failed_warning(caplog)


def test_try_rename_collision_logs_warning_and_keeps_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = tmp_path / "2026-05-16T13-00-00"
    session.mkdir()
    (session / "combined.wav").write_bytes(b"RIFF")
    collision = tmp_path / "2026-05-16T13-00-00-silent"
    collision.mkdir()

    caplog.set_level(logging.WARNING, logger="record.naming")

    asyncio.run(
        naming.try_rename_session_folder(
            session_dir=session, transcript=_transcript("")
        )
    )

    assert session.is_dir()
    assert collision.is_dir()
    assert (session / "combined.wav").read_bytes() == b"RIFF"

    matching = [
        rec for rec in caplog.records
        if "session_rename_failed" in rec.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one `session_rename_failed` WARNING; "
        f"saw: {[r.getMessage() for r in caplog.records]!r}"
    )
