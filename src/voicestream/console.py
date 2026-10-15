"""Terminal UI: prints the live transcript and item activity, and accepts simple
commands on stdin (``run A3``, ``dismiss A2``, ``push A1``, ``end``…) so the
assistant is fully usable without the web dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import sys
import threading
from typing import TYPE_CHECKING, Any, TextIO

from .models import Event, fmt_ts

if TYPE_CHECKING:
    from .session import Assistant

log = logging.getLogger(__name__)

HELP = """commands:
  run <id> [prompt…]   approve/run the agent for an item      reject <id>   decline it
  dismiss <id>         hide a wrong item                       restore <id>  undo dismiss
  push <id>            push an item to your sinks now          extract       extract now
  add <text>           add an action item ("add Call Sam by Friday")
  end                  end the current meeting (summary + push)
  status               show current items and spend            quit          stop listening
"""


class Style:
    def __init__(self, stream: TextIO) -> None:
        self.on = stream.isatty() and not os.environ.get("NO_COLOR")

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def dim(self, t: str) -> str:
        return self(t, "2")

    def bold(self, t: str) -> str:
        return self(t, "1")


class ConsolePrinter:
    def __init__(self, stream: TextIO | None = None, show_levels: bool = False) -> None:
        self.out = stream or sys.stdout
        self.s = Style(self.out)
        self.show_levels = show_levels
        self._lock = threading.Lock()

    def emit(self, line: str) -> None:
        with self._lock:
            print(line, file=self.out, flush=True)

    def __call__(self, ev: Event) -> None:
        try:
            self.handle(ev)
        except Exception:  # never break the bus
            log.debug("console print failed", exc_info=True)

    def handle(self, ev: Event) -> None:
        s, d = self.s, ev.data
        t = ev.type
        if t == "assistant.started":
            self.emit(s.bold("● voicestream listening"))
            for src in d.get("sources", []):
                self.emit(s.dim(f"  source     {src}"))
            self.emit(s.dim(f"  transcribe {d.get('backend')} · extract {d.get('extraction')} · agents {d.get('agents')}"
                            + (" · always-on" if d.get("always_on") else "")))
            self.emit(s.dim("  type 'help' for commands, Ctrl+C to stop"))
            self.emit(s("  ⚑ let everyone on the call know they're being transcribed", "33"))
        elif t == "meeting.started":
            self.emit(s.bold(f"\n── meeting started{': ' + d['title'] if d.get('title') else ''} ──"))
            self.emit(s.dim(f"   {d.get('dir')}"))
        elif t == "segment":
            seg = d["segment"]
            color = "36" if seg.get("source") == "mic" else "33"
            self.emit(f"{s.dim('[' + fmt_ts(seg['t_start']) + ']')} {s(seg['speaker'], color)}: {seg['text']}")
        elif t in ("item.added", "item.updated"):
            it = d["item"]
            if it.get("dismissed") and t == "item.updated":
                self.emit(s.dim(f"   ✗ {it['id']} dismissed: {it['title']}"))
                return
            mark = "✚" if t == "item.added" else "✎"
            bits = [f"{it['id']} {it['kind']}: {s.bold(it['title'])}"]
            if it.get("owner"):
                bits.append(f"@{it['owner']}")
            if it.get("due_date") or it.get("due_text"):
                bits.append(f"due {it.get('due_date') or it.get('due_text')}")
            line = f"   {s(mark, '32')} " + " — ".join(bits)
            agent = it.get("agent")
            if agent and agent.get("status") == "suggested" and t == "item.added":
                line += s(f"   [agent can do this: 'run {it['id']}']", "35")
            self.emit(line)
        elif t == "agent.updated" and not d.get("moved"):
            it = d["item"]
            a = it.get("agent") or {}
            st = a.get("status")
            if st == "running":
                self.emit(s(f"   🤖 {it['id']} agent working: {it['title']}", "35"))
            elif st == "done":
                first = (a.get("result_summary") or "").strip().splitlines()[:1]
                self.emit(s(f"   🤖 {it['id']} agent done (${a.get('cost_usd', 0):.2f}): {first[0] if first else ''}", "35"))
                if a.get("workspace"):
                    self.emit(s.dim(f"      → {a['workspace']}"))
            elif st == "failed":
                self.emit(s(f"   🤖 {it['id']} agent failed: {a.get('error')}", "31"))
            elif st == "queued":
                self.emit(s.dim(f"   🤖 {it['id']} agent queued"))
        elif t == "item.pushed":
            self.emit(s.dim(f"   ↗ {d['item_id']} → {d['sink']} {d.get('ref') or ''}"))
        elif t == "notes.pushed":
            self.emit(s.dim(f"   ↗ notes → {d['sink']} {d.get('ref') or ''}"))
        elif t in ("sink.error", "source.error"):
            what = d.get("sink") or d.get("source")
            self.emit(s(f"   ⚠ {what}: {d.get('error')}", "31"))
        elif t == "agent.budget":
            self.emit(s(f"   ⚠ daily agent budget reached; {d.get('item_id')} needs manual approval", "33"))
        elif t == "screen":
            self.emit(s.dim(f"   🖥  {d['note']['description'][:160]}"))
        elif t == "meeting.ending":
            self.emit(s.dim("   … wrapping up the meeting (final extraction, summary, push)"))
        elif t == "meeting.ended" and not d.get("notes"):
            self.emit(s.dim("── meeting closed: nothing was said, nothing kept ──"))
        elif t == "meeting.ended":
            self.emit(s.bold(f"── meeting ended: {d.get('title') or 'untitled'} · {d.get('items', 0)} items ──"))
            self.emit(f"   notes: {d.get('notes')}")
        elif t == "meeting.notes_updated":
            self.emit(s.dim(f"   notes updated with late agent results: {d.get('notes')}"))
        elif t == "assistant.stopped":
            cost = (d.get("cost") or {}).get("session") or {}
            today = (d.get("cost") or {}).get("today") or {}
            self.emit(
                s.dim(
                    f"● stopped · this run ${cost.get('total_usd', 0):.3f} "
                    f"(speech-to-text {cost.get('stt_minutes', 0):.1f} min) · today ${today.get('total_usd', 0):.3f}"
                )
            )
        elif t == "level" and self.show_levels:
            self.emit(s.dim(f"   {d['source']}: {d['db']:6.1f} dBFS {'speech' if d['speech'] else ''}"))


async def command_loop(assistant: Assistant, printer: ConsolePrinter, stream: TextIO | None = None) -> None:
    """Read commands from stdin (in a thread) until EOF or ``quit``."""
    stream = stream or sys.stdin
    if not stream.isatty() and stream is sys.stdin:
        return
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, stream.readline)
        if not line:
            return
        try:
            if await handle_command(assistant, printer, line.strip()) == "quit":
                return
        except Exception as exc:
            printer.emit(printer.s(f"   ⚠ {exc}", "31"))


async def handle_command(assistant: Assistant, printer: ConsolePrinter, line: str) -> str | None:
    if not line:
        return None
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    cmd, args = parts[0].lower(), parts[1:]
    emit = printer.emit
    if cmd in ("help", "?", "h"):
        emit(HELP)
    elif cmd in ("quit", "exit", "stop", "q"):
        assistant.stop()
        return "quit"
    elif cmd == "end":
        await assistant.end_meeting("manual")
    elif cmd == "extract":
        await assistant.extract_now()
    elif cmd in ("run", "approve") and args:
        ok = assistant.approve_agent(args[0].upper(), " ".join(args[1:]) or None)
        emit(f"   {'queued' if ok else 'cannot run'} {args[0].upper()}")
    elif cmd == "reject" and args:
        assistant.reject_agent(args[0].upper())
    elif cmd == "dismiss" and args:
        assistant.dismiss_item(args[0].upper(), True)
    elif cmd == "restore" and args:
        assistant.dismiss_item(args[0].upper(), False)
    elif cmd == "push" and args:
        res = await assistant.push_item(args[0].upper())
        emit(f"   pushed: {res or 'no sink accepted it'}")
    elif cmd == "add" and args:
        it = assistant.add_item("action", " ".join(args))
        emit(f"   added {it['id']}")
    elif cmd == "status":
        _print_status(assistant, printer)
    else:
        emit(f"   unknown command {cmd!r} — type 'help'")
    return None


def _print_status(assistant: Assistant, printer: ConsolePrinter) -> None:
    st: dict[str, Any] = assistant.state()
    m = st.get("meeting")
    if not m:
        printer.emit("   no meeting in progress")
    else:
        printer.emit(f"   meeting: {m.get('title') or 'untitled'} · {len(m['segments'])} lines")
        for it in m["items"].values():
            if it.get("dismissed"):
                continue
            agent = (it.get("agent") or {}).get("status")
            printer.emit(f"   {it['id']:>4} {it['kind']:<8} {it['title']}" + (f"  [agent {agent}]" if agent else ""))
    today = st["cost"]["today"]
    printer.emit(f"   spend today ${today['total_usd']:.3f} of ${st['cost']['daily_budget_usd']:.2f} budget")


def install_signal_handlers(assistant: Assistant) -> None:
    """First Ctrl+C: graceful stop (summary + push). Second: hard exit."""
    loop = asyncio.get_running_loop()
    hits = {"n": 0}

    def on_sigint() -> None:
        hits["n"] += 1
        if hits["n"] == 1:
            print("\nstopping… (finishing the meeting; Ctrl+C again to abort)", file=sys.stderr, flush=True)
            assistant.stop()
        else:
            os._exit(130)

    import signal

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows
            loop.add_signal_handler(sig, on_sigint)
