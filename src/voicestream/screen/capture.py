"""Periodic screenshots → one-line descriptions of what's being shared.

Slides, docs and dashboards shown in a meeting often carry the names, numbers
and deadlines nobody says out loud. Every ``screen.interval_s`` the screen is
captured; if it changed meaningfully (cheap thumbnail diff) it is described by
a small vision model and the text is added to the transcript as a <screen>
line. Images never enter the cached extraction prompt — only the description.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import numpy as np

from ..audio.base import Clock
from ..brain.llm import LLM, LLMError, image_block, text_block
from ..brain.prompts import SCREEN_SYSTEM
from ..config import Config
from ..costs import CostMeter
from ..models import ScreenNote

log = logging.getLogger(__name__)

THUMB = (64, 36)


def grab(monitor: int = 1) -> Any:
    """Capture one monitor as a PIL image (``mss`` + Pillow)."""
    import mss  # type: ignore[import-not-found]
    from PIL import Image

    factory = getattr(mss, "MSS", None) or mss.mss  # mss 10.2 deprecated mss.mss()
    with factory() as sct:
        monitors = sct.monitors
        mon = monitors[monitor] if 0 <= monitor < len(monitors) else monitors[1 if len(monitors) > 1 else 0]
        shot = sct.grab(mon)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def thumbnail(img: Any) -> np.ndarray:
    small = img.convert("L").resize(THUMB)
    return np.asarray(small, dtype=np.float32) / 255.0


def changed_fraction(a: np.ndarray | None, b: np.ndarray) -> float:
    """Fraction of thumbnail pixels that changed noticeably (1.0 when there's no previous frame)."""
    if a is None or a.shape != b.shape:
        return 1.0
    return float(np.mean(np.abs(a - b) > 0.08))


def to_jpeg(img: Any, max_width: int) -> bytes:
    im = img
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=75)
    return buf.getvalue()


class ScreenWatcher:
    def __init__(
        self,
        cfg: Config,
        llm: LLM | None,
        costs: CostMeter | None = None,
        clock: Clock | None = None,
        grabber: Callable[[int], Any] = grab,
    ) -> None:
        if llm is None:
            raise RuntimeError("screen descriptions need Claude (ANTHROPIC_API_KEY)")
        try:
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            raise ImportError("Pillow is required: pip install 'voicestream[screen]'") from exc
        self.cfg = cfg
        self.sc = cfg.screen
        self.llm = llm
        self.costs = costs
        self.clock = clock or Clock()
        self.grabber = grabber
        self._prev: np.ndarray | None = None
        self._last_desc = ""
        self.captures = 0
        self.described = 0

    async def describe(self, jpeg: bytes) -> str:
        text = await self.llm.complete(
            model=self.sc.model,
            system=SCREEN_SYSTEM,
            content=[image_block(jpeg, "image/jpeg"), text_block("Describe what is on screen for the meeting notes.")],
            max_tokens=300,
            effort="low",
        )
        return " ".join(text.split())

    async def step(self) -> ScreenNote | None:
        """Capture once; return a note if the screen changed and shows something relevant."""
        img = await asyncio.to_thread(self.grabber, self.sc.monitor)
        self.captures += 1
        thumb = thumbnail(img)
        if changed_fraction(self._prev, thumb) < self.sc.change_threshold:
            return None
        self._prev = thumb
        if self.costs and self.costs.over_budget():
            return None
        jpeg = await asyncio.to_thread(to_jpeg, img, self.sc.max_width)
        try:
            desc = await self.describe(jpeg)
        except LLMError as exc:
            log.warning("screen description failed: %s", exc)
            return None
        self.described += 1
        if not desc or desc.lower().startswith("nothing relevant") or desc == self._last_desc:
            return None
        self._last_desc = desc
        path = None
        if self.sc.save_images:
            folder = self.cfg.data_path / "screens" / datetime.now().strftime("%Y-%m-%d")
            folder.mkdir(parents=True, exist_ok=True)
            p = folder / f"{datetime.now():%H%M%S}.jpg"
            p.write_bytes(jpeg)
            path = str(p)
        return ScreenNote(t=self.clock.now(), description=desc, image_path=path)

    async def run(self, deliver: Callable[[ScreenNote], None], should_capture: Callable[[], bool] = lambda: True) -> None:
        while True:
            await asyncio.sleep(self.sc.interval_s)
            if not should_capture():
                continue
            try:
                note = await self.step()
            except Exception as exc:  # permission denied, no display…
                log.error("screen capture failed (%s); disabling", exc)
                return
            if note is not None:
                deliver(note)
