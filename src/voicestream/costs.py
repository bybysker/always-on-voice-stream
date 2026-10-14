"""Cost meter: tracks transcription minutes and Claude tokens against a daily budget.

Spend is persisted per day in ``<data_dir>/spend/YYYY-MM-DD.json`` so the
budget holds across restarts and auto-split meetings.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .config import Config, ModelPrice
from .events import EventBus

log = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_anthropic(cls, model: str, usage: object) -> TokenUsage:
        def g(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        return cls(
            model=model,
            input_tokens=g("input_tokens"),
            output_tokens=g("output_tokens"),
            cache_read_tokens=g("cache_read_input_tokens"),
            cache_write_tokens=g("cache_creation_input_tokens"),
        )


@dataclass
class DaySpend:
    day: str
    stt_usd: float = 0.0
    stt_minutes: float = 0.0
    llm_usd: float = 0.0
    agents_usd: float = 0.0
    tokens: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_usd(self) -> float:
        return self.stt_usd + self.llm_usd + self.agents_usd


class CostMeter:
    def __init__(self, cfg: Config, bus: EventBus | None = None, persist: bool = True) -> None:
        self.cfg = cfg
        self.bus = bus
        self.persist = persist
        self._lock = threading.Lock()
        self._day = self._load(date.today().isoformat())
        self.session = DaySpend(day="session")

    # ------------------------------------------------------------------ pricing

    def model_price(self, model: str) -> ModelPrice | None:
        prices = self.cfg.pricing.llm_per_mtok
        if model in prices:
            return prices[model]
        # tolerate suffixed ids / unknown minor versions by family prefix
        for name, price in prices.items():
            family = name.rsplit("-", 2)[0]  # e.g. claude-haiku
            if model.startswith(family):
                return price
        return None

    def llm_cost(self, u: TokenUsage) -> float:
        p = self.model_price(u.model)
        if p is None:
            return 0.0
        return (
            u.input_tokens * p.input
            + u.output_tokens * p.output
            + u.cache_read_tokens * p.cache_read
            + u.cache_write_tokens * p.cache_write
        ) / 1_000_000

    def stt_cost(self, model: str, seconds: float) -> float:
        per_min = self.cfg.pricing.stt_per_minute.get(model, 0.0)
        return per_min * seconds / 60.0

    # ----------------------------------------------------------------- recording

    def add_stt(self, model: str, seconds: float) -> None:
        usd = self.stt_cost(model, seconds)
        with self._lock:
            self._roll_day()
            for d in (self._day, self.session):
                d.stt_usd += usd
                d.stt_minutes += seconds / 60.0
        self._changed()

    def add_llm(self, u: TokenUsage) -> float:
        usd = self.llm_cost(u)
        with self._lock:
            self._roll_day()
            for d in (self._day, self.session):
                d.llm_usd += usd
                t = d.tokens.setdefault(u.model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
                t["in"] += u.input_tokens
                t["out"] += u.output_tokens
                t["cache_read"] += u.cache_read_tokens
                t["cache_write"] += u.cache_write_tokens
        self._changed()
        return usd

    def add_agent(self, usd: float) -> None:
        with self._lock:
            self._roll_day()
            self._day.agents_usd += usd
            self.session.agents_usd += usd
        self._changed()

    # -------------------------------------------------------------------- budget

    @property
    def today(self) -> DaySpend:
        with self._lock:
            self._roll_day()
            return self._day

    def over_budget(self) -> bool:
        d = self.today
        return (d.stt_usd + d.llm_usd) >= self.cfg.budget.daily_usd

    def agents_over_budget(self, upcoming: float = 0.0) -> bool:
        return self.today.agents_usd + upcoming > self.cfg.budget.agents_daily_usd

    def snapshot(self) -> dict[str, object]:
        d = self.today
        return {
            "today": {**asdict(d), "total_usd": round(d.total_usd, 4)},
            "session": {**asdict(self.session), "total_usd": round(self.session.total_usd, 4)},
            "daily_budget_usd": self.cfg.budget.daily_usd,
            "agents_daily_budget_usd": self.cfg.budget.agents_daily_usd,
        }

    # ------------------------------------------------------------------ storage

    def _path(self, day: str) -> Path:
        return self.cfg.data_path / "spend" / f"{day}.json"

    def _load(self, day: str) -> DaySpend:
        if self.persist:
            p = self._path(day)
            if p.is_file():
                try:
                    data = json.loads(p.read_text())
                    return DaySpend(**{k: v for k, v in data.items() if k in DaySpend.__dataclass_fields__})
                except (OSError, ValueError, TypeError):
                    log.warning("could not read %s; starting the day at $0", p)
        return DaySpend(day=day)

    def _roll_day(self) -> None:
        today = date.today().isoformat()
        if self._day.day != today:
            self._day = self._load(today)

    def _changed(self) -> None:
        if self.persist:
            try:
                p = self._path(self._day.day)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(asdict(self._day), indent=1))
                tmp.replace(p)
            except OSError as exc:
                log.debug("could not persist spend: %s", exc)
        if self.bus is not None:
            self.bus.publish("cost", **self.snapshot())
