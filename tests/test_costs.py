"""Cost meter: the daily budget is what keeps an always-on assistant at a dollar or two a day."""

from __future__ import annotations

import json
from datetime import date

import pytest

from voicestream import costs
from voicestream.config import Config
from voicestream.costs import CostMeter, TokenUsage
from voicestream.events import EventBus


class _Today:
    """Stand-in for ``datetime.date`` inside :mod:`voicestream.costs` with a settable today()."""

    value = date(2026, 10, 14)

    @classmethod
    def today(cls) -> date:
        return cls.value


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[_Today]:
    _Today.value = date(2026, 10, 14)
    monkeypatch.setattr(costs, "date", _Today)
    return _Today


def test_llm_cost_uses_per_model_prices_including_cache(cfg: Config) -> None:
    m = CostMeter(cfg, persist=False)
    u = TokenUsage("claude-haiku-5-5", input_tokens=1_000_000, output_tokens=1_000_000,
                   cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert m.llm_cost(u) == pytest.approx(0.10 + 0.50 + 0.01 + 0.125)
    assert m.llm_cost(TokenUsage("claude-sonnet-5-5", input_tokens=500_000, output_tokens=100_000)) == pytest.approx(1.0 + 1.0)
    assert m.llm_cost(TokenUsage("claude-opus-5-5", output_tokens=100_000, cache_write_tokens=200_000)) == pytest.approx(2.0 + 1.0)


def test_unknown_suffix_falls_back_to_family_price(cfg: Config) -> None:
    m = CostMeter(cfg, persist=False)
    haiku = cfg.pricing.llm_per_mtok["claude-haiku-5-5"]
    assert m.model_price("claude-haiku-5-5-20261001") == haiku
    assert m.model_price("claude-fable-5-1-preview") == cfg.pricing.llm_per_mtok["claude-fable-5-1"]
    assert m.model_price("gpt-5") is None
    assert m.llm_cost(TokenUsage("gpt-5", input_tokens=10**9)) == 0.0


def test_fallback_price_never_crosses_major_versions(cfg: Config) -> None:
    m = CostMeter(cfg, persist=False)
    assert m.model_price("claude-haiku-4-5") != cfg.pricing.llm_per_mtok["claude-haiku-5-5"]
    assert m.model_price("claude-opus-4-1-20250805") != cfg.pricing.llm_per_mtok["claude-opus-5-5"]
    assert m.model_price("claude-haiku-5") == cfg.pricing.llm_per_mtok["claude-haiku-5-5"]  # same major version
    assert m.model_price("claude-sonnet-5-7") == cfg.pricing.llm_per_mtok["claude-sonnet-5-5"]


def test_an_unpriced_model_is_free_with_one_warning(cfg: Config, caplog: pytest.LogCaptureFixture) -> None:
    m = CostMeter(cfg, persist=False)
    for _ in range(3):
        assert m.llm_cost(TokenUsage("claude-haiku-4-5", input_tokens=1_000_000)) == 0.0
    assert [r.getMessage() for r in caplog.records if "claude-haiku-4-5" in r.getMessage()] == [
        "no price for model claude-haiku-4-5: its spend counts as $0 (add it to [pricing.llm_per_mtok])"
    ]
    cfg.pricing.llm_per_mtok["claude-haiku-4-5"] = cfg.pricing.llm_per_mtok["claude-haiku-5-5"].model_copy(
        update={"input": 1.0, "output": 5.0}
    )
    assert m.llm_cost(TokenUsage("claude-haiku-4-5", input_tokens=1_000_000)) == pytest.approx(1.0)


def test_default_cache_read_prices(cfg: Config) -> None:
    # A tester's xfail claimed Sonnet 5.5 cache reads cost $0.20/MTok. Anthropic's pricing page (checked
    # 2026-10-14) bills cache reads at 0.05x the base input price on Opus and Sonnet 5.5: $0.10 for Sonnet
    # ($2 input) and $0.20 for Opus ($4 input). So the config is right and the claim was wrong.
    for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
        price = cfg.pricing.llm_per_mtok[model]
        assert price.cache_read == pytest.approx(0.05 * price.input)
    assert cfg.pricing.llm_per_mtok["claude-sonnet-5-5"].cache_read == pytest.approx(0.10)


def test_pricing_overrides_from_config(cfg: Config) -> None:
    cfg.pricing.llm_per_mtok["claude-haiku-5-5"] = cfg.pricing.llm_per_mtok["claude-haiku-5-5"].model_copy(update={"input": 1.0})
    assert CostMeter(cfg, persist=False).llm_cost(TokenUsage("claude-haiku-5-5", input_tokens=1_000_000)) == pytest.approx(1.0)


def test_stt_cost_per_minute(cfg: Config) -> None:
    m = CostMeter(cfg, persist=False)
    assert m.stt_cost("gpt-transcribe", 600) == pytest.approx(0.045)
    assert m.stt_cost("nova-3", 60) == pytest.approx(0.0077)
    assert m.stt_cost("local", 3600) == 0.0
    m.add_stt("gpt-transcribe", 120)
    assert m.today.stt_minutes == pytest.approx(2.0) and m.today.stt_usd == pytest.approx(0.009)


def test_token_usage_from_anthropic_usage_object() -> None:
    class Usage:
        input_tokens = 120
        output_tokens = 30
        cache_read_input_tokens = 4000
        cache_creation_input_tokens = None  # the API sends null when nothing was written

    u = TokenUsage.from_anthropic("claude-haiku-5-5", Usage())
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (120, 30, 4000, 0)


def test_spend_persists_per_day_across_instances(cfg: Config, clock: type[_Today]) -> None:
    a = CostMeter(cfg)
    a.add_llm(TokenUsage("claude-haiku-5-5", input_tokens=1_000_000, output_tokens=200_000))
    a.add_stt("gpt-transcribe", 60)
    a.add_agent(0.5)
    path = cfg.data_path / "spend" / "2026-10-14.json"
    saved = json.loads(path.read_text())
    assert saved["llm_usd"] == pytest.approx(0.2) and saved["tokens"]["claude-haiku-5-5"]["in"] == 1_000_000
    b = CostMeter(cfg)
    assert b.today.llm_usd == pytest.approx(0.2)
    assert b.today.stt_usd == pytest.approx(0.0045) and b.today.agents_usd == pytest.approx(0.5)
    assert b.session.total_usd == 0.0  # the session counter is per process
    b.add_llm(TokenUsage("claude-haiku-5-5", output_tokens=1_000_000))
    assert CostMeter(cfg).today.llm_usd == pytest.approx(0.7)
    assert CostMeter(cfg).today.tokens["claude-haiku-5-5"] == {"in": 1_000_000, "out": 1_200_000, "cache_read": 0, "cache_write": 0}


def test_no_files_without_persistence(cfg: Config) -> None:
    CostMeter(cfg, persist=False).add_stt("gpt-transcribe", 60)
    assert not (cfg.data_path / "spend").exists()


def test_corrupt_spend_file_starts_the_day_at_zero(cfg: Config, clock: type[_Today]) -> None:
    path = cfg.data_path / "spend" / "2026-10-14.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert CostMeter(cfg).today.total_usd == 0.0


def test_budget_checks(cfg: Config) -> None:
    cfg.budget.daily_usd = 0.01
    cfg.budget.agents_daily_usd = 1.0
    m = CostMeter(cfg, persist=False)
    m.add_agent(5.0)
    assert not m.over_budget()  # agent spend has its own cap
    m.add_stt("gpt-transcribe", 60)  # $0.0045
    assert not m.over_budget()
    m.add_llm(TokenUsage("claude-haiku-5-5", output_tokens=12_000))  # $0.006 -> $0.0105 total
    assert m.over_budget()

    n = CostMeter(cfg, persist=False)
    n.add_agent(0.6)
    assert not n.agents_over_budget()
    assert not n.agents_over_budget(upcoming=0.4)  # exactly at the cap is still allowed
    assert n.agents_over_budget(upcoming=0.5)


def test_snapshot_shape_and_cost_events(cfg: Config) -> None:
    bus = EventBus()
    m = CostMeter(cfg, bus=bus, persist=False)
    usd = m.add_llm(TokenUsage("claude-sonnet-5-5", input_tokens=1000, output_tokens=100))
    assert usd == pytest.approx(0.003)
    snap = m.snapshot()
    assert set(snap) == {"today", "session", "daily_budget_usd", "agents_daily_budget_usd"}
    for part in ("today", "session"):
        assert set(snap[part]) == {"day", "stt_usd", "stt_minutes", "llm_usd", "agents_usd", "tokens", "total_usd"}
    assert snap["today"]["total_usd"] == pytest.approx(0.003) and snap["session"]["day"] == "session"
    assert snap["daily_budget_usd"] == cfg.budget.daily_usd
    (event,) = bus.history
    assert event.type == "cost" and event.data == snap
    json.dumps(snap)  # streamed to the dashboard as-is


def test_day_rollover_starts_a_fresh_budget(cfg: Config, clock: type[_Today]) -> None:
    cfg.budget.daily_usd = 0.01
    m = CostMeter(cfg)
    m.add_stt("gpt-transcribe", 600)  # $0.045
    assert m.over_budget()
    clock.value = date(2026, 10, 15)
    assert not m.over_budget()
    assert m.today.day == "2026-10-15" and m.today.total_usd == 0.0
    m.add_stt("gpt-transcribe", 60)
    assert m.session.stt_minutes == pytest.approx(11.0)  # the session spans midnight
    spend = cfg.data_path / "spend"
    assert json.loads((spend / "2026-10-14.json").read_text())["stt_minutes"] == pytest.approx(10.0)
    assert json.loads((spend / "2026-10-15.json").read_text())["stt_minutes"] == pytest.approx(1.0)
