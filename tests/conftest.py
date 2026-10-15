from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from voicestream.config import Config  # noqa: E402
from voicestream.models import Meeting  # noqa: E402


@pytest.fixture(autouse=True)
def _private_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config() also reads ~/.voicestream/.env: never let a developer's real keys into a test."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Isolated config: data in tmp, offline extraction, no agents, no auto-split."""
    for var in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "NOTION_TOKEN", "MONDAY_TOKEN", "LINEAR_API_KEY",
        "GITHUB_TOKEN", "TODOIST_TOKEN", "SLACK_WEBHOOK_URL", "VOICESTREAM_WEBHOOK_URL", "VOICESTREAM_WEBHOOK_SECRET",
        "VOICESTREAM_GOOGLE_URL", "VOICESTREAM_GOOGLE_SECRET", "HUBSPOT_TOKEN",
    ):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    c = Config(user_name="Alex", data_dir=str(tmp_path / "data"))
    c.brain.provider = "heuristic"
    c.agents.mode = "off"
    c.always_on.idle_split_minutes = 0
    return c


@pytest.fixture
def meeting() -> Meeting:
    return Meeting(title="Weekly sync")
