"""Local Markdown sink: always-available fallback.

Keeps a running ``TODO.md`` (one checklist line per action item, newest meeting
first) and writes every meeting's notes next to it.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import ClassVar

from ..models import Item, ItemStatus, Meeting
from ..storage import atomic_write, slugify
from .base import Sink, SinkConfig


class MarkdownConfig(SinkConfig):
    enabled: bool = True
    todo_file: str | None = None
    """Default: ``<data_dir>/TODO.md``."""
    notes_dir: str | None = None
    """Default: ``<data_dir>/notes/``."""


_LINE = re.compile(r"<!-- vs:(?P<ref>[^ ]+) -->$")


class MarkdownSink(Sink):
    name = "markdown"
    Config: ClassVar[type[SinkConfig]] = MarkdownConfig
    supports_update = True
    supports_notes = True

    def __init__(self, cfg: MarkdownConfig, app) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self.todo = Path(os.path.expanduser(cfg.todo_file)) if cfg.todo_file else app.data_path / "TODO.md"
        self.notes_dir = Path(os.path.expanduser(cfg.notes_dir)) if cfg.notes_dir else app.data_path / "notes"
        self._lock = asyncio.Lock()

    def _ref(self, item: Item, meeting: Meeting) -> str:
        return f"{meeting.id}/{item.id}"

    def _line(self, item: Item, meeting: Meeting) -> str:
        box = "x" if item.status == ItemStatus.DONE or item.dismissed else " "
        title = f"~~{item.title}~~" if item.dismissed or item.status == ItemStatus.CANCELLED else item.title
        bits = [f"- [{box}] {title}"]
        if item.owner:
            bits.append(f"@{item.owner.replace(' ', '_')}")
        if item.due_date or item.due_text:
            bits.append(f"📅 {item.due_date or item.due_text}")
        if item.agent and item.agent.status.value == "done":
            bits.append("🤖 done")
        label = meeting.title or meeting.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
        bits.append(f"_({label})_")
        return " ".join(bits) + f" <!-- vs:{self._ref(item, meeting)} -->"

    async def _rewrite(self, ref: str, line: str | None) -> None:
        async with self._lock:
            lines = self.todo.read_text(encoding="utf-8").splitlines() if self.todo.exists() else ["# TODO", ""]
            out, found = [], False
            for existing in lines:
                m = _LINE.search(existing)
                if m and m.group("ref") == ref:
                    found = True
                    if line is not None:
                        out.append(line)
                    continue
                out.append(existing)
            if not found and line is not None:
                # newest first, right after the header
                insert_at = 2 if len(out) >= 2 and out[0].startswith("#") else 0
                out.insert(insert_at, line)
            atomic_write(self.todo, "\n".join(out).rstrip() + "\n")

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        ref = self._ref(item, meeting)
        await self._rewrite(ref, self._line(item, meeting))
        return f"file://{self.todo}#{ref}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        await self._rewrite(self._ref(item, meeting), self._line(item, meeting))

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        stamp = meeting.started_at.astimezone().strftime("%Y-%m-%d_%H%M")
        path = self.notes_dir / f"{stamp}-{slugify(meeting.title or 'meeting')}.md"
        atomic_write(path, notes_markdown)
        return f"file://{path}"

    async def check(self) -> str:
        return f"writes {self.todo} and {self.notes_dir}/"
