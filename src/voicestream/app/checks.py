"""Health checks as data: what ``voicestream doctor`` prints and the app's setup/warnings screens show.

Each :class:`Check` is one line of the doctor's output (``✔ title — detail``). ``fix`` and
``action`` are for the app: a plain-words hint and a machine-readable pointer the UI turns into a
button (``secret:ANTHROPIC_API_KEY``, ``model:large-v3-turbo-q5_0``, ``pane:microphone``,
``settings:sinks.notion``…). Async so the sink checks can run inside the app's own event loop.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..config import Config
    from ..costs import CostMeter

Status = Literal["ok", "warn", "fail"]
MARKS = {"ok": "✔", "warn": "!", "fail": "✘"}

_KEY_FOR = {"openai": "OPENAI_API_KEY", "openai-realtime": "OPENAI_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}


@dataclass
class Check:
    id: str
    area: str
    status: Status
    title: str
    detail: str = ""
    fix: str | None = None
    action: str | None = None

    def line(self) -> str:
        """The doctor's line for it."""
        return f" {MARKS[self.status]} {self.title}" + (f" — {self.detail}" if self.detail else "")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def failed(checks: list[Check]) -> bool:
    return any(c.status == "fail" for c in checks)


def _has_claude_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def privacy_checks(cfg: Config) -> list[Check]:
    out = []
    for p in (cfg.data_path, cfg.data_path / ".env", Path("~/.voicestream/.env").expanduser()):
        try:
            if os.name == "posix" and p.exists() and p.stat().st_mode & 0o077:
                cmd = f"chmod {'700' if p.is_dir() else '600'} '{p}'"
                out.append(Check(f"privacy:{p}", "privacy", "warn", f"{p} is readable by other accounts on this computer",
                                 cmd, fix=f"run {cmd} in Terminal", action="chmod"))
        except OSError:
            pass
    return out


def transcription_checks(cfg: Config) -> list[Check]:
    be = cfg.transcription.backend
    need = _KEY_FOR.get(be)
    if need:
        if os.environ.get(need):
            return [Check("transcription", "transcription", "ok", f"transcription: {be}", f"{need} set")]
        return [Check("transcription", "transcription", "fail", f"transcription: {be}", f"{need} is not set",
                      fix=f"add your {'Deepgram' if be == 'deepgram' else 'OpenAI'} key", action=f"secret:{need}")]
    if be == "whisper-cpp":
        from ..transcribe.whisper_cpp import check as whisper_check

        status, detail = whisper_check(cfg)
        action = None
        if status != "ok":
            from .. import stt_models

            wc = cfg.transcription.whisper_cpp
            missing = next((m for m in (wc.model, stt_models.VAD_MODEL) if m in stt_models.CATALOG
                            and not stt_models.is_installed(cfg, m)), None)
            action = f"model:{missing}" if missing else None
        return [Check("transcription", "transcription", status, "transcription: whisper.cpp on this computer (free)", detail,
                      action=action)]
    if be == "local":
        try:
            import faster_whisper  # noqa: F401

            return [Check("transcription", "transcription", "ok", "transcription: local faster-whisper")]
        except ImportError:
            return [Check("transcription", "transcription", "fail", "transcription: local", "pip install 'voicestream[local]'")]
    return []


def extraction_checks(cfg: Config) -> list[Check]:
    if cfg.brain.provider == "anthropic":
        if _has_claude_key():
            return [Check("extraction", "claude", "ok", f"extraction: Claude {cfg.brain.model}", f"summary {cfg.brain.summary_model}")]
        return [Check("extraction", "claude", "warn", "extraction: ANTHROPIC_API_KEY not set", "falling back to the offline heuristic",
                      fix="add your Claude key", action="secret:ANTHROPIC_API_KEY")]
    return [Check("extraction", "claude", "warn", "extraction: offline heuristic", "set brain.provider = 'anthropic' for real extraction",
                  action="settings:brain.provider")]


def audio_checks(cfg: Config) -> list[Check]:
    out = []
    try:
        from ..audio.mic import capture_report

        for status, what, detail in capture_report(cfg):
            action = ("pane:microphone" if what == "microphone" else "pane:screen_audio") if status != "ok" else None
            out.append(Check(f"audio:{what}", "audio", status, what, detail, action=action))  # type: ignore[arg-type]
    except ImportError as exc:
        out.append(Check("audio", "audio", "fail", "audio capture", f"{exc} — pip install 'voicestream[audio]'"))
    ffmpeg = shutil.which("ffmpeg")
    out.append(Check("ffmpeg", "audio", "ok" if ffmpeg else "warn", "ffmpeg",
                     ffmpeg or "not found (needed for non-WAV replay and ffmpeg capture)"))
    return out


def agent_checks(cfg: Config) -> list[Check]:
    mode, has_key = cfg.agents.mode, _has_claude_key()
    if mode == "off":
        return [Check("agents", "agents", "ok", "agents: off")]
    if cfg.agents.engine == "agent-sdk":
        try:
            import claude_agent_sdk
        except ImportError:
            return [Check("agents", "agents", "warn", "agents: claude-agent-sdk not installed",
                          "pip install 'voicestream[agents]' or set agents.engine = 'api'")]
        from ..agents.runner import find_claude_cli

        title = f"agents: {mode} via Agent SDK {claude_agent_sdk.__version__}"
        if has_key and find_claude_cli() is None:
            # what AgentRunner.engine() then does (the Mac app ships without Claude Code): say so, not "Agent SDK"
            return [Check("agents", "agents", "ok", f"agents: {mode} via single API calls",
                          "Claude Code isn't installed: agents draft their result with one Claude call, without web search")]
        if has_key:
            return [Check("agents", "agents", "ok", title, cfg.agents.model)]
        return [Check("agents", "agents", "warn", title,
                      "ANTHROPIC_API_KEY not set: agents only run if Claude Code is logged in on this computer",
                      action="secret:ANTHROPIC_API_KEY")]
    title = f"agents: {mode} via single API calls"
    if has_key:
        return [Check("agents", "agents", "ok", title, cfg.agents.model)]
    return [Check("agents", "agents", "warn", title, "ANTHROPIC_API_KEY not set: agents can't run", action="secret:ANTHROPIC_API_KEY")]


def dashboard_checks(cfg: Config) -> list[Check]:
    if not cfg.dashboard.enabled:
        return []
    try:
        import starlette  # noqa: F401
        import uvicorn  # noqa: F401

        return [Check("dashboard", "dashboard", "ok", f"dashboard: http://{cfg.dashboard.host}:{cfg.dashboard.port}")]
    except ImportError:
        return [Check("dashboard", "dashboard", "warn", "dashboard disabled", "pip install 'voicestream[dashboard]'")]


async def sink_checks(cfg: Config) -> list[Check]:
    """Every enabled sink gets a line, also those whose settings don't validate (they'd be skipped)."""
    from pydantic import ValidationError

    from ..sinks.base import SinkError, build_sink, describe_config_error

    out = []
    for name, raw in cfg.sinks.items():
        settings = f"settings:sinks.{name}"
        try:
            sink = build_sink(name, raw, cfg)
        except ValidationError as exc:
            out.append(Check(f"sink:{name}", "tools", "fail", f"sink {name}", f"{describe_config_error(name, exc)} — it would be skipped",
                             action=settings))
            continue
        except (KeyError, ImportError, SinkError) as exc:
            out.append(Check(f"sink:{name}", "tools", "fail", f"sink {name}", str(exc), action=settings))
            continue
        if sink is None:
            continue
        try:
            out.append(Check(f"sink:{sink.name}", "tools", "ok", f"sink {sink.name}", await sink.check()))
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            out.append(Check(f"sink:{sink.name}", "tools", "fail", f"sink {sink.name}", str(exc), action=settings))
        finally:
            await sink.aclose()
    return out


def budget_checks(cfg: Config, costs: CostMeter | None = None) -> list[Check]:
    from ..costs import CostMeter

    # the app passes its own meter: a second one on the same day would lose the other's updates
    today = (costs or CostMeter(cfg, persist=True)).today
    return [Check("budget", "budget", "ok", f"spend today ${today.total_usd:.3f}",
                  f"budget ${cfg.budget.daily_usd:.2f}/day (+ agents ${cfg.budget.agents_daily_usd:.2f})")]


async def run_checks(cfg: Config, costs: CostMeter | None = None) -> list[Check]:
    """All checks, in the doctor's order."""
    return [
        *privacy_checks(cfg),
        *transcription_checks(cfg),
        *extraction_checks(cfg),
        *audio_checks(cfg),
        *agent_checks(cfg),
        *dashboard_checks(cfg),
        *await sink_checks(cfg),
        *budget_checks(cfg, costs),
    ]


def header(cfg: Config) -> list[str]:
    from .. import __version__

    return [f"voicestream {__version__} · python {sys.version.split()[0]} · {sys.platform}", f"data dir: {cfg.data_path}"]
