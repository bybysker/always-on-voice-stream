"""Smaller pieces of the app work: the cost estimate, doctor as data, Deepgram's multilingual mode,
and an assistant that fails to start closing what it opened."""

from __future__ import annotations

import asyncio
import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from test_session import IdleSource

from voicestream import cli
from voicestream.app.checks import Check, failed, run_checks
from voicestream.config import Config, DeepgramConfig
from voicestream.costs import CostMeter, TokenUsage, estimate_daily
from voicestream.session import Assistant
from voicestream.sinks.base import Sink, SinkConfig
from voicestream.transcribe.base import FakeTranscriber, TranscriptionError
from voicestream.transcribe.deepgram import DeepgramTranscriber


# ------------------------------------------------------------------------ cost


def _old_cmd_cost_numbers(cfg: Config, hours: float) -> tuple[float, float, float]:
    """The arithmetic ``voicestream cost`` did before it moved to costs.estimate_daily."""
    meter = CostMeter(cfg, persist=False)
    speech_min = hours * 60 * 0.6 * 1.1
    tc = cfg.transcription
    model = {"openai": tc.openai.model, "openai-realtime": tc.openai_realtime.model, "deepgram": tc.deepgram.model}.get(tc.backend, tc.backend)
    stt = meter.stt_cost(model, speech_min * 60)
    calls = hours * 3600 / max(30.0, cfg.brain.extract_interval_s)
    llm = calls * meter.llm_cost(TokenUsage(cfg.brain.model, input_tokens=1500, output_tokens=400, cache_read_tokens=6000, cache_write_tokens=600))
    summary = max(1, round(hours)) * meter.llm_cost(TokenUsage(cfg.brain.summary_model, input_tokens=25000, output_tokens=2500))
    return stt, llm, summary


@pytest.mark.parametrize("backend", ["openai", "openai-realtime", "deepgram"])
@pytest.mark.parametrize("hours", [2.0, 4.0, 8.0])
def test_estimate_matches_the_cli_for_cloud_speech_to_text(tmp_path: Path, backend: str, hours: float) -> None:
    cfg = Config(data_dir=str(tmp_path))
    cfg.transcription.backend = backend  # type: ignore[assignment]
    e = estimate_daily(cfg, hours)
    stt, llm, summary = _old_cmd_cost_numbers(cfg, hours)
    assert (e["stt_usd"], e["extraction_usd"], e["summaries_usd"]) == (stt, llm, summary)
    assert e["total_usd"] == pytest.approx(stt + llm + summary)
    assert e["per_month_usd"] == pytest.approx(20 * (stt + llm + summary))
    assert e["backend"] == backend and e["assumptions"]["workdays_per_month"] == 20


def test_estimate_known_values(tmp_path: Path) -> None:
    cfg = Config(data_dir=str(tmp_path))  # gpt-transcribe, Haiku extraction, Sonnet summaries
    e = estimate_daily(cfg, 8)
    assert round(e["stt_usd"], 2) == 1.43 and round(e["total_usd"], 2) == 2.18  # what `voicestream cost` always said
    cfg.transcription.backend = "deepgram"
    assert estimate_daily(cfg, 1)["stt_usd"] == pytest.approx(60 * 0.6 * 1.1 * 0.0092)  # nova-3 multilingual


@pytest.mark.parametrize("backend", ["whisper-cpp", "local"])
def test_speech_to_text_on_this_computer_is_free(tmp_path: Path, backend: str) -> None:
    cfg = Config(data_dir=str(tmp_path))
    cfg.transcription.backend = backend  # type: ignore[assignment]
    e = estimate_daily(cfg, 4)
    assert e["stt_usd"] == 0.0 and e["total_usd"] == pytest.approx(e["extraction_usd"] + e["summaries_usd"])
    assert 0.25 < e["total_usd"] < 0.6  # ~$0.38/day: Claude only
    cfg.brain.provider = "heuristic"
    assert estimate_daily(cfg, 4)["total_usd"] == 0.0  # offline: nothing at all


def test_cli_cost_prints_the_estimate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfgp = tmp_path / "voicestream.toml"
    cfgp.write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n')
    assert cli.main(["-c", str(cfgp), "cost", "--hours", "8"]) == 0
    out = capsys.readouterr().out
    assert "speech-to-text ~$1.43 (gpt-transcribe)" in out and "= ~$2.18/day before agents" in out and "/month" in out


# ---------------------------------------------------------------------- doctor


class _Ok(Sink):
    name = "ok"

    async def push_item(self, item: Any, meeting: Any) -> str:
        return ""

    async def check(self) -> str:
        return "all good"


def test_checks_are_data_and_the_doctor_prints_them(tmp_path: Path) -> None:
    cfgp = tmp_path / "voicestream.toml"
    cfgp.write_text(
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n[transcription]\nbackend = "deepgram"\n'
        "[sinks.notion]\nenabled = true\nbogus = 1\n"
    )
    cfg = cli.load_config(cfgp)
    checks = asyncio.run(run_checks(cfg))
    by_id = {c.id: c for c in checks}
    t = by_id["transcription"]
    assert (t.status, t.detail, t.action) == ("fail", "DEEPGRAM_API_KEY is not set", "secret:DEEPGRAM_API_KEY")
    assert by_id["extraction"].action == "secret:ANTHROPIC_API_KEY" and by_id["extraction"].status == "warn"
    assert by_id["sink:notion"].status == "fail" and by_id["sink:notion"].action == "settings:sinks.notion"
    assert checks[-1].id == "budget" and failed(checks)
    assert {"id", "area", "status", "title", "detail", "fix", "action"} == set(checks[0].as_dict())
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["-c", str(cfgp), "doctor"])
    lines = out.getvalue().splitlines()
    assert code == 1 and lines[0].startswith("voicestream ") and lines[1] == f"data dir: {cfg.data_path}"
    assert lines[2:] == [c.line() for c in asyncio.run(run_checks(cfg))]
    assert Check("x", "a", "ok", "title").line() == " ✔ title"
    assert Check("x", "a", "warn", "title", "why").line() == " ! title — why"


async def test_checks_use_the_hosts_meter(cfg: Config) -> None:
    meter = CostMeter(cfg, persist=False)
    meter.add_agent(0.5)
    checks = await run_checks(cfg, meter)
    assert checks[-1].title == "spend today $0.500"


# -------------------------------------------------------------------- deepgram


def test_deepgram_auto_language_is_multilingual() -> None:
    t = DeepgramTranscriber(DeepgramConfig(), api_key="k", language=None, keywords=["Acme"])
    assert "language=multi" in t.url() and "keyterm=Acme" in t.url()
    assert "language=fr" in DeepgramTranscriber(DeepgramConfig(), api_key="k", language="fr").url()
    assert "language=multi" not in t.url().replace("language=multi", "", 1)  # once
    older = DeepgramTranscriber(DeepgramConfig(model="nova-2"), api_key="k", language=None)
    assert "language=" not in older.url()  # no "multi" mode there: Deepgram's default


def test_nova_3_is_priced_multilingual() -> None:
    assert Config().pricing.stt_per_minute["nova-3"] == 0.0092


# ---------------------------------------------------------------- failed starts


class _Closing(Sink):
    name = "closing"

    def __init__(self, app: Config) -> None:
        super().__init__(SinkConfig(enabled=True), app)
        self.closed = False

    async def push_item(self, item: Any, meeting: Any) -> str:
        return ""

    async def aclose(self) -> None:
        self.closed = True


async def test_a_failed_start_closes_what_it_opened(cfg: Config) -> None:
    made: list[FakeTranscriber] = []
    closed: list[str] = []

    class Tracked(FakeTranscriber):
        async def aclose(self) -> None:
            closed.append(self.model)

    def factory(source: str) -> FakeTranscriber:
        if source == "system":
            raise TranscriptionError("DEEPGRAM_API_KEY is not set", fatal=True)
        t = Tracked(vad=cfg.vad)
        made.append(t)
        return t

    sink = _Closing(cfg)
    a = Assistant(cfg, sources=[IdleSource("mic"), IdleSource("system")], transcriber_factory=factory, llm=None, sinks=[sink])
    with pytest.raises(TranscriptionError):
        await a.run()
    assert len(made) == 1 and closed == ["fake"] and sink.closed  # e.g. a whisper-server child would be stopped
