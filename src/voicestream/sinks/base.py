"""Sink interface: where action items (and meeting notes) end up.

A sink receives items that pass its filters (kinds, owners, confidence) and
returns an external reference (URL or id) that is stored on the item, so the
same item is never created twice and later edits can be propagated.
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import Config
from ..models import Item, Meeting

log = logging.getLogger(__name__)


class SinkError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class SinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    kinds: list[Literal["action", "decision", "question", "note"]] = ["action"]
    owners: Literal["all", "mine"] = "all"
    """``mine`` = only items owned by you (e.g. your personal task list)."""
    min_confidence: float | None = None
    """Overrides ``dispatch.min_confidence`` for this sink."""
    include_agent_tasks: bool = True
    """Also push tasks that were delegated to an agent."""
    notes: bool = True
    """Push the meeting notes too (for sinks that support it)."""


class Sink(ABC):
    name: ClassVar[str]
    Config: ClassVar[type[SinkConfig]] = SinkConfig
    supports_update: ClassVar[bool] = False
    supports_notes: ClassVar[bool] = False

    def __init__(self, cfg: SinkConfig, app: Config) -> None:
        self.cfg = cfg
        self.app = app

    def accepts(self, item: Item, default_min_conf: float) -> bool:
        if item.dismissed or item.kind.value not in self.cfg.kinds:
            return False
        if item.confidence < (self.cfg.min_confidence if self.cfg.min_confidence is not None else default_min_conf):
            return False
        if self.cfg.owners == "mine" and not item.is_mine(self.app.user_name):
            return False
        if not self.cfg.include_agent_tasks and item.agent is not None:
            return False
        return True

    @abstractmethod
    async def push_item(self, item: Item, meeting: Meeting) -> str:
        """Create the item externally; return a URL or id."""

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        """Propagate edits (title/owner/due/status/dismissed). Optional."""

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        """Publish the meeting notes; return a URL/id. Optional."""
        return None

    async def check(self) -> str:
        """Validate credentials/config without side effects; return a short status line."""
        return "ok"

    async def aclose(self) -> None:
        pass


# name -> "module:Class" (imported lazily so optional sinks never break startup)
REGISTRY: dict[str, str] = {
    "markdown": "voicestream.sinks.markdown:MarkdownSink",
    "notion": "voicestream.sinks.notion:NotionSink",
    "monday": "voicestream.sinks.monday:MondaySink",
    "linear": "voicestream.sinks.linear:LinearSink",
    "github": "voicestream.sinks.github:GitHubSink",
    "slack": "voicestream.sinks.slack:SlackSink",
    "webhook": "voicestream.sinks.webhook:WebhookSink",
    "todoist": "voicestream.sinks.todoist:TodoistSink",
}


def sink_class(name: str) -> type[Sink]:
    target = REGISTRY.get(name)
    if target is None:
        raise KeyError(f"unknown sink '{name}' (known: {', '.join(sorted(REGISTRY))})")
    module, _, cls = target.partition(":")
    return getattr(importlib.import_module(module), cls)


def build_sinks(app: Config, only: list[str] | None = None) -> list[Sink]:
    """Instantiate every enabled sink from ``app.sinks``; misconfigured ones are skipped loudly."""
    sinks: list[Sink] = []
    for name, raw in app.sinks.items():
        if only is not None and name not in only:
            continue
        raw = dict(raw or {})
        if not raw.get("enabled", name == "markdown"):
            continue
        raw.setdefault("enabled", True)
        try:
            cls = sink_class(name)
            cfg = cls.Config.model_validate(raw)
            sinks.append(cls(cfg, app))
        except (KeyError, ImportError) as exc:
            log.error("sink %s unavailable: %s", name, exc)
        except ValidationError as exc:
            log.error("sink %s misconfigured: %s", name, exc)
        except SinkError as exc:
            log.error("sink %s disabled: %s", name, exc)
    return sinks


def item_markdown(item: Item, meeting: Meeting) -> str:
    """Body text used by sinks that accept Markdown descriptions."""
    lines = []
    if item.detail:
        lines += [item.detail, ""]
    meta = []
    if item.owner:
        meta.append(f"**Owner:** {item.owner}")
    if item.due_date or item.due_text:
        due = item.due_date or ""
        if item.due_text and item.due_text != item.due_date:
            due = f"{due} ({item.due_text})" if due else item.due_text
        meta.append(f"**Due:** {due}")
    if item.priority:
        meta.append(f"**Priority:** {item.priority}")
    if meta:
        lines += [" · ".join(meta), ""]
    if item.evidence:
        lines += [f"> {item.evidence}", ""]
    if item.agent and item.agent.result_summary:
        lines += ["**Agent result:**", "", item.agent.result_summary, ""]
    title = meeting.title or f"meeting on {meeting.started_at.astimezone():%Y-%m-%d %H:%M}"
    lines.append(f"_Captured by voicestream from {title}._")
    return "\n".join(lines).strip()


def env_secret(cfg: Any, field: str, default_env: str) -> str:
    """Read a token from the env var named by ``cfg.<field>`` (default ``default_env``)."""
    import os

    var = getattr(cfg, field, None) or default_env
    value = os.environ.get(var)
    if not value:
        raise SinkError(f"environment variable {var} is not set", retryable=False)
    return value
