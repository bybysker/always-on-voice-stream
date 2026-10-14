"""Command line: ``voicestream run | replay | import | devices | doctor | init | sessions | summarize | push | cost``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from importlib import resources
from pathlib import Path

from . import __version__
from .config import Config, load_config

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


async def _run_assistant(assistant, cfg: Config, *, transcript=None, speed: float = 0.0) -> int:  # noqa: ANN001
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
    commands = asyncio.ensure_future(command_loop(assistant, printer))
    try:
        if transcript is not None:
            await assistant.replay_transcript(transcript, speed=speed)
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
        return asyncio.run(_run_assistant(assistant, cfg))
    except TranscriptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_import(args: argparse.Namespace) -> int:
    from .session import Assistant
    from .transcript_import import parse_transcript

    cfg = _apply_overrides(load_config(args.config), args)
    segments = parse_transcript(args.file, cfg.user_name)
    if not segments:
        print(f"error: no transcript lines found in {args.file}", file=sys.stderr)
        return 2
    if not args.dashboard:
        cfg.dashboard.enabled = False
    print(f"importing {len(segments)} lines from {args.file}")
    assistant = Assistant(cfg, sources=[], title=args.title, always_on=False)
    return asyncio.run(_run_assistant(assistant, cfg, transcript=segments, speed=args.speed))


def cmd_devices(args: argparse.Namespace) -> int:
    try:
        from .audio.mic import describe_devices
    except ImportError as exc:
        print(f"audio capture libraries missing: {exc}\n  pip install 'voicestream[audio]'")
        return 1
    print(describe_devices())
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser() if args.path else Path("~/.voicestream/voicestream.toml").expanduser()
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    template = resources.files("voicestream").joinpath("config.example.toml").read_text(encoding="utf-8")
    target.write_text(template, encoding="utf-8")
    env = target.parent / ".env"
    if not env.exists():
        env.write_text(resources.files("voicestream").joinpath("env.example").read_text(encoding="utf-8"), encoding="utf-8")
        os.chmod(env, 0o600)
    print(f"wrote {target}\nwrote {env} (put your API keys there)\nnext: voicestream doctor")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ok = True

    def line(status: str, what: str, detail: str = "") -> None:
        mark = {"ok": "✔", "warn": "!", "fail": "✘"}[status]
        print(f" {mark} {what}" + (f" — {detail}" if detail else ""))

    print(f"voicestream {__version__} · python {sys.version.split()[0]} · {sys.platform}")
    print(f"data dir: {cfg.data_path}")
    # transcription
    be = cfg.transcription.backend
    need = {"openai": "OPENAI_API_KEY", "openai-realtime": "OPENAI_API_KEY", "deepgram": "DEEPGRAM_API_KEY"}.get(be)
    if need:
        if os.environ.get(need):
            line("ok", f"transcription: {be}", f"{need} set")
        else:
            ok = False
            line("fail", f"transcription: {be}", f"{need} is not set")
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
    if cfg.agents.mode != "off":
        if cfg.agents.engine == "agent-sdk":
            try:
                import claude_agent_sdk

                line("ok", f"agents: {cfg.agents.mode} via Agent SDK {claude_agent_sdk.__version__}", cfg.agents.model)
            except ImportError:
                line("warn", "agents: claude-agent-sdk not installed", "pip install 'voicestream[agents]' or set agents.engine = 'api'")
        else:
            line("ok", f"agents: {cfg.agents.mode} via single API calls", cfg.agents.model)
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
    # sinks
    from .sinks.base import build_sinks

    async def check_sinks() -> None:
        nonlocal ok
        for sink in build_sinks(cfg):
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
        print(f"{d.name:<48} {m.title or '(untitled)':<40} {len(m.segments):>4} lines {actions:>3} actions")
    return 0


def _resolve_session(cfg: Config, ref: str) -> Path:
    from .storage import list_sessions

    p = Path(ref).expanduser()
    if p.exists():
        return p
    if ref in ("last", "latest"):
        sessions = list_sessions(cfg.data_path)
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
    from .sinks.base import build_sinks
    from .storage import atomic_write, load_meeting

    cfg = load_config(args.config)
    meeting, folder = load_meeting(_resolve_session(cfg, args.session))
    sinks = build_sinks(cfg, only=args.sink or None)
    if not sinks:
        print("no enabled sinks (configure [sinks.<name>] in voicestream.toml)")
        return 1
    cfg.dispatch.mode = "end"

    async def go() -> None:
        d = Dispatcher(cfg, meeting, sinks)
        n = await d.sync(require_stable=False)
        print(f"pushed/updated {n} item(s)")
        if meeting.summary_markdown and not args.no_notes:
            refs = await d.push_notes(meeting.summary_markdown)
            for name, ref in refs.items():
                print(f"notes → {name}: {ref}")
        await d.aclose()

    asyncio.run(go())
    atomic_write(folder / "meeting.json", meeting.model_dump_json(indent=1))
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
        sp.add_argument("--backend", choices=["openai", "openai-realtime", "deepgram", "local", "fake"])
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
    rp.add_argument("--dashboard", action="store_true", help="also serve the dashboard while replaying")
    rp.set_defaults(func=cmd_replay)

    im = sub.add_parser("import", help="process an existing transcript (.vtt/.srt from Zoom/Meet/Teams, 'Name: text' .txt)")
    common(im)
    im.add_argument("file")
    im.add_argument("--speed", type=float, default=0.0, help="replay speed (0 = all at once)")
    im.add_argument("--dashboard", action="store_true")
    im.set_defaults(func=cmd_import)

    sub.add_parser("devices", help="list audio devices and system-audio options").set_defaults(func=cmd_devices)
    sub.add_parser("doctor", help="check keys, devices, sinks and budget").set_defaults(func=cmd_doctor)

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
    ps.set_defaults(func=cmd_push)

    c = sub.add_parser("cost", help="today's spend and a daily estimate")
    c.add_argument("--hours", type=float, default=8.0, help="hours of meetings per day for the estimate")
    c.set_defaults(func=cmd_cost)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
