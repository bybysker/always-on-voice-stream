"""Live extraction loop: decides *when* to ask Claude about new transcript lines
and feeds the answer into the :class:`~voicestream.brain.tracker.ItemTracker`.

Cost control:
* only runs when there is enough new speech (``min_new_chars``), at most every
  ``extract_interval_s`` — sooner when a trigger phrase ("can you…", "by Friday")
  or a wake phrase ("Hey Claude, …") is heard;
* the transcript is sent as append-only blocks with a cache breakpoint on the
  newest one, so each call re-reads earlier context at ~10% of the price;
* falls back to the offline heuristic when there's no API key or the daily
  budget is spent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date

from ..config import Config
from ..costs import CostMeter
from ..events import EventBus
from ..models import Item, Meeting, ScreenNote, TranscriptSegment
from .heuristic import extract_heuristic
from .llm import LLM, LLMError, LLMUnavailable, text_block
from .phrases import find_phrase
from .prompts import EXTRACTION_SYSTEM, meeting_context, render_items, render_screen, render_segment
from .schema import WireExtraction, wire_to_result
from .tracker import ExtractionResult, ItemTracker

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 240_000  # ~60k tokens; keeps Haiku in its cheaper <=100k-token tier


class Extractor:
    def __init__(
        self,
        cfg: Config,
        meeting: Meeting,
        tracker: ItemTracker,
        llm: LLM | None,
        costs: CostMeter | None = None,
        bus: EventBus | None = None,
        now: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.cfg = cfg
        self.bc = cfg.brain
        self.meeting = meeting
        self.tracker = tracker
        self.llm = llm if cfg.brain.provider == "anthropic" else None
        self.costs = costs
        self.bus = bus
        self.now = now
        self.today = today
        self.processed_upto = 0  # segments [0, processed_upto) have been extracted
        self._screens_upto = 0
        self._blocks: list[str] = []  # append-only transcript blocks already sent to Claude
        self._wake = asyncio.Event()
        self._stopped = False
        self._last_run = now()
        self._trigger_at: float | None = None
        self._failures = 0
        self._last_seg_seen: float | None = None
        self._lock = asyncio.Lock()
        self.rounds = 0
        self.mode = "llm" if self.llm is not None else "heuristic"

    # ------------------------------------------------------------------ inputs

    def on_segment(self, seg: TranscriptSegment) -> None:
        now = self.now()
        self._last_seg_seen = now
        if seg.source in self.bc.commands_from and find_phrase(seg.text, self.bc.wake_phrases):
            # someone addressed the assistant: answer fast (short delay to catch the rest)
            self._trigger_at = min(self._trigger_at or now + 3.0, now + 3.0)
        elif find_phrase(seg.text, self.bc.trigger_phrases):
            at = now + self.bc.trigger_delay_s
            self._trigger_at = min(self._trigger_at or at, at)
        self._wake.set()

    def on_screen(self, note: ScreenNote) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    # ------------------------------------------------------------------- loop

    def pending_segments(self) -> list[tuple[int, TranscriptSegment]]:
        segs = self.meeting.segments
        return [(i, segs[i]) for i in range(self.processed_upto, len(segs))]

    def pending_chars(self) -> int:
        return sum(len(s.text) for _, s in self.pending_segments())

    def due(self) -> bool:
        pending = self.pending_chars()
        if pending == 0:
            return False
        now = self.now()
        if self._trigger_at is not None and now >= self._trigger_at and now - self._last_run >= 8.0:
            return True
        since = now - self._last_run
        if pending >= self.bc.min_new_chars and since >= self.bc.extract_interval_s:
            return True
        # speech stopped a while ago: flush what we have so items show up promptly
        if self._last_seg_seen is not None and now - self._last_seg_seen >= 30.0 and since >= 20.0:
            return True
        # don't let a trickle of short lines wait forever
        return since >= 4 * self.bc.extract_interval_s

    async def run(self) -> None:
        """Run until :meth:`stop`; a final pass is done separately via :meth:`flush`."""
        while not self._stopped:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stopped:
                break
            if self.due():
                try:
                    await self.extract_now("scheduled")
                except Exception:  # never let the loop die
                    log.exception("extraction round failed")

    async def flush(self) -> tuple[list[Item], list[Item]]:
        """Final extraction over everything not processed yet."""
        if self.pending_segments():
            return await self.extract_now("final")
        return [], []

    # -------------------------------------------------------------- extraction

    async def extract_now(self, reason: str = "manual") -> tuple[list[Item], list[Item]]:
        async with self._lock:
            pending = self.pending_segments()
            if not pending:
                return [], []
            upto = pending[-1][0] + 1
            screens = self.meeting.screen_notes[self._screens_upto :]
            self._last_run = self.now()
            self._trigger_at = None
            use_llm = self.llm is not None and not (self.costs and self.costs.over_budget())
            if self.llm is not None and not use_llm and self.mode != "heuristic-budget":
                log.warning("daily budget reached: switching extraction to the offline heuristic")
                self.mode = "heuristic-budget"
            result: ExtractionResult | None = None
            if use_llm:
                try:
                    result = await self._extract_llm(pending, screens)
                    self._failures = 0
                    self.mode = "llm"
                except LLMUnavailable as exc:
                    log.error("Claude unavailable (%s); using the offline heuristic from now on", exc)
                    self.llm = None
                    self.mode = "heuristic"
                except LLMError as exc:
                    self._failures += 1
                    log.warning("extraction failed (%s), attempt %d", exc, self._failures)
                    if self._failures < 3 and reason != "final":
                        self._trigger_at = self.now() + min(60.0, 10.0 * self._failures)
                        return [], []  # keep lines pending; retry soon
                    self._failures = 0
                    self.mode = "heuristic"  # this round only: the next one tries Claude again
            if result is None:
                result = extract_heuristic(
                    pending,
                    user_name=self.cfg.user_name,
                    wake_phrases=self.bc.wake_phrases,
                    commands_from=self.bc.commands_from,
                )
                if self.llm is not None:
                    # Claude is back later (retry, budget): it must still see these lines as context
                    self._blocks = self._blocks_with(pending, screens)
            refs = {i: s.id for i, s in pending}
            # allow references to earlier lines too (updates often cite them)
            for i in range(max(0, pending[0][0] - 200), pending[0][0]):
                refs[i] = self.meeting.segments[i].id
            self._enforce_command_sources(result, pending)
            added, updated = self.tracker.apply(result, refs)
            self.processed_upto = upto
            self._screens_upto += len(screens)
            self.rounds += 1
            if self.bus:
                self.bus.publish(
                    "extraction",
                    meeting_id=self.meeting.id,
                    reason=reason,
                    mode=self.mode,
                    lines=len(pending),
                    added=[i.id for i in added],
                    updated=[i.id for i in updated],
                )
            return added, updated

    def _enforce_command_sources(self, result: ExtractionResult, pending: list[tuple[int, TranscriptSegment]]) -> None:
        """A direct request to the assistant counts only if one of the lines the item cites is new this
        round, comes from ``brain.commands_from`` (your mic) and contains a wake phrase — whatever the model
        says — so other participants can't trigger auto-run agents: neither by getting you to answer "yeah
        sure" to their "Hey Claude…" nor by the model citing an old command of yours."""
        allowed = set(self.bc.commands_from)
        commands = {i for i, s in pending if s.source in allowed and find_phrase(s.text, self.bc.wake_phrases)}
        for item in result.new_items:
            if item.explicit_request and not commands.intersection(item.segment_refs):
                item.explicit_request = False

    def _blocks_with(self, pending: list[tuple[int, TranscriptSegment]], screens: list[ScreenNote]) -> list[str]:
        """The blocks sent so far plus one for ``pending``. Blocks are byte-identical once sent and only
        ever appended, so every call hits the prompt cache for all earlier blocks."""
        new_block = f"<transcript part='{self.rounds + 1}'>\n{self._build_block(pending, screens)}\n</transcript>"
        blocks = self._blocks + [new_block]
        # cap context: drop the oldest blocks (one-off cache miss) when the meeting gets long
        while len(blocks) > 1 and sum(len(b) for b in blocks) > MAX_CONTEXT_CHARS:
            blocks.pop(0)
        return blocks

    def _build_block(self, pending: list[tuple[int, TranscriptSegment]], screens: list[ScreenNote]) -> str:
        events: list[tuple[float, str]] = [(s.t_start, render_segment(i, s)) for i, s in pending]
        events += [(n.t, render_screen(n)) for n in screens]
        events.sort(key=lambda e: e[0])
        return "\n".join(text for _, text in events)

    async def _extract_llm(
        self, pending: list[tuple[int, TranscriptSegment]], screens: list[ScreenNote]
    ) -> ExtractionResult:
        assert self.llm is not None
        part = self.rounds + 1
        blocks = self._blocks_with(pending, screens)
        content = [text_block(b) for b in blocks[:-1]]
        content.append(text_block(blocks[-1], cache=True))
        visible = [i for i in self.meeting.items.values() if not i.dismissed]
        content.append(
            text_block(
                meeting_context(self.meeting, self.cfg.user_name, self.today(), self.bc.attendees, self.bc.language)
                + (f"\nVocabulary: {', '.join(self.bc.vocabulary)}" if self.bc.vocabulary else "")
                + f"\n\nExisting items:\n{render_items(visible)}\n\n"
                f"Only transcript part {part} (lines #{pending[0][0]}–#{pending[-1][0]}) is new; earlier "
                "parts were already processed and are context only. Return new items and updates "
                "from the new lines, or empty lists if there is nothing new."
            )
        )
        wire = await self.llm.structured(
            model=self.bc.model,
            system=EXTRACTION_SYSTEM,
            content=content,
            schema=WireExtraction,
            max_tokens=self.bc.max_output_tokens,
            effort=self.bc.effort,
        )
        # only commit the block once the call succeeded (keeps the cache prefix stable)
        self._blocks = blocks
        return wire_to_result(wire)
