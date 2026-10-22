"""The Mac app's routes, on top of the live dashboard's (see :func:`.server.create_app`).

Everything here needs the token like the dashboard's own API, except the app page and its two
static files. Names that come from the URL — a meeting folder, a model, a key name — are matched
against the real list (never joined to a path), and the routes that open things on the desktop
take a name from a fixed list (:mod:`voicestream.app.desktop`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Coroutine
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .. import stt_models
from ..app import desktop, logs
from ..app import secrets as keys
from ..app.checks import run_checks
from ..app.host import START_WAIT_S, HostError
from ..app.meetings import MeetingBusy, MeetingNotFound, resolve_folder
from ..app.prefs import FloatingPosition, Hotkey, MeetingStart, Minutes, layout_context
from ..app.settings import SettingsError, save_checked, settings_view
from ..costs import estimate_daily
from .server import AgentApproval, ItemEdit, NewItem, _Body, _error, _parse  # noqa: F401 - shared bodies

if TYPE_CHECKING:
    from ..app.host import AppHost
    from .server import _Api

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
APP_HTML = STATIC / "app.html"
FONTS = (  # Geist, bundled (static/fonts/OFL.txt): the app is offline, so nothing loads from a font service
    "geist-latin-wght-normal.woff2", "geist-latin-ext-wght-normal.woff2",
    "geist-latin-wght-italic.woff2", "geist-latin-ext-wght-italic.woff2",
    "geist-mono-latin-wght-normal.woff2", "geist-mono-latin-ext-wght-normal.woff2",
)
ASSETS = {
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    **{f"/static/fonts/{name}": (f"fonts/{name}", "font/woff2") for name in FONTS},
}
HISTORY_DAYS = 30
MEETING_ACTIONS = ("start", "stop", "toggle", "keep")
"""``POST /api/meeting/<action>``; ``/api/meeting/end`` is the dashboard's own (:mod:`.server`)."""
ANSWERS = ("suggestion/accept", "suggestion/dismiss", "notice/dismiss")
"""``POST /api/<answer>``: the answers to what the window and the floating button ask or tell."""
START_ANSWER_S = START_WAIT_S + 5.0
"""A start is answered once listening is up (within about :data:`.host.START_WAIT_S`), except one that waits
for the last meeting's wrap-up, which can take minutes: after this long, with what it will do."""


# --------------------------------------------------------------------------- bodies


class SettingsPatch(_Body):
    changes: dict[str, Any]


class SecretValue(_Body):
    value: str = ""


class PushRequest(_Body):
    sinks: list[str] | None = None
    notes: bool = True
    force: bool = False
    """Also push a meeting another voicestream still has open (e.g. left over from a crash)."""


class RevealRequest(_Body):
    what: Literal["data_dir", "config", "log", "meeting"]
    folder: str | None = None


class OpenSettingsRequest(_Body):
    pane: Literal["microphone", "screen_audio", "automation", "login_items"]


class PrefsPatch(_Body):
    listen_on_launch: bool | None = None
    start_at_login: bool | None = None
    ui_language: Literal["auto", "en", "fr"] | None = None
    window: dict[str, Any] | None = Field(None)
    hotkey: Hotkey | None = None
    """The keyboard shortcut, checked as the page checks it, by the Mac's keyboard layout (``null``: turned off)."""
    hotkey_sound: bool | None = None
    meeting_start: MeetingStart | None = None
    floating_button: bool | None = None
    floating_position: FloatingPosition | None = None
    """``null``: the floating button goes back to its default place."""
    spotlight_launcher: bool | None = None
    call_suggestions: bool | None = None
    silence_ask_minutes: Minutes | None = None
    silence_end_minutes: Minutes | None = None


class HotkeyRecording(_Body):
    active: bool


# --------------------------------------------------------------------------- errors


async def _settings_error(request: Request, exc: SettingsError) -> JSONResponse:
    return JSONResponse({"error": str(exc), "field": exc.field}, status_code=400)


async def _conflict(request: Request, exc: Exception) -> JSONResponse:
    return _error(409, str(exc))


async def _missing(request: Request, exc: MeetingNotFound) -> JSONResponse:
    return _error(404, str(exc.args[0]) if exc.args else "not found")


def _flag(request: Request, name: str) -> bool:
    return request.query_params.get(name, "").lower() in ("1", "true", "yes")


def _int(request: Request, name: str, default: int, lo: int, hi: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(400, f"{name} must be a whole number") from None
    if not lo <= value <= hi:
        raise HTTPException(400, f"{name} must be between {lo} and {hi}")
    return value


class AppApi:
    PUBLIC = frozenset({"/app", *ASSETS})
    """The page and its static files: no data in them, and a page can't send headers for <script src>."""

    def __init__(self, host: AppHost, api: _Api) -> None:
        self.host = host
        self.api = api

    def exception_handlers(self) -> dict[Any, Any]:
        return {
            SettingsError: _settings_error,
            MeetingNotFound: _missing,
            MeetingBusy: _conflict,
            HostError: _conflict,
            desktop.DesktopError: _conflict,
        }

    def routes(self) -> list[Route]:
        m, item = "/api/meetings/{folder}", "/api/meetings/{folder}/items/{item_id}"
        return [
            Route("/app", self.page, methods=["GET"]),
            *(Route(path, self.asset, methods=["GET"]) for path in ASSETS),
            Route("/api/app", self.app_state, methods=["GET"]),
            Route("/api/listen/{action}", self.listen, methods=["POST"]),
            *(Route(f"/api/meeting/{name}", self.meeting_action, methods=["POST"]) for name in MEETING_ACTIONS),
            *(Route(f"/api/{name}", self.answer, methods=["POST"]) for name in ANSWERS),
            Route("/api/settings", self.get_settings, methods=["GET"]),
            Route("/api/settings", self.patch_settings, methods=["PATCH"]),
            Route("/api/secrets/{name}", self.put_secret, methods=["PUT"]),
            Route("/api/secrets/{name}/test", self.check_secret, methods=["POST"]),
            Route("/api/doctor", self.doctor, methods=["GET"]),
            Route("/api/devices", self.devices, methods=["GET"]),
            Route("/api/meetings", self.meetings, methods=["GET"]),
            Route(m, self.meeting, methods=["GET"]),
            Route(f"{m}/push", self.push_meeting, methods=["POST"]),
            Route(f"{m}/summarize", self.summarize_meeting, methods=["POST"]),
            Route(f"{m}/items", self.add_item, methods=["POST"]),
            Route(item, self.edit_item, methods=["PATCH"]),
            Route(f"{item}/dismiss", self.dismiss_item, methods=["POST"]),
            Route(f"{item}/restore", self.restore_item, methods=["POST"]),
            Route("/api/cost", self.cost, methods=["GET"]),
            Route("/api/models", self.models, methods=["GET"]),
            Route("/api/models/{name}/download", self.model_download, methods=["POST"]),
            Route("/api/models/{name}/cancel", self.model_cancel, methods=["POST"]),
            Route("/api/models/{name}", self.model_remove, methods=["DELETE"]),
            Route("/api/setup/complete", self.setup_complete, methods=["POST"]),
            Route("/api/prefs", self.patch_prefs, methods=["PATCH"]),
            Route("/api/hotkey/recording", self.hotkey_recording, methods=["POST"]),
            Route("/api/reveal", self.reveal, methods=["POST"]),
            Route("/api/open-settings", self.open_settings, methods=["POST"]),
            Route("/api/permissions/{kind}", self.permission, methods=["POST"]),
            Route("/api/google-bridge", self.google_bridge, methods=["GET"]),
        ]

    # ------------------------------------------------------------------ the page

    async def page(self, request: Request) -> Response:
        try:
            html = APP_HTML.read_text(encoding="utf-8")  # read each time: the UI can be edited live
        except OSError:
            raise HTTPException(404, "the app page is missing from this install") from None
        nonce = secrets.token_urlsafe(16)
        html = html.replace("<style>", f'<style nonce="{nonce}">').replace("<script>", f'<script nonce="{nonce}">')
        csp = (
            f"default-src 'none'; script-src 'nonce-{nonce}' 'self'; style-src 'nonce-{nonce}' 'self'; "
            "font-src 'self'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return HTMLResponse(html, headers={"content-security-policy": csp})

    async def asset(self, request: Request) -> Response:
        name, media_type = ASSETS[request.url.path]
        try:
            data = (STATIC / name).read_bytes()
        except OSError:
            raise HTTPException(404, f"{name} is not part of this install") from None
        return Response(data, media_type=media_type)

    # --------------------------------------------------------------- listening

    async def app_state(self, request: Request) -> JSONResponse:
        return JSONResponse(self.host.state())

    async def listen(self, request: Request) -> JSONResponse:
        action, force, host = request.path_params["action"], _flag(request, "force"), self.host
        if action == "start":
            await host.listen_start()
        elif action == "pause":
            await host.listen_pause()
        elif action == "resume":
            await host.listen_resume()
        elif action in ("stop", "restart"):
            # a graceful stop wraps the meeting up (summary, pushes): it can take a while, so answer
            # with "stopping" and let app.state events tell the rest
            coro = host.listen_stop(force=force) if action == "stop" else host.listen_restart(force=force)
            task = asyncio.ensure_future(coro)
            host.track(task)
            await asyncio.wait({task}, timeout=0.5)
        else:
            raise HTTPException(404, f"unknown action {action!r}")
        return JSONResponse(host.state())

    async def meeting_action(self, request: Request) -> JSONResponse:
        """The window's Start and Stop, and Keep for "Still in a meeting?": the host's own meeting actions,
        the ones the menu bar, the shortcut, the floating button and Spotlight use too. A start answers once
        listening is up (or failed), a stop at once (the wrap-up goes on, ``app.state`` events tell the
        rest). The reply is the app's state with ``action``: what was done (``null``: nothing to do)."""
        name, host = request.url.path.rsplit("/", 1)[-1], self.host
        if name == "keep":
            action: str | None = "keep" if await host.meeting_keep() else None
        else:
            run = {"start": host.meeting_start, "stop": host.meeting_stop, "toggle": host.meeting_toggle}[name]
            action = await self._in_time(run())
        return JSONResponse({**host.state(), "action": action})

    async def _in_time(self, action: Coroutine[Any, Any, str | None]) -> str | None:
        """What a meeting action did, once it is done — or, for a start waiting for the last meeting's wrap-up,
        ``"start"`` after :data:`START_ANSWER_S`: the request mustn't hang for minutes (the window's request
        could time out meanwhile), the start goes on and ``app.state`` events tell the rest."""
        task = asyncio.ensure_future(action)
        self.host.track(task)
        done, _ = await asyncio.wait({task}, timeout=START_ANSWER_S)
        return task.result() if done else "start"

    async def answer(self, request: Request) -> JSONResponse:
        """Start or Not now on "Call detected — start notes?", and OK on a notice. The reply is the app's
        state with ``action``: what was done (``null``: there was nothing to answer)."""
        host = self.host
        name = request.url.path.removeprefix("/api/")
        if name == "suggestion/accept":
            action = await self._in_time(host.suggestion_accept())
        elif name == "suggestion/dismiss":
            action = "dismiss" if host.suggestion_dismiss() else None
        else:
            action = "dismiss" if host.dismiss_notice() else None
        return JSONResponse({**host.state(), "action": action})

    # ---------------------------------------------------------------- settings

    async def get_settings(self, request: Request) -> JSONResponse:
        # the file as it is now: edited by hand (the app asks for that for some settings) since it was read?
        await self.host.refresh_config()
        view = settings_view(self.host.cfg)
        view["config_path"] = str(self.host.config_path)
        view["config_exists"] = self.host.config_path.is_file()
        return JSONResponse(view)

    async def patch_settings(self, request: Request) -> JSONResponse:
        body = await _parse(request, SettingsPatch)
        # refuses the settings that only matter for running programs (see app.settings.LOCKED)
        cfg, review = await asyncio.to_thread(save_checked, self.host.config_path, body.changes)
        if review:
            # what Voicestream runs, or where it sends your meetings and keys, changed: say so where you
            # will see it (the app's warning banner mirrors voicestream warnings) and in the log
            log.warning("settings changed what Voicestream runs or where it sends data — check this was you: %s",
                        "; ".join(f"{r['field']} = {r['value']}" for r in review))
        result = await self.host.apply_settings(wait=False)
        self.host.bus.publish("settings.changed", changes=sorted(body.changes), review=review, **result)
        return JSONResponse({
            "values": cfg.model_dump(mode="json"),
            "restart_required": result["restart_pending"],
            "restarting": result["restarting"],
            "review": review,
        })  # fmt: skip

    def _secret_name(self, request: Request) -> str:
        name = request.path_params["name"]
        try:
            return keys.check_name(name)
        except keys.SecretError:
            raise HTTPException(404, f"unknown key name {name!r}") from None

    async def put_secret(self, request: Request) -> JSONResponse:
        name = self._secret_name(request)
        body = await _parse(request, SecretValue)
        try:
            result = await asyncio.to_thread(keys.set_secret, name, body.value)  # a Keychain prompt may block
        except keys.SecretError as exc:
            raise HTTPException(400, str(exc)) from None
        applied = await self.host.apply_settings(wait=False)  # the LLM and transcribers read keys when built
        self.host.bus.publish("settings.changed", secrets=[name], **applied)
        return JSONResponse(result)

    async def check_secret(self, request: Request) -> JSONResponse:
        name = self._secret_name(request)
        return JSONResponse(await keys.verify(name, self.host.cfg))

    async def doctor(self, request: Request) -> JSONResponse:
        checks = await run_checks(self.host.cfg, self.host.costs)
        return JSONResponse({"checks": [c.as_dict() for c in checks]})

    async def devices(self, request: Request) -> JSONResponse:
        from ..app.devices import list_devices

        return JSONResponse(await asyncio.to_thread(list_devices, self.host.cfg))

    async def permission(self, request: Request) -> JSONResponse:
        from ..app.devices import probe

        kind = request.path_params["kind"]
        if kind not in ("microphone", "system-audio"):
            raise HTTPException(404, f"unknown permission {kind!r}")
        return JSONResponse(await probe(self.host.cfg, kind, hooks=self.host.native_hooks))

    # ---------------------------------------------------------------- meetings

    async def meetings(self, request: Request) -> JSONResponse:
        limit = _int(request, "limit", 50, 1, 500)
        offset = _int(request, "offset", 0, 0, 1_000_000)
        q = request.query_params.get("q", "")[:200]
        # on the loop, not in a thread: it reads the in-memory meetings the loop is changing
        return JSONResponse(self.host.meetings.list(limit, offset, q))

    async def meeting(self, request: Request) -> JSONResponse:
        return JSONResponse(self.host.meetings.detail(request.path_params["folder"]))

    async def push_meeting(self, request: Request) -> JSONResponse:
        body = await _parse(request, PushRequest)
        result = await self.host.meetings.push(request.path_params["folder"], body.sinks, notes=body.notes, force=body.force)
        result.pop("up_to_date", None)
        return JSONResponse(result)

    async def summarize_meeting(self, request: Request) -> JSONResponse:
        self.host.meetings.start_summarize(request.path_params["folder"])
        return JSONResponse({"ok": True}, status_code=202)

    async def add_item(self, request: Request) -> JSONResponse:
        new = await _parse(request, NewItem)
        item = self.host.meetings.add_item(
            request.path_params["folder"], new.kind.value, new.title, owner=new.owner or None, due_text=new.due_text or None
        )
        return JSONResponse(item, status_code=201)

    async def edit_item(self, request: Request) -> JSONResponse:
        fields = (await _parse(request, ItemEdit)).model_dump(mode="json", exclude_unset=True)
        p = request.path_params
        return JSONResponse(self.host.meetings.edit_item(p["folder"], p["item_id"], **fields))

    async def dismiss_item(self, request: Request) -> JSONResponse:
        p = request.path_params
        return JSONResponse(self.host.meetings.dismiss_item(p["folder"], p["item_id"], True))

    async def restore_item(self, request: Request) -> JSONResponse:
        p = request.path_params
        return JSONResponse(self.host.meetings.dismiss_item(p["folder"], p["item_id"], False))

    # -------------------------------------------------------------------- costs

    async def cost(self, request: Request) -> JSONResponse:
        raw = request.query_params.get("hours", "4")
        try:
            hours = float(raw)
        except ValueError:
            raise HTTPException(400, "hours must be a number") from None
        if not 0 < hours <= 24:
            raise HTTPException(400, "hours must be between 0 and 24")
        cfg, costs = self.host.cfg, self.host.costs
        today = costs.today
        estimate = {k: round(v, 4) if isinstance(v, float) else v for k, v in estimate_daily(cfg, hours).items()}
        return JSONResponse({
            "today": costs.snapshot()["today"],
            "history": _history(cfg.data_path / "spend", today),
            "estimate": estimate,
            "budget": {"daily_usd": cfg.budget.daily_usd, "agents_daily_usd": cfg.budget.agents_daily_usd},
        })  # fmt: skip

    # ------------------------------------------------------------------- models

    def _model(self, request: Request) -> str:
        name = request.path_params["name"]
        if name not in stt_models.CATALOG:
            raise HTTPException(404, f"unknown model {name!r}")
        return name

    async def models(self, request: Request) -> JSONResponse:
        return JSONResponse(self.host.models())

    async def model_download(self, request: Request) -> JSONResponse:
        name = self._model(request)
        started = self.host.model_download(name)
        return JSONResponse({"ok": True, "started": started}, status_code=202)

    async def model_cancel(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "cancelled": self.host.model_cancel(self._model(request))})

    async def model_remove(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "removed": await self.host.model_remove(self._model(request))})

    # -------------------------------------------------------------------- prefs

    async def setup_complete(self, request: Request) -> JSONResponse:
        return JSONResponse(self.host.complete_setup().model_dump(mode="json"))

    async def patch_prefs(self, request: Request) -> JSONResponse:
        # a shortcut recorded with ⌥ held may come without its label: the Mac's keyboard layout has it
        body = await _parse(request, PrefsPatch, layout_context(self.host.keyboard_layout))
        return JSONResponse(self.host.update_prefs(body.model_dump(exclude_unset=True)).model_dump(mode="json"))

    async def hotkey_recording(self, request: Request) -> JSONResponse:
        """Settings is recording a new keyboard shortcut (or is done): the current one is off meanwhile."""
        body = await _parse(request, HotkeyRecording)
        self.host.record_hotkey(body.active)
        return JSONResponse(self.host.state())

    # ------------------------------------------------------------------ desktop

    async def reveal(self, request: Request) -> JSONResponse:
        body = await _parse(request, RevealRequest)
        cfg = self.host.cfg
        if body.what == "data_dir":
            path = cfg.data_path
        elif body.what == "config":
            path = self.host.config_path if self.host.config_path.exists() else self.host.config_path.parent
        elif body.what == "log":
            path = logs.log_path(cfg.data_path)
            path = path if path.exists() else path.parent
        else:
            if not body.folder:
                raise HTTPException(400, "folder is required")
            path = resolve_folder(cfg, body.folder)
        await asyncio.to_thread(desktop.reveal, path)
        return JSONResponse({"ok": True})

    async def open_settings(self, request: Request) -> JSONResponse:
        body = await _parse(request, OpenSettingsRequest)
        await asyncio.to_thread(desktop.open_settings, body.pane)
        return JSONResponse({"ok": True})

    async def google_bridge(self, request: Request) -> JSONResponse:
        """The Apps Script for Settings → Your tools → Google ("Copy the bridge script"): what
        ``voicestream google-bridge`` prints, for people who never open a terminal."""
        from ..sinks.google import BRIDGE_SOURCE

        return JSONResponse({"script": await asyncio.to_thread(BRIDGE_SOURCE.read_text, encoding="utf-8")})


def _history(spend_dir: Path, today: Any) -> list[dict[str, Any]]:
    """The last :data:`HISTORY_DAYS` days, oldest first, zeros for days without spend (bars need them)."""
    end = date.fromisoformat(today.day)
    out = []
    for i in range(HISTORY_DAYS - 1, -1, -1):
        day = (end - timedelta(days=i)).isoformat()
        row = {"day": day, "stt_usd": 0.0, "llm_usd": 0.0, "agents_usd": 0.0}
        if day == today.day:
            row.update(stt_usd=today.stt_usd, llm_usd=today.llm_usd, agents_usd=today.agents_usd)  # fresher than the file
        else:
            try:
                data = json.loads((spend_dir / f"{day}.json").read_text(encoding="utf-8"))
                row.update({k: float(data.get(k) or 0.0) for k in ("stt_usd", "llm_usd", "agents_usd")})
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        out.append({k: round(v, 4) if isinstance(v, float) else v for k, v in row.items()})
    return out
