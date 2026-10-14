"""Tiny in-process pub/sub bus.

Every interesting thing (new transcript segment, item added/updated, agent
progress, cost update...) is published as an :class:`~voicestream.models.Event`.
The dashboard, the console printer and the event log all subscribe to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from .models import Event

log = logging.getLogger(__name__)

Listener = Callable[[Event], Any]


class EventBus:
    def __init__(self, history: int = 500) -> None:
        self._queues: set[asyncio.Queue[Event]] = set()
        self._listeners: list[Listener] = []
        self._history: list[Event] = []
        self._history_max = history

    def publish(self, type: str, **data: Any) -> Event:
        event = Event(type=type, data=data)
        self._history.append(event)
        if len(self._history) > self._history_max:
            del self._history[: len(self._history) - self._history_max]
        for listener in list(self._listeners):
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    task = asyncio.ensure_future(result)
                    task.add_done_callback(_log_task_error)
            except Exception:  # a broken listener must never break the pipeline
                log.exception("event listener failed for %s", type)
        for q in list(self._queues):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop oldest for slow consumers
            q.put_nowait(event)
        return event

    def listen(self, fn: Listener) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    @contextlib.asynccontextmanager
    async def subscribe(self, maxsize: int = 1000) -> AsyncIterator[asyncio.Queue[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._queues.add(q)
        try:
            yield q
        finally:
            self._queues.discard(q)

    @property
    def history(self) -> list[Event]:
        return list(self._history)


def _log_task_error(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and (exc := task.exception()):
        log.error("async event listener failed: %r", exc)
