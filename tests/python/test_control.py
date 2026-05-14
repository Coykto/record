"""Tests for the control-socket request/response protocol.

Drives :func:`record.control.serve` against a stub handler over a real
Unix-domain socket (bound in a short ``mkdtemp`` directory to dodge macOS's
104-char ``AF_UNIX`` path limit). Each test sends one request, asserts the
response shape, and tears the server down.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from record import control


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def short_tmp() -> Any:
    """Yield a short-path tempdir suitable for AF_UNIX paths on macOS."""
    d = Path(tempfile.mkdtemp(prefix="rct-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _run(coro: Any) -> Any:
    """Run an async coroutine in a fresh event loop and return its result."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Request / response framing
# ---------------------------------------------------------------------------


def test_parse_request_round_trips_each_op() -> None:
    """Every op string parses to the matching model with extra="forbid"."""
    for line, expected_type in [
        ('{"op":"start"}', control.StartRequest),
        ('{"op":"stop"}', control.StopRequest),
        ('{"op":"status"}', control.StatusRequest),
        ('{"op":"quit","finalize":true}', control.QuitRequest),
        ('{"op":"quit","finalize":false}', control.QuitRequest),
    ]:
        parsed = control.parse_request(line)
        assert isinstance(parsed, expected_type), f"{line!r} -> {parsed!r}"


def test_quit_request_defaults_finalize_to_true() -> None:
    parsed = control.parse_request('{"op":"quit"}')
    assert isinstance(parsed, control.QuitRequest)
    assert parsed.finalize is True


def test_parse_request_rejects_unknown_op() -> None:
    with pytest.raises(Exception):
        control.parse_request('{"op":"nope"}')


def test_parse_request_rejects_extra_field() -> None:
    with pytest.raises(Exception):
        control.parse_request('{"op":"start","extra":"x"}')


def test_serialize_response_strips_none_fields() -> None:
    """Optional fields default to None; the wire payload must not carry them."""
    resp = control.ControlResponse(status="ok")
    line = control.serialize_response(resp)
    # Sanity: only `status` survives.
    assert line == '{"status":"ok"}'


# ---------------------------------------------------------------------------
# Server round-trips against a stub handler
# ---------------------------------------------------------------------------


async def _send_one(
    socket_path: Path, line: str, *, timeout: float = 2.0
) -> str:
    """Open the socket, send ``line``, read one response line, close."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path)), timeout=timeout
    )
    try:
        writer.write((line + "\n").encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return raw.decode("utf-8").strip()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def test_server_dispatches_start_request(short_tmp: Path) -> None:
    sock = short_tmp / "d.sock"
    captured: list[control.ControlRequest] = []

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        captured.append(req)
        return control.ControlResponse(
            status="ok",
            capture_id="abc",
            audio_path="/abs/x.wav",
            video_path="/abs/x.mp4",
        )

    async def _drive() -> str:
        server = await control.serve(_handler, socket_path=sock)
        try:
            return await _send_one(sock, '{"op":"start"}')
        finally:
            server.close()
            await server.wait_closed()

    raw = _run(_drive())
    resp = control.parse_response(raw)
    assert resp.status == "ok"
    assert resp.capture_id == "abc"
    assert resp.audio_path == "/abs/x.wav"
    assert resp.video_path == "/abs/x.mp4"
    assert len(captured) == 1
    assert isinstance(captured[0], control.StartRequest)


def test_server_dispatches_stop_request(short_tmp: Path) -> None:
    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        assert isinstance(req, control.StopRequest)
        return control.ControlResponse(status="not_running", detail="idle")

    async def _drive() -> str:
        server = await control.serve(_handler, socket_path=sock)
        try:
            return await _send_one(sock, '{"op":"stop"}')
        finally:
            server.close()
            await server.wait_closed()

    raw = _run(_drive())
    resp = control.parse_response(raw)
    assert resp.status == "not_running"
    assert resp.detail == "idle"


def test_server_dispatches_status_request(short_tmp: Path) -> None:
    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        assert isinstance(req, control.StatusRequest)
        return control.ControlResponse(
            status="ok",
            daemon=control.DaemonInfo(running=True, pid=4242),
            hotkey=control.HotkeyInfo(state="unregistered"),
            capture=control.CaptureState(running=False),
        )

    async def _drive() -> str:
        server = await control.serve(_handler, socket_path=sock)
        try:
            return await _send_one(sock, '{"op":"status"}')
        finally:
            server.close()
            await server.wait_closed()

    raw = _run(_drive())
    resp = control.parse_response(raw)
    assert resp.status == "ok"
    assert resp.daemon is not None and resp.daemon.running is True
    assert resp.daemon.pid == 4242
    assert resp.hotkey is not None and resp.hotkey.state == "unregistered"
    assert resp.capture is not None and resp.capture.running is False


def test_server_dispatches_quit_request(short_tmp: Path) -> None:
    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        assert isinstance(req, control.QuitRequest)
        assert req.finalize is True  # default
        return control.ControlResponse(status="ok")

    async def _drive() -> str:
        server = await control.serve(_handler, socket_path=sock)
        try:
            return await _send_one(sock, '{"op":"quit"}')
        finally:
            server.close()
            await server.wait_closed()

    raw = _run(_drive())
    resp = control.parse_response(raw)
    assert resp.status == "ok"


def test_server_returns_error_on_malformed_input(short_tmp: Path) -> None:
    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        # Should NOT be reached for a malformed request.
        raise AssertionError("handler should not have been called")

    async def _drive() -> str:
        server = await control.serve(_handler, socket_path=sock)
        try:
            return await _send_one(sock, "not json at all")
        finally:
            server.close()
            await server.wait_closed()

    raw = _run(_drive())
    resp = control.parse_response(raw)
    assert resp.status == "error"
    assert "malformed" in (resp.detail or "")


def test_server_returns_error_on_unknown_op(short_tmp: Path) -> None:
    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        raise AssertionError("handler should not have been called")

    async def _drive() -> str:
        server = await control.serve(_handler, socket_path=sock)
        try:
            return await _send_one(sock, '{"op":"explode"}')
        finally:
            server.close()
            await server.wait_closed()

    raw = _run(_drive())
    resp = control.parse_response(raw)
    assert resp.status == "error"


def test_server_applies_0600_mode_to_socket(short_tmp: Path) -> None:
    """The socket file must be 0600 so other users can't connect."""
    import stat

    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(status="ok")

    async def _drive() -> None:
        server = await control.serve(_handler, socket_path=sock)
        try:
            assert sock.exists()
            mode = stat.S_IMODE(sock.stat().st_mode)
            assert mode == 0o600, f"expected 0600, got {oct(mode)}"
        finally:
            server.close()
            await server.wait_closed()

    _run(_drive())


# ---------------------------------------------------------------------------
# Stale-socket recovery
# ---------------------------------------------------------------------------


def test_serve_unlinks_stale_socket_file(short_tmp: Path) -> None:
    """A dead (non-socket) file at the path is unlinked; serve binds cleanly."""
    sock = short_tmp / "d.sock"
    sock.write_text("not a socket")

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(status="ok")

    async def _drive() -> None:
        server = await control.serve(_handler, socket_path=sock)
        try:
            assert sock.exists()  # rebind worked
        finally:
            server.close()
            await server.wait_closed()

    _run(_drive())


def test_serve_refuses_when_another_daemon_is_live(short_tmp: Path) -> None:
    """A live server on the same path → SocketAlreadyBound on second serve()."""
    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(status="ok")

    async def _drive() -> None:
        first = await control.serve(_handler, socket_path=sock)
        try:
            with pytest.raises(control.SocketAlreadyBound):
                await control.serve(_handler, socket_path=sock)
        finally:
            first.close()
            await first.wait_closed()

    _run(_drive())


# ---------------------------------------------------------------------------
# Client helper
# ---------------------------------------------------------------------------


def test_send_request_returns_typed_response(short_tmp: Path) -> None:
    """End-to-end client → server → client over a real socket."""
    sock = short_tmp / "d.sock"

    async def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="ok", capture_id="abc"
        )

    async def _drive() -> control.ControlResponse:
        server = await control.serve(_handler, socket_path=sock)
        try:
            return await control.send_request(
                control.StartRequest(), socket_path=sock
            )
        finally:
            server.close()
            await server.wait_closed()

    resp = _run(_drive())
    assert resp.status == "ok"
    assert resp.capture_id == "abc"


def test_send_request_raises_daemon_unreachable_on_missing_socket(
    short_tmp: Path,
) -> None:
    sock = short_tmp / "nope.sock"

    async def _drive() -> None:
        with pytest.raises(control.DaemonUnreachable):
            await control.send_request(control.StatusRequest(), socket_path=sock)

    _run(_drive())


def test_send_request_sync_works_for_typer_callers(short_tmp: Path) -> None:
    """``send_request_sync`` opens its own event loop; safe for the Typer CLI.

    Slight subtlety: we can't run a server in the same event loop because
    ``send_request_sync`` calls ``asyncio.run`` and that requires no running
    loop. So we spin up the server in a thread.
    """
    import threading

    sock = short_tmp / "d.sock"
    server_ready = threading.Event()
    server_done = threading.Event()
    server_holder: dict[str, Any] = {}

    def _serve_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _handler(req: control.ControlRequest) -> control.ControlResponse:
            return control.ControlResponse(status="ok", capture_id="sync")

        async def _boot() -> None:
            server = await control.serve(_handler, socket_path=sock)
            server_holder["s"] = server
            server_ready.set()
            try:
                await asyncio.wait_for(server.serve_forever(), timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        try:
            loop.run_until_complete(_boot())
        except Exception:
            pass
        finally:
            loop.close()
            server_done.set()

    t = threading.Thread(target=_serve_thread, daemon=True)
    t.start()
    assert server_ready.wait(timeout=5.0)

    try:
        resp = control.send_request_sync(
            control.StartRequest(), socket_path=sock
        )
        assert resp.status == "ok"
        assert resp.capture_id == "sync"
    finally:
        # Tell the server loop to stop.
        srv = server_holder.get("s")
        if srv is not None:
            srv.close()
        t.join(timeout=5.0)
