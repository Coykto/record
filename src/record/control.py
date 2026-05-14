"""Unix-domain socket control protocol between the ``record`` CLI and the daemon.

Slice 2 of spec 003 — tech spec §2.5.

Wire shape
----------

One request per line, one response per line. UTF-8 JSON. The daemon closes the
connection after replying so the CLI never has to handle multi-response
streams.

Requests use a ``"op"`` discriminator (mirrors ``ipc.py``'s ``"cmd"`` /
``"event"`` style):

    {"op": "start"}
    {"op": "stop"}
    {"op": "status"}
    {"op": "quit", "finalize": true}

Responses
---------

The response shape is intentionally a *single* :class:`ControlResponse` model
with ``status: Literal[...]`` and optional fields, rather than a discriminated
union per status. Reasoning: every call site is "send a request, look at the
``status`` field, branch on the few well-known strings"; a discriminated union
would force every consumer to import every response variant for type-narrowing
even though they share most of the fields. With the flat model the CLI's
``send_request_sync`` returns a single type the caller can pattern-match on.
The shape stays disciplined because ``extra="forbid"`` rejects any field not
in this module.

Stale-socket recovery
---------------------

Tech spec §3 risk #4: a previous daemon may have left a stale socket file
behind. :func:`serve` probes the existing socket on startup; if the probe
connects (another daemon is alive) we refuse to start with
:class:`SocketAlreadyBound`. If it errors / times out we unlink the stale
path and bind cleanly.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from . import paths

# ---------------------------------------------------------------------------
# Timeouts (module-top so tests can monkeypatch)
# ---------------------------------------------------------------------------

# Probe used by :func:`serve` on startup to discriminate a live daemon from a
# stale socket file. Short on purpose: a live daemon answers near-instantly.
STALE_PROBE_TIMEOUT_SECONDS: float = 0.5

# Default client-side budgets. ``send_request`` accepts overrides; these are
# the values the CLI uses for every call except ``quit`` (which may take
# longer because the daemon finalises an in-flight capture before responding).
DEFAULT_CONNECT_TIMEOUT_SECONDS: float = 5.0
DEFAULT_RESPONSE_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DaemonUnreachable(RuntimeError):
    """Raised by the client when the daemon's socket cannot be reached.

    Covers the FR 2.7 "daemon is not running" branch: missing socket file,
    ``ECONNREFUSED``, generic ``OSError`` on connect, or a response read that
    times out / EOFs before a complete line arrives.
    """


class SocketAlreadyBound(RuntimeError):
    """Raised by :func:`serve` when another live daemon answers the probe."""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["start"] = "start"


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["stop"] = "stop"


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["status"] = "status"


class QuitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["quit"] = "quit"
    finalize: bool = True


ControlRequest = Annotated[
    Union[StartRequest, StopRequest, StatusRequest, QuitRequest],
    Field(discriminator="op"),
]
_request_adapter: TypeAdapter[ControlRequest] = TypeAdapter(ControlRequest)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


# Closed set of status strings the daemon may emit. New states must be added
# here so the CLI can ``Literal``-match on them without falling into the
# default branch.
ControlStatus = Literal[
    "ok",
    "already_running",
    "not_running",
    "busy",
    "capture_in_progress",
    "error",
]


class CaptureState(BaseModel):
    """Capture sub-object inside :attr:`ControlResponse.capture` for status.

    Mirrors tech spec §2.6. ``running=False`` collapses every other field to
    ``None`` on the wire — :func:`serialize_response` strips ``None`` values.
    """

    model_config = ConfigDict(extra="forbid")

    running: bool
    started_at: str | None = None
    duration_seconds: float | None = None
    audio_path: str | None = None
    video_path: str | None = None


class DaemonInfo(BaseModel):
    """Daemon sub-object inside the status payload (tech spec §2.6)."""

    model_config = ConfigDict(extra="forbid")

    running: bool
    pid: int | None = None
    started_at: str | None = None
    autostart_registered: bool = False


class HotkeyInfo(BaseModel):
    """Hotkey sub-object inside the status payload (tech spec §2.6).

    Slice 2 stubs ``state`` at ``"unregistered"``; slice 5 fills this in with
    the real Carbon outcome.
    """

    model_config = ConfigDict(extra="forbid")

    configured: str | None = None
    state: Literal[
        "unregistered",
        "registered",
        "conflict",
        "invalid",
        "disabled_no_permission",
    ] = "unregistered"
    message: str | None = None


class ControlResponse(BaseModel):
    """Single shape for every control-socket response.

    Per the module docstring: a flat optional-field model rather than a
    discriminated union, gated by ``extra="forbid"``. ``status`` is the only
    always-present field; everything else is request-specific.
    """

    model_config = ConfigDict(extra="forbid")

    status: ControlStatus
    detail: str | None = None

    # start
    capture_id: str | None = None
    audio_path: str | None = None
    video_path: str | None = None

    # status payload (tech spec §2.6)
    daemon: DaemonInfo | None = None
    hotkey: HotkeyInfo | None = None
    capture: CaptureState | None = None


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def parse_request(line: str) -> ControlRequest:
    """Parse one JSON line into the matching request model.

    Raises :class:`pydantic.ValidationError` (subclass of :class:`ValueError`)
    on malformed input or unknown discriminator — same convention as ``ipc.py``.
    """
    return _request_adapter.validate_json(line)


def serialize_request(req: ControlRequest) -> str:
    """Serialize a request to a single JSON line (no trailing newline)."""
    return _request_adapter.dump_json(req, exclude_none=True).decode("utf-8")


def serialize_response(resp: ControlResponse) -> str:
    """Serialize a response to a single JSON line (no trailing newline).

    ``exclude_none=True`` keeps the wire payload tight — for an
    ``already_running`` start reply we don't want a phantom ``daemon: null``
    leaking through.
    """
    return resp.model_dump_json(exclude_none=True)


def parse_response(line: str) -> ControlResponse:
    """Parse one JSON line into a :class:`ControlResponse`."""
    return ControlResponse.model_validate_json(line)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


Handler = Callable[[ControlRequest], Awaitable[ControlResponse]]


async def _probe_socket(path: Path, *, timeout: float) -> bool:
    """Return ``True`` if something accepts connections on ``path``.

    Best-effort: any error / timeout is treated as "nothing alive there".
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=timeout
        )
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError, OSError):
        return False
    # Connection succeeded — close it without sending anything and report
    # "live". A real client would now send a request, but for the probe we
    # just want the binary answer.
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # pragma: no cover - defensive
            pass
    except Exception:  # pragma: no cover - defensive
        pass
    del reader
    return True


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: Handler,
) -> None:
    """Read one line, dispatch to ``handler``, write one line, close.

    A malformed line yields a ``status="error"`` response and the connection
    still closes cleanly — never raises out to the server loop.
    """
    try:
        raw = await reader.readline()
        if not raw:
            # Client closed without sending anything. Nothing to do.
            return

        try:
            req = parse_request(raw.decode("utf-8", errors="replace").strip())
        except (ValidationError, ValueError) as exc:
            resp: ControlResponse = ControlResponse(
                status="error", detail=f"malformed request: {exc}"
            )
        else:
            try:
                resp = await handler(req)
            except Exception as exc:  # pragma: no cover - defensive
                resp = ControlResponse(
                    status="error", detail=f"handler raised {type(exc).__name__}: {exc}"
                )

        writer.write((serialize_response(resp) + "\n").encode("utf-8"))
        try:
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Client went away before reading the reply — common when a slow
            # daemon meets an impatient CLI. Not an error from our side.
            pass
    finally:
        try:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover - defensive
                pass
        except Exception:  # pragma: no cover - defensive
            pass


async def serve(
    handler: Handler,
    *,
    socket_path: Path | None = None,
) -> asyncio.AbstractServer:
    """Bind and start the Unix-domain control socket.

    Returns a started :class:`asyncio.AbstractServer`. The caller is
    responsible for keeping a reference to it (so it isn't GC'd) and for
    closing it on shutdown.

    Stale-socket recovery: if ``socket_path`` already exists, we probe it
    briefly. A live response → :class:`SocketAlreadyBound`. A dead/timeout
    response → unlink and proceed.

    Permission mode 0o600 is applied via ``os.chmod`` after bind —
    :func:`asyncio.start_unix_server` does not accept a ``mode`` argument.
    """
    target = socket_path if socket_path is not None else paths.daemon_socket()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        alive = await _probe_socket(target, timeout=STALE_PROBE_TIMEOUT_SECONDS)
        if alive:
            raise SocketAlreadyBound(
                f"another daemon is already listening on {target}"
            )
        # Stale — clear it before bind.
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    async def _client_cb(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _handle_client(reader, writer, handler)

    server = await asyncio.start_unix_server(_client_cb, path=str(target))

    # Tighten permissions — asyncio.start_unix_server's default is the
    # process umask, which is usually 0o022 → world-readable.
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - defensive
        pass

    return server


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


async def send_request(
    req: ControlRequest,
    *,
    socket_path: Path | None = None,
    timeout_connect: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    timeout_response: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
) -> ControlResponse:
    """Open the socket, send one request, read one response, close.

    Translates every "daemon not reachable" failure mode into
    :class:`DaemonUnreachable` so the CLI has a single branch for FR 2.7.
    """
    target = socket_path if socket_path is not None else paths.daemon_socket()

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(target)),
            timeout=timeout_connect,
        )
    except FileNotFoundError as exc:
        raise DaemonUnreachable(f"socket not found: {target}") from exc
    except ConnectionRefusedError as exc:
        raise DaemonUnreachable(f"connection refused: {target}") from exc
    except asyncio.TimeoutError as exc:
        raise DaemonUnreachable(f"connect timed out: {target}") from exc
    except OSError as exc:
        # ENOENT can also surface as OSError on some platforms; bucket every
        # connect-side OSError into the unreachable signal.
        if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
            raise DaemonUnreachable(f"socket unreachable: {target} ({exc})") from exc
        raise DaemonUnreachable(f"socket error: {target} ({exc})") from exc

    try:
        writer.write((serialize_request(req) + "\n").encode("utf-8"))
        await writer.drain()

        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout_response)
        except asyncio.TimeoutError as exc:
            raise DaemonUnreachable("response timed out") from exc

        if not raw:
            raise DaemonUnreachable("daemon closed connection without responding")

        try:
            return parse_response(raw.decode("utf-8", errors="replace").strip())
        except (ValidationError, ValueError) as exc:
            raise DaemonUnreachable(f"malformed response: {exc}") from exc
    finally:
        try:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover - defensive
                pass
        except Exception:  # pragma: no cover - defensive
            pass


def send_request_sync(
    req: ControlRequest,
    *,
    socket_path: Path | None = None,
    timeout_connect: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    timeout_response: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
) -> ControlResponse:
    """Synchronous wrapper around :func:`send_request` for the Typer CLI.

    The CLI commands are synchronous (Typer doesn't natively support async
    command bodies), so we wrap the coroutine in :func:`asyncio.run` here.
    Always allocates a fresh event loop — never assumes one is running.
    """
    return asyncio.run(
        send_request(
            req,
            socket_path=socket_path,
            timeout_connect=timeout_connect,
            timeout_response=timeout_response,
        )
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    # Exceptions
    "DaemonUnreachable",
    "SocketAlreadyBound",
    # Requests
    "StartRequest",
    "StopRequest",
    "StatusRequest",
    "QuitRequest",
    "ControlRequest",
    # Response
    "ControlResponse",
    "ControlStatus",
    "CaptureState",
    "DaemonInfo",
    "HotkeyInfo",
    # Wire helpers
    "parse_request",
    "serialize_request",
    "parse_response",
    "serialize_response",
    # Server / client
    "Handler",
    "serve",
    "send_request",
    "send_request_sync",
    # Timeouts
    "STALE_PROBE_TIMEOUT_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_RESPONSE_TIMEOUT_SECONDS",
]
