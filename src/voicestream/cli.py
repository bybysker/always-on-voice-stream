"""Command line: ``voicestream run | replay | import | devices | doctor | init | sessions | summarize | push | cost``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import Config, ConfigError, load_config

if TYPE_CHECKING:
    from .models import TranscriptSegment

log = logging.getLogger("voicestream")


# ------------------------------------------------------------------- helpers


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn", "uvicorn.error", "uvicorn.access", "anthropic"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    if getattr(args, "backend", None):
        cfg.transcription.backend = args.backend
    if getattr(args, "language", None):
        cfg.transcription.language = args.language
    if getattr(args, "agents", None):
        cfg.agents.mode = args.agents
    if getattr(args, "dispatch", None):
        cfg.dispatch.mode = args.dispatch
    if getattr(args, "offline", False):
        cfg.brain.provider = "heuristic"
    if getattr(args, "screen", False):
        cfg.screen.enabled = True
    if getattr(args, "no_dashboard", False):
        cfg.dashboard.enabled = False
    if getattr(args, "port", None):
        cfg.dashboard.port = args.port
    if getattr(args, "user", None):
        cfg.user_name = args.user
    if getattr(args, "data_dir", None):
        cfg.data_dir = args.data_dir
    if getattr(args, "store_audio", False):
        cfg.capture.store_audio = True
    return cfg


async def _run_assistant(  # noqa: ANN001
    assistant, cfg: Config, *, transcript=None, speed: float = 0.0, started_at=None, keep_open: bool = False
) -> int:
    from .console import ConsolePrinter, command_loop, install_signal_handlers

    printer = ConsolePrinter()
    assistant.bus.listen(printer)
    install_signal_handlers(assistant)
    server_task = None
    if cfg.dashboard.enabled:
        try:
            from .dashboard.server import serve

            url, server_task = await serve(assistant, cfg.dashboard)
            printer.emit(printer.s.bold(f"● dashboard: {url}"))
            if cfg.dashboard.open_browser:
                webbrowser.open(url)
        except ImportError:
            printer.emit(printer.s.dim("  (dashboard needs: pip install 'voicestream[dashboard]')"))
        except OSError as exc:
            printer.emit(printer.s(f"  dashboard failed to start: {exc}", "31"))
    # import/replay --dashboard: the point is to review the meeting there, so don't close it at the end
    assistant.keep_open = keep_open and server_task is not None
    commands = asyncio.ensure_future(command_loop(assistant, printer))
    try:
        if transcript is not None:
            await assistant.replay_transcript(transcript, speed=speed, started_at=started_at)
        else:
            await assistant.run()
    finally:
        commands.cancel()
        if server_task is not None:
            server_task.cancel()
            try:
                await server_task
            except BaseException:  # noqa: BLE001
                pass
    return 0


# ------------------------------------------------------------------ commands


def cmd_run(args: argparse.Namespace) -> int:
    from .audio import CaptureSetupError
    from .session import Assistant
    from .transcribe import TranscriptionError

    cfg = _apply_overrides(load_config(args.config), args)
    sources = args.source or None
    always_on = False if args.once else None
    try:
        assistant = Assistant(cfg, source_specs=sources, title=args.title, always_on=always_on)
        return asyncio.run(_run_assistant(assistant, cfg))
    except (CaptureSetupError, TranscriptionError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_replay(args: argparse.Namespace) -> int:
    from .session import Assistant

    cfg = _apply_overrides(load_config(args.config), args)
    files = args.files
    names = ["mic", "system"]
    if args.as_source:
        names = [args.as_source] + [n for n in names if n != args.as_source]
    if len(files) > 2:
        print("error: replay takes one file (mic) or two (mic + system)", file=sys.stderr)
        return 2
    specs = [f"file:{Path(f).expanduser()}@{names[i]}" for i, f in enumerate(files)]
    if not args.dashboard:
        cfg.dashboard.enabled = False
    from .transcribe import TranscriptionError

    assistant = Assistant(cfg, source_specs=specs, title=args.title, always_on=False, replay_speed=args.speed)
    try:
        return asyncio.run(_run_assistant(assistant, cfg, keep_open=args.dashboard))
    except TranscriptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_import(args: argparse.Namespace) -> int:
    from .session import Assistant
    from .transcript_import import parse_transcript

    cfg = _apply_overrides(load_config(args.config), args)
    segments = parse_transcript(args.file, cfg.user_name, me=args.me)
    if not segments:
        print(f"error: no transcript lines found in {args.file}", file=sys.stderr)
        return 2
    if not args.dashboard:
        cfg.dashboard.enabled = False
    try:
        started_at = _meeting_start(args.date, Path(args.file), segments)
    except ValueError as exc:
        print(f"error: --date {args.date!r}: {exc} (use e.g. 2026-10-12 or 2026-10-12T14:30)", file=sys.stderr)
        return 2
    print(f"importing {len(segments)} lines from {args.file} (meeting of {started_at.astimezone():%Y-%m-%d %H:%M})")
    assistant = Assistant(cfg, sources=[], title=args.title, always_on=False)
    return asyncio.run(
        _run_assistant(assistant, cfg, transcript=segments, speed=args.speed, started_at=started_at, keep_open=args.dashboard)
    )


def _meeting_start(when: str | None, path: Path, segments: list[TranscriptSegment]) -> datetime:
    """When an imported meeting started: ``--date`` (local time), else the file's time minus its length
    (meeting tools write the transcript when the call ends)."""
    if when:
        start = datetime.fromisoformat(when)
        return (start if start.tzinfo else start.astimezone()).astimezone(UTC)
    length = max((s.t_end for s in segments), default=0.0)
    return datetime.fromtimestamp(path.stat().st_mtime, UTC) - timedelta(seconds=length)


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        from .audio.mic import describe_devices

        text = describe_devices()  # imports sounddevice itself
    except ImportError as exc:
        print(f"audio capture libraries missing: {exc}\n  pip install 'voicestream[audio]'")
        return 1
    print(text)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from .storage import private_dir

    target = Path(args.path).expanduser() if args.path else Path("~/.voicestream/voicestream.toml").expanduser()
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    if target.parent == Path("~/.voicestream").expanduser() or not target.parent.exists():
        private_dir(target.parent)  # it also holds the .env, and by default every transcript
    template = resources.files("voicestream").joinpath("config.example.toml").read_text(encoding="utf-8")
    target.write_text(template, encoding="utf-8")
    env = target.parent / ".env"
    if not env.exists():
        env.write_text(resources.files("voicestream").joinpath("env.example").read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(env, 0o600)
    print(f"wrote {target}\nwrote {env} (put your API keys there)\nnext: voicestream doctor")
    return 0


def cmd_google_bridge(args: argparse.Namespace) -> int:
    """Print the Apps Script bridge (``voicestream google-bridge | pbcopy``), or a fresh secret for it."""
    if args.secret:
        import secrets

        print(secrets.token_urlsafe(32))
        return 0
    from .sinks.google import BRIDGE_SOURCE

    sys.stdout.write(BRIDGE_SOURCE.read_text(encoding="utf-8"))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ok = True

    def line(status: str, what: str, detail: str = "") -> None:
        mark = {"ok": "✔", "warn": "!", "fail": "✘"}[status]
        print(f" {mark} {what}" + (f" — {detail}" if detail else ""))

    print(f"voicestream {__version__} · python {sys.version.split()[0]} · {sys.platform}")
    print(f"data dir: {cfg.data_path}")
    for p in (cfg.data_path, cfg.data_path / ".env", Path("~/.voicestream/.env").expanduser()):
        try:
            if os.name == "posix" and p.exists() and p.stat().st_mode & 0o077:
                line("warn", f"{p} is readable by other accounts on this computer", f"chmod {'700' if p.is_dir() else '600'} '{p}'")
        except OSError:
            pass
    # transcription
    be = cfg.transcription.backend
    need = {"openai": "OPENAI_API_KEY", "openai-realtime": "OPENAI_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}.get(be)
    if need:
        if os.environ.get(need):
            line("ok", f"transcription: {be}", f"{need} set")
        else:
            ok = False
            line("fail", f"transcription: {be}", f"{need} is not set")
    elif be == "whisper-cpp":
        from .transcribe.whisper_cpp import check as whisper_check

        status, detail = whisper_check(cfg)
        ok = ok and status != "fail"
        line(status, "transcription: whisper.cpp on this computer (free)", detail)
    elif be == "local":
        try:
            import faster_whisper  # noqa: F401

            line("ok", "transcription: local faster-whisper")
        except ImportError:
            ok = False
            line("fail", "transcription: local", "pip install 'voicestream[local]'")
    # extraction
    if cfg.brain.provider == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            line("ok", f"extraction: Claude {cfg.brain.model}", f"summary {cfg.brain.summary_model}")
        else:
            line("warn", "extraction: ANTHROPIC_API_KEY not set", "falling back to the offline heuristic")
    else:
        line("warn", "extraction: offline heuristic", "set brain.provider = 'anthropic' for real extraction")
    # audio
    try:
        from .audio.mic import capture_report

        for status, what, detail in capture_report(cfg):
            ok = ok and status != "fail"
            line(status, what, detail)
    except ImportError as exc:
        ok = False
        line("fail", "audio capture", f"{exc} — pip install 'voicestream[audio]'")
    line("ok" if shutil.which("ffmpeg") else "warn", "ffmpeg", shutil.which("ffmpeg") or "not found (needed for non-WAV replay and ffmpeg capture)")
    # agents
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if cfg.agents.mode != "off":
        if cfg.agents.engine == "agent-sdk":
            try:
                import claude_agent_sdk

                if has_key:
                    line("ok", f"agents: {cfg.agents.mode} via Agent SDK {claude_agent_sdk.__version__}", cfg.agents.model)
                else:
                    line("warn", f"agents: {cfg.agents.mode} via Agent SDK {claude_agent_sdk.__version__}",
                         "ANTHROPIC_API_KEY not set: agents only run if Claude Code is logged in on this computer")
            except ImportError:
                line("warn", "agents: claude-agent-sdk not installed", "pip install 'voicestream[agents]' or set agents.engine = 'api'")
        elif has_key:
            line("ok", f"agents: {cfg.agents.mode} via single API calls", cfg.agents.model)
        else:
            line("warn", f"agents: {cfg.agents.mode} via single API calls", "ANTHROPIC_API_KEY not set: agents can't run")
    else:
        line("ok", "agents: off")
    # dashboard
    if cfg.dashboard.enabled:
        try:
            import starlette  # noqa: F401
            import uvicorn  # noqa: F401

            line("ok", f"dashboard: http://{cfg.dashboard.host}:{cfg.dashboard.port}")
        except ImportError:
            line("warn", "dashboard disabled", "pip install 'voicestream[dashboard]'")
    # sinks: every enabled one gets a line, also those whose settings don't validate (they'd be skipped)
    from pydantic import ValidationError

    from .sinks.base import SinkError, build_sink, describe_config_error

    async def check_sinks() -> None:
        nonlocal ok
        for name, raw in cfg.sinks.items():
            try:
                sink = build_sink(name, raw, cfg)
            except ValidationError as exc:
                ok = False
                line("fail", f"sink {name}", f"{describe_config_error(name, exc)} — it would be skipped")
                continue
            except (KeyError, ImportError, SinkError) as exc:
                ok = False
                line("fail", f"sink {name}", str(exc))
                continue
            if sink is None:
                continue
            try:
                line("ok", f"sink {sink.name}", await sink.check())
            except Exception as exc:  # noqa: BLE001
                ok = False
                line("fail", f"sink {sink.name}", str(exc))
            finally:
                await sink.aclose()

    asyncio.run(check_sinks())
    from .costs import CostMeter

    today = CostMeter(cfg, persist=True).today
    line("ok", f"spend today ${today.total_usd:.3f}", f"budget ${cfg.budget.daily_usd:.2f}/day (+ agents ${cfg.budget.agents_daily_usd:.2f})")
    return 0 if ok else 1


def cmd_sessions(args: argparse.Namespace) -> int:
    from .storage import list_sessions, load_meeting

    cfg = load_config(args.config)
    for d in list_sessions(cfg.data_path)[: args.limit]:
        try:
            m, _ = load_meeting(d)
        except Exception:  # noqa: BLE001
            continue
        actions = len([i for i in m.items.values() if i.kind.value == "action" and not i.dismissed])
        # the folder name is what `push`/`summarize` take; the date is when the meeting took place
        # (an imported meeting is filed when you import it)
        when = m.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"{d.name:<24} {when:<17} {m.title or '(untitled)':<40} {len(m.segments):>4} lines {actions:>3} actions")
    return 0


def _in_progress(folder: Path) -> bool:
    from .storage import load_meeting

    try:
        return load_meeting(folder)[0].ended_at is None
    except Exception:  # noqa: BLE001
        return False


def _resolve_session(cfg: Config, ref: str) -> Path:
    from .storage import list_sessions

    p = Path(ref).expanduser()
    if p.exists():
        return p
    if ref in ("last", "latest"):
        sessions = list_sessions(cfg.data_path)
        # the newest *finished* meeting: `voicestream run` keeps the one it is recording in the list too
        for d in sessions:
            if not _in_progress(d):
                return d
        if sessions:
            return sessions[0]
    for d in list_sessions(cfg.data_path):
        if d.name.startswith(ref):
            return d
    raise SystemExit(f"no session matching {ref!r} in {cfg.data_path / 'sessions'}")


def cmd_summarize(args: argparse.Namespace) -> int:
    from .brain.llm import ClaudeLLM, LLMUnavailable
    from .brain.summarizer import summarize
    from .brain.tracker import ItemTracker
    from .costs import CostMeter
    from .storage import atomic_write, load_meeting

    cfg = load_config(args.config)
    meeting, folder = load_meeting(_resolve_session(cfg, args.session))
    costs = CostMeter(cfg)

    async def go() -> str:
        try:
            llm = ClaudeLLM(on_usage=costs.add_llm)
        except LLMUnavailable as exc:
            print(f"note: {exc}; writing notes without a model summary")
            llm = None
        notes = await summarize(cfg, meeting, ItemTracker(meeting, None, cfg.user_name), llm)
        if llm:
            await llm.aclose()
        return notes

    notes = asyncio.run(go())
    atomic_write(folder / "notes.md", notes)
    atomic_write(folder / "meeting.json", meeting.model_dump_json(indent=1))
    print(notes)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from .dispatch import Dispatcher
    from .events import EventBus
    from .models import Event
    from .sinks.base import build_sinks
    from .storage import atomic_write, load_meeting

    cfg = load_config(args.config)
    meeting, folder = load_meeting(_resolve_session(cfg, args.session))
    if meeting.ended_at is None and not args.force:
        print(f"error: {folder.name} is still in progress (is `voicestream run` recording it?). "
              "End the meeting first, or pass --force.", file=sys.stderr)
        return 2
    sinks = build_sinks(cfg, only=args.sink or None)
    if not sinks:
        print("no enabled sinks (configure [sinks.<name>] in voicestream.toml)")
        return 1
    cfg.dispatch.mode = "end"
    bus = EventBus()
    failures: list[str] = []

    def on_event(ev: Event) -> None:
        if ev.type == "sink.error":
            failures.append(f"{ev.data.get('item_id') or 'notes'} → {ev.data.get('sink')}: {ev.data.get('error')}")

    bus.listen(on_event)

    def save() -> None:
        atomic_write(folder / "meeting.json", meeting.model_dump_json(indent=1))

    async def go() -> None:
        # saved after every push: an interrupted run (Ctrl+C on a slow sink) keeps the refs of what it created
        d = Dispatcher(cfg, meeting, sinks, bus, on_change=save)
        try:
            n = await d.sync(require_stable=False)
            synced = n == 0 and any(i.pushed for i in meeting.items.values())
            print(f"pushed/updated {n} item(s)" + (" (the rest were already up to date)" if synced else ""))
            if meeting.summary_markdown and not args.no_notes:
                refs = await d.push_notes(meeting.summary_markdown)
                for name, ref in refs.items():
                    print(f"notes → {name}: {ref}")
        finally:
            await d.aclose()

    try:
        asyncio.run(go())
    finally:
        save()
    if failures:
        print(f"{len(failures)} push(es) failed:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    from . import stt_models

    cfg = load_config(args.config)
    if args.action in ("download", "remove") and not args.name:
        print(f"error: name a model, e.g. `voicestream models {args.action} {stt_models.DEFAULT_MODEL}`", file=sys.stderr)
        return 2
    if args.action == "download":
        names = [args.name] + ([stt_models.VAD_MODEL] if cfg.transcription.whisper_cpp.vad else [])
        for name in dict.fromkeys(names):
            if stt_models.is_installed(cfg, name):
                print(f"{name}: already downloaded")
                continue
            shown = [-1]

            def progress(done: int, total: int | None, name: str = name) -> None:
                pct = int(done * 100 / total) if total else -1
                if pct != shown[0]:
                    shown[0] = pct
                    size = f"{done / 1e6:.0f}/{total / 1e6:.0f} MB" if total else f"{done / 1e6:.0f} MB"
                    print(f"\r{name}: {size}" + (f" ({pct}%)" if pct >= 0 else ""), end="", flush=True)

            try:
                path = asyncio.run(stt_models.download(cfg, name, on_progress=progress))
            except stt_models.DownloadError as exc:
                print(f"\nerror: {exc}", file=sys.stderr)
                return 1
            print(f"\n{name}: saved to {path}")
        return 0
    if args.action == "remove":
        print(f"{args.name}: removed" if stt_models.remove(cfg, args.name) else f"{args.name}: not downloaded")
        return 0
    current = cfg.transcription.whisper_cpp.model
    print(f"models folder: {stt_models.models_dir(cfg)}")
    for m in stt_models.CATALOG.values():
        mark = "✔" if stt_models.is_installed(cfg, m.name) else " "
        use = "  ← in use" if m.name == current and cfg.transcription.backend == "whisper-cpp" else ""
        print(f" {mark} {m.name:<22} {m.size / 1e6:>6.0f} MB  {m.label:<16} {m.note}{use}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    from .costs import CostMeter, TokenUsage

    cfg = load_config(args.config)
    meter = CostMeter(cfg, persist=True)
    d = meter.today
    print(f"today: ${d.total_usd:.3f}  (speech-to-text ${d.stt_usd:.3f} for {d.stt_minutes:.1f} min, "
          f"Claude ${d.llm_usd:.3f}, agents ${d.agents_usd:.3f})")
    hours = args.hours
    speech_min = hours * 60 * 0.6 * 1.1  # 60% speech + VAD padding
    tc = cfg.transcription
    model = {"openai": tc.openai.model, "openai-realtime": tc.openai_realtime.model, "deepgram": tc.deepgram.model}.get(tc.backend, tc.backend)
    stt = meter.stt_cost(model, speech_min * 60)
    calls = hours * 3600 / max(30.0, cfg.brain.extract_interval_s)
    llm = calls * meter.llm_cost(TokenUsage(cfg.brain.model, input_tokens=1500, output_tokens=400, cache_read_tokens=6000, cache_write_tokens=600))
    summary = max(1, round(hours)) * meter.llm_cost(TokenUsage(cfg.brain.summary_model, input_tokens=25000, output_tokens=2500))
    print(f"estimate for {hours:g} h of meetings: speech-to-text ~${stt:.2f} ({model}), "
          f"live extraction ~${llm:.2f} ({cfg.brain.model}), summaries ~${summary:.2f} ({cfg.brain.summary_model}) "
          f"= ~${stt + llm + summary:.2f}/day before agents")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voicestream", description="Always-on meeting voice stream with Claude.")
    p.add_argument("--version", action="version", version=f"voicestream {__version__}")
    p.add_argument("-c", "--config", help="path to voicestream.toml")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--title", help="meeting title")
        sp.add_argument("--backend", choices=["openai", "openai-realtime", "deepgram", "whisper-cpp", "local", "fake"])
        sp.add_argument("--language", help="ISO-639-1 language hint, e.g. en")
        sp.add_argument("--agents", choices=["off", "suggest", "commands", "auto"])
        sp.add_argument("--dispatch", choices=["end", "immediate", "manual"])
        sp.add_argument("--offline", action="store_true", help="no Claude: offline heuristic extraction")
        sp.add_argument("--user", help="your name (owner of first-person commitments)")
        sp.add_argument("--data-dir", help="override data_dir")
        sp.add_argument("--port", type=int, help="dashboard port")

    r = sub.add_parser("run", help="listen live (mic + system audio) — the always-on mode")
    common(r)
    r.add_argument("--source", action="append", help="mic | system | file:<path>[@name] | ffmpeg:<args>[@name] (repeatable)")
    r.add_argument("--once", action="store_true", help="one meeting: don't auto-split on long silences")
    r.add_argument("--screen", action="store_true", help="also watch the screen (shared slides) — costs a bit more")
    r.add_argument("--no-dashboard", action="store_true")
    r.add_argument("--store-audio", action="store_true", help="also save raw audio in the session folder")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("replay", help="process a recording as if it were live (1 file = you, 2 files = you + others)")
    common(rp)
    rp.add_argument("files", nargs="+")
    rp.add_argument("--as", dest="as_source", choices=["mic", "system"], help="treat the (first) file as this source")
    rp.add_argument("--speed", type=float, default=0.0, help="0 = as fast as possible (default), 1 = real time")
    rp.add_argument("--dashboard", action="store_true", help="also serve the dashboard (it stays open until Ctrl+C)")
    rp.set_defaults(func=cmd_replay)

    im = sub.add_parser("import", help="process an existing transcript (.vtt/.srt from Zoom/Meet/Teams, 'Name: text' .txt)")
    common(im)
    im.add_argument("file")
    im.add_argument("--me", help="your name exactly as the transcript labels you, e.g. 'Alex Martin' (default: user_name)")
    im.add_argument("--speed", type=float, default=0.0, help="replay speed (0 = all at once)")
    im.add_argument("--date", help="when the meeting started, e.g. 2026-10-12T14:30 (default: the file's time minus its length)")
    im.add_argument("--dashboard", action="store_true", help="also serve the dashboard (it stays open until Ctrl+C)")
    im.set_defaults(func=cmd_import)

    sub.add_parser("devices", help="list audio devices and system-audio options").set_defaults(func=cmd_devices)
    sub.add_parser("doctor", help="check keys, devices, sinks and budget").set_defaults(func=cmd_doctor)
    gb = sub.add_parser("google-bridge", help="print the Google Apps Script bridge to paste (… | pbcopy)")
    gb.add_argument("--secret", action="store_true", help="print a new random secret for the bridge instead")
    gb.set_defaults(func=cmd_google_bridge)

    i = sub.add_parser("init", help="write a starter voicestream.toml and .env")
    i.add_argument("path", nargs="?")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("sessions", help="list recorded meetings")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    sm = sub.add_parser("summarize", help="re-run the summary for a saved meeting")
    sm.add_argument("session", help="folder, name prefix, or 'last'")
    sm.set_defaults(func=cmd_summarize)

    ps = sub.add_parser("push", help="push a saved meeting's items/notes to your sinks")
    ps.add_argument("session", help="folder, name prefix, or 'last'")
    ps.add_argument("--sink", action="append", help="only this sink (repeatable)")
    ps.add_argument("--no-notes", action="store_true")
    ps.add_argument("--force", action="store_true", help="also push a meeting that is still in progress")
    ps.set_defaults(func=cmd_push)

    mo = sub.add_parser("models", help="speech models for free on-device transcription (whisper.cpp)")
    mo.add_argument("action", nargs="?", choices=["list", "download", "remove"], default="list")
    mo.add_argument("name", nargs="?", help="e.g. large-v3-turbo-q5_0 (see `voicestream models`)")
    mo.set_defaults(func=cmd_models)

    c = sub.add_parser("cost", help="today's spend and a daily estimate")
    c.add_argument("--hours", type=float, default=8.0, help="hours of meetings per day for the estimate")
    c.set_defaults(func=cmd_cost)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    # everything voicestream writes (transcripts, notes, TODO.md, agent work) is for your eyes only
    umask = os.umask(0o077)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # `voicestream sessions | head`: the reader left; stop quietly
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())  # so exit's flush can't fail again
        return 141
    except (FileNotFoundError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        os.umask(umask)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
