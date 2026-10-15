"""Live local web dashboard: transcript, levels, items, and push/dismiss/edit/agent controls.

The server runs inside the assistant's own event loop, so route handlers call the
:class:`~voicestream.session.Assistant` control API directly, and ``/api/events``
streams every bus event to the page as Server-Sent Events (one-way, reconnects by
itself, no websocket dependency).

It binds 127.0.0.1 by default, but any web page open in your browser can still send requests to
localhost, so:

* every route except ``GET /`` needs the per-run token, as ``X-Voicestream-Token`` or
  ``?token=`` (``EventSource`` can't set headers); this is also the CSRF defence;
* requests whose ``Host`` isn't a loopback name are refused, so a DNS-rebinding page
  (``evil.example`` re-resolved to 127.0.0.1) can't even load the page;
* there are no CORS headers, and ``/api/file`` only serves text files inside the data dir.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
from collections.abc import AsyncIterator, Collection, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import DashboardConfig
from ..models import Event, ItemKind, ItemStatus

if TYPE_CHECKING:
    from ..session import Assistant

log = logging.getLogger(__name__)

TOKEN_HEADER = "x-voicestream-token"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
KEEPALIVE_S = 15.0
"""Comment line sent on an idle event stream so proxies and browsers keep it open."""
LEVEL_INTERVAL_S = 0.2
"""Input meters don't need more than ~5 updates per second per source."""
MAX_FILE_BYTES = 5_000_000
INDEX_HTML = Path(__file__).parent / "static" / "index.html"

# on every response: transcripts must not be cached, and the token in the page URL must
# never leak through a Referer header
_HARDENING = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}


# ------------------------------------------------------------------- request bodies


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


_B = TypeVar("_B", bound=_Body)


class ItemEdit(_Body):
    """``PATCH /api/items/{id}``: what a person may correct (the tracker re-normalizes it)."""

    title: str | None = Field(None, min_length=1)
    detail: str | None = None
    owner: str | None = None
    due_text: str | None = None
    due_date: date | None = None
    status: ItemStatus | None = None
    priority: Literal["low", "medium", "high"] | None = None


class NewItem(_Body):
    kind: ItemKind
    title: str = Field(min_length=1)
    owner: str | None = None
    due_text: str | None = None


class AgentApproval(_Body):
    prompt: str | None = None
    """Edited brief for the agent; empty keeps the suggested one."""


# ------------------------------------------------------------------------ security


def allowed_hosts_for(bind_host: str) -> frozenset[str] | None:
    """Host names the dashboard answers to when bound to ``bind_host`` (``None`` = any).

    Binding one non-loopback address (say a LAN IP) is an explicit choice, so that name is
    allowed too. Binding every interface (``0.0.0.0``/``::``) leaves the machine's names
    unknowable here, so the Host check is off and only the token protects the API.
    """
    host = bind_host.strip().strip("[]").lower()
    if host in ("", "0.0.0.0", "::"):
        return None
    return LOOPBACK_HOSTS | {host}


def _hostname(host_header: str) -> str:
    """``localhost:8765`` → ``localhost``, ``[::1]:8765`` → ``::1``; malformed → ``""``."""
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else ""
    return host.partition(":")[0]


class _Guard:
    """Host check on every request and the token on all but ``GET /``.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so the event stream isn't buffered.
    """

    def __init__(self, app: ASGIApp, token: str, allowed_hosts: Collection[str] | None) -> None:
        self.app = app
        self.token = token.encode()
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        refusal = self._refusal(Request(scope))
        if refusal is not None:
            await refusal(scope, receive, send)
            return

        async def send_hardened(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _HARDENING.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_hardened)

    def _refusal(self, request: Request) -> Response | None:
        if self.allowed_hosts is not None and _hostname(request.headers.get("host", "")) not in self.allowed_hosts:
            return _error(403, "unexpected Host header (open the dashboard via 127.0.0.1 or localhost)")
        if request.scope["path"] == "/":
            return None  # the page itself; it reads the token from its own URL
        given = request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or ""
        # bytes, not str: compare_digest raises on non-ASCII strings
        if not secrets.compare_digest(given.encode(), self.token):
            return _error(401, "missing or wrong token: open the dashboard URL printed in the terminal")
        return None


# -------------------------------------------------------------------------- errors


def _error(status: int, message: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return _error(exc.status_code, exc.detail, exc.headers)


async def _not_found(request: Request, exc: LookupError) -> JSONResponse:
    """Only the control API's own lookups are 404s: ``KeyError(item_id)`` for the item in
    the path, a bare ``LookupError`` for "no meeting in progress". Any other KeyError or
    IndexError is a bug, so it is re-raised (500, logged) rather than passed off as "not found"."""
    item_id = request.path_params.get("item_id")
    if type(exc) is KeyError and item_id is not None and exc.args == (item_id,):
        return _error(404, f"unknown item {item_id}")
    if type(exc) is LookupError:
        return _error(404, str(exc) or "not found")
    raise exc


async def _invalid(request: Request, exc: ValidationError) -> JSONResponse:
    problems = (f"{'.'.join(map(str, e['loc']))}: {e['msg']}" if e["loc"] else e["msg"] for e in exc.errors())
    return _error(400, "; ".join(problems))


def _outcome(ok: bool, refusal: str) -> JSONResponse:
    return JSONResponse({"ok": True}) if ok else _error(409, refusal)


def _log_failure(task: asyncio.Future[object]) -> None:
    if not task.cancelled() and (exc := task.exception()):
        log.error("ending the meeting from the dashboard failed", exc_info=exc)


# ---------------------------------------------------------------------------- routes


def _target(request: Request) -> tuple[str, str | None]:
    """Item id from the path; ``?meeting_id=`` picks a finished meeting's item (ids repeat)."""
    return request.path_params["item_id"], request.query_params.get("meeting_id") or None


async def _parse(request: Request, model: type[_B]) -> _B:
    return model.model_validate_json(await request.body() or b"{}")


def _sse(event: Event) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


_SHUTDOWN = Event(type="shutdown")
"""Queued into open event streams to end them (identity-checked, never sent)."""


class _Api:
    def __init__(self, assistant: Assistant, page: str) -> None:
        self.assistant = assistant
        self.page = page
        self.data_root = assistant.cfg.data_path
        self.closed = False
        self._streams: set[asyncio.Queue[Event]] = set()

    def close_streams(self) -> None:
        """End open event streams: a graceful shutdown would otherwise wait for (and then
        cancel, with a logged traceback) every browser tab still connected."""
        self.closed = True
        for queue in self._streams:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(_SHUTDOWN)

    def routes(self) -> list[Route]:
        item = "/api/items/{item_id}"
        return [
            Route("/", self.index, methods=["GET"]),
            Route("/api/state", self.state, methods=["GET"]),
            Route("/api/events", self.events, methods=["GET"]),
            Route("/api/file", self.file, methods=["GET"]),
            Route("/api/items", self.add_item, methods=["POST"]),
            Route(item, self.edit_item, methods=["PATCH"]),
            Route(f"{item}/push", self.push, methods=["POST"]),
            Route(f"{item}/dismiss", self.dismiss, methods=["POST"]),
            Route(f"{item}/restore", self.restore, methods=["POST"]),
            Route(f"{item}/agent/run", self.run_agent, methods=["POST"]),
            Route(f"{item}/agent/reject", self.reject_agent, methods=["POST"]),
            Route("/api/meeting/end", self.end_meeting, methods=["POST"]),
            Route("/api/extract", self.extract, methods=["POST"]),
        ]

    async def index(self, request: Request) -> HTMLResponse:
        # a fresh nonce per load: only our own inline script/style may run
        nonce = secrets.token_urlsafe(16)
        html = self.page.replace("<style>", f'<style nonce="{nonce}">', 1).replace(
            "<script>", f'<script nonce="{nonce}">', 1
        )
        csp = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return HTMLResponse(html, headers={"content-security-policy": csp})

    async def state(self, request: Request) -> JSONResponse:
        return JSONResponse(self.assistant.state())

    async def events(self, request: Request) -> StreamingResponse:
        return StreamingResponse(self._stream(), media_type="text/event-stream", headers={"x-accel-buffering": "no"})

    async def _stream(self) -> AsyncIterator[str]:
        if self.closed:
            return
        loop = asyncio.get_running_loop()
        last_level: dict[str, float] = {}
        # Starlette cancels this generator when the client goes away, which unsubscribes
        async with self.assistant.bus.subscribe() as queue:
            self._streams.add(queue)
            try:
                # subscribed before the snapshot, with no await in between: nothing falls through
                yield "retry: 3000\n" + _sse(Event(type="state", data=self.assistant.state()))
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if event is _SHUTDOWN:
                        return
                    if event.type == "level":
                        source, now = str(event.data.get("source")), loop.time()
                        if now - last_level.get(source, float("-inf")) < LEVEL_INTERVAL_S:
                            continue
                        last_level[source] = now
                    yield _sse(event)
            finally:
                self._streams.discard(queue)

    def file(self, request: Request) -> Response:
        """Notes and agent results. Sync on purpose: Starlette runs it in a worker thread."""
        raw = request.query_params.get("path", "")
        if not raw or "\x00" in raw:
            raise HTTPException(400, "path is required")
        root = self.data_root.resolve()
        try:
            path = (root / raw).resolve()  # an absolute ``raw`` replaces root; symlinks are followed
        except (OSError, ValueError):
            raise HTTPException(400, "invalid path") from None
        # dot-files too: the data dir may hold a .env with API keys
        if not path.is_relative_to(root) or any(p.startswith(".") for p in path.relative_to(root).parts):
            raise HTTPException(403, "only files inside the voicestream data directory can be opened")
        try:
            # stat can fail too (ENAMETOOLONG, EACCES); also keeps FIFOs and devices out of open()
            if not path.is_file():
                raise HTTPException(404, "file not found")
            with path.open("rb") as fh:
                data = fh.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise HTTPException(404, f"cannot read the file: {exc.strerror or exc}") from None
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"file is larger than {MAX_FILE_BYTES:,} bytes")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(415, "not a UTF-8 text file") from None
        media_type = "text/markdown" if path.suffix.lower() in (".md", ".markdown") else "text/plain"
        return Response(data, media_type=media_type, headers={"content-security-policy": "default-src 'none'; sandbox"})

    async def add_item(self, request: Request) -> JSONResponse:
        new = await _parse(request, NewItem)
        item = self.assistant.add_item(new.kind.value, new.title, owner=new.owner or None, due_text=new.due_text or None)
        return JSONResponse(item, status_code=201)

    async def edit_item(self, request: Request) -> JSONResponse:
        item_id, meeting_id = _target(request)
        fields = (await _parse(request, ItemEdit)).model_dump(mode="json", exclude_unset=True)
        return JSONResponse(self.assistant.edit_item(item_id, meeting_id=meeting_id, **fields))

    async def push(self, request: Request) -> JSONResponse:
        item_id, meeting_id = _target(request)
        return JSONResponse({"results": await self.assistant.push_item(item_id, meeting_id=meeting_id)})

    async def dismiss(self, request: Request) -> JSONResponse:
        item_id, meeting_id = _target(request)
        return _outcome(self.assistant.dismiss_item(item_id, dismissed=True, meeting_id=meeting_id), "item not found")

    async def restore(self, request: Request) -> JSONResponse:
        item_id, meeting_id = _target(request)
        return _outcome(self.assistant.dismiss_item(item_id, dismissed=False, meeting_id=meeting_id), "item not found")

    async def run_agent(self, request: Request) -> JSONResponse:
        item_id, meeting_id = _target(request)
        body = await _parse(request, AgentApproval)
        ok = self.assistant.approve_agent(item_id, prompt=body.prompt or None, meeting_id=meeting_id)
        return _outcome(ok, "nothing to run: no agent task, already queued or running, or the meeting has closed")

    async def reject_agent(self, request: Request) -> JSONResponse:
        item_id, meeting_id = _target(request)
        ok = self.assistant.reject_agent(item_id, meeting_id=meeting_id)
        return _outcome(ok, "only a suggested or failed agent task can be rejected")

    async def end_meeting(self, request: Request) -> JSONResponse:
        # finishing (final extraction, summary, push, waiting for agents) can take minutes;
        # the page follows it through meeting.ending / meeting.ended events
        task = asyncio.ensure_future(self.assistant.end_meeting("dashboard"))
        task.add_done_callback(_log_failure)
        self.assistant.track(task)  # so a shutdown waits for it
        return JSONResponse({"ok": True}, status_code=202)

    async def extract(self, request: Request) -> JSONResponse:
        await self.assistant.extract_now()
        return JSONResponse({"ok": True})


def create_app(
    assistant: Assistant, token: str, *, allowed_hosts: Collection[str] | None = LOOPBACK_HOSTS
) -> Starlette:
    """The dashboard ASGI app. ``allowed_hosts=None`` turns the Host check off (see
    :func:`allowed_hosts_for`)."""
    if not token:
        raise ValueError("the dashboard token must not be empty")
    api = _Api(assistant, INDEX_HTML.read_text(encoding="utf-8"))
    app = Starlette(
        routes=api.routes(),
        middleware=[Middleware(_Guard, token=token, allowed_hosts=allowed_hosts)],
        exception_handlers={HTTPException: _http_error, LookupError: _not_found, ValidationError: _invalid},
    )
    app.state.close_streams = api.close_streams
    return app


# --------------------------------------------------------------------------- serving


class _Server(uvicorn.Server):
    """uvicorn without its signal handlers: the CLI owns Ctrl+C."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _run(server: uvicorn.Server, sock: socket.socket, app: Starlette) -> None:
    """Serve until cancelled, then shut down gracefully."""
    serving = asyncio.ensure_future(server.serve(sockets=[sock]))
    try:
        await asyncio.shield(serving)
    finally:
        app.state.close_streams()
        server.should_exit = True
        await serving


async def serve(
    assistant: Assistant, cfg: DashboardConfig, token: str | None = None
) -> tuple[str, asyncio.Task[None]]:
    """Start the dashboard on the running loop; returns ``(URL with token, server task)``.

    Cancel the task to stop it. Raises :class:`OSError` when the port can't be bound.
    """
    token = token or secrets.token_urlsafe(24)
    bind = cfg.host.strip("[]")
    app = create_app(assistant, token, allowed_hosts=allowed_hosts_for(bind))
    # bind here, not in uvicorn: it sys.exit()s on a port that's in use
    sock = socket.create_server((bind, cfg.port), family=socket.AF_INET6 if ":" in bind else socket.AF_INET)
    config = uvicorn.Config(
        app,
        log_config=None,  # leave the app's logging alone
        log_level="warning",
        access_log=False,  # request lines would carry ?token=
        lifespan="off",
        ws="none",
        timeout_graceful_shutdown=1,
    )
    server = _Server(config)
    task = asyncio.ensure_future(_run(server, sock, app))
    while not server.started:
        if task.done():
            sock.close()
            task.result()  # re-raises the startup error
            raise RuntimeError("the dashboard server stopped during startup")
        await asyncio.sleep(0.01)
    host = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(bind, bind)  # wildcard → loopback
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{sock.getsockname()[1]}/?token={token}", task
