"""Configuration: a TOML file (``voicestream.toml``) plus environment variables.

Secrets (API keys, tokens) are only ever read from the environment or a
``.env`` file — never from the TOML — so the config file is safe to share.
Lookup order for the config file: ``--config`` flag, ``$VOICESTREAM_CONFIG``,
``./voicestream.toml``, ``~/.voicestream/voicestream.toml``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MicConfig(_Section):
    device: str | int | None = None
    """Device name substring or index. ``None`` = OS default input."""
    label: str = "Me"


class SystemAudioConfig(_Section):
    method: Literal["auto", "soundcard", "tap", "device", "ffmpeg"] = "auto"
    """How to capture what other participants say:

    * ``auto``      – soundcard loopback on Windows/Linux; tap (else a BlackHole device) on macOS.
    * ``soundcard`` – WASAPI loopback (Windows) / PulseAudio-PipeWire monitor (Linux).
    * ``tap``       – macOS 14.2+ Core Audio process tap (``catap``), no virtual device needed.
    * ``device``    – a named input device, e.g. ``BlackHole 2ch`` on macOS.
    * ``ffmpeg``    – an ffmpeg input spec in ``ffmpeg_input`` (works everywhere).
    """
    device: str | int | None = None
    ffmpeg_input: str | None = None
    """e.g. ``-f pulse -i default.monitor`` or ``-f avfoundation -i :BlackHole 2ch``."""
    label: str = "Others"


class CaptureConfig(_Section):
    sources: list[str] = Field(default_factory=lambda: ["mic", "system"])
    mic: MicConfig = Field(default_factory=MicConfig)
    system: SystemAudioConfig = Field(default_factory=SystemAudioConfig)
    store_audio: bool = False
    """Also save raw audio (WAV per source) in the session folder."""


class VADConfig(_Section):
    engine: Literal["energy", "webrtc"] = "energy"
    frame_ms: int = 30
    margin_db: float = 12.0
    """Speech must be this many dB above the adaptive noise floor."""
    min_threshold_db: float = -52.0
    """Absolute floor (dBFS) below which nothing counts as speech."""
    start_frames: int = 3
    hangover_s: float = 0.7
    """Silence needed to close an utterance."""
    pre_roll_s: float = 0.3
    min_speech_s: float = 0.35
    max_utterance_s: float = 25.0
    min_chunk_s: float = 1.2
    """Very short utterances are held and merged with the next one (better accuracy)."""
    merge_gap_s: float = 2.0
    webrtc_aggressiveness: int = 2


class OpenAITranscribeConfig(_Section):
    model: str = "gpt-transcribe"
    """``gpt-transcribe`` ($0.0045/min) is current; ``gpt-4o-mini-transcribe`` ($0.003/min)
    is cheaper but deprecated (OpenAI shutdown 2027-02-26)."""
    base_url: str = "https://api.openai.com/v1"
    prompt: str | None = None
    """Context hint, e.g. "Weekly product sync with Ashley, Sam and Sarah"."""
    timeout_s: float = 60.0


class OpenAIRealtimeConfig(_Section):
    model: str = "gpt-transcribe"
    """``gpt-transcribe`` gives a final transcript per utterance at $0.0045/min;
    ``gpt-live-transcribe`` adds word-by-word live captions at $0.017/min."""
    url: str = "wss://api.openai.com/v1/realtime?intent=transcription"
    noise_reduction: Literal["near_field", "far_field", "none"] = "near_field"
    """``near_field`` for a headset/laptop mic; ``far_field`` for a room mic; ``none`` for system audio."""
    delay: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    """Latency/accuracy trade-off for streaming models (``gpt-live-transcribe``)."""
    prompt: str | None = None
    rotate_after_s: float = 55 * 60
    """OpenAI caps realtime sessions at 60 minutes; reconnect between utterances before that."""


class DeepgramConfig(_Section):
    model: str = "nova-3"
    url: str = "wss://api.deepgram.com/v1/listen"
    endpointing_ms: int = 400
    smart_format: bool = True
    diarize: bool = False
    keyterms: list[str] = Field(default_factory=list)


class LocalWhisperConfig(_Section):
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1


class TranscriptionConfig(_Section):
    backend: Literal["openai", "openai-realtime", "deepgram", "local", "fake"] = "openai"
    language: str | None = None
    """ISO-639-1 hint, e.g. ``en``. ``None`` = auto-detect."""
    openai: OpenAITranscribeConfig = Field(default_factory=OpenAITranscribeConfig)
    openai_realtime: OpenAIRealtimeConfig = Field(default_factory=OpenAIRealtimeConfig)
    deepgram: DeepgramConfig = Field(default_factory=DeepgramConfig)
    local: LocalWhisperConfig = Field(default_factory=LocalWhisperConfig)


class EchoConfig(_Section):
    enabled: bool = True
    """Drop mic segments that are just the speakers bleeding into the mic."""
    similarity: float = 0.72
    window_s: float = 6.0


class BrainConfig(_Section):
    provider: Literal["anthropic", "heuristic"] = "anthropic"
    """``heuristic`` = offline rule-based extraction (no API key, lower quality)."""
    model: str = "claude-haiku-5-5"
    """Live extraction runs every minute or two all day, so it uses the cheapest
    current model (~$0.10-0.50/day). Raise to ``claude-sonnet-5-5`` for better recall."""
    effort: Literal["low", "medium", "high"] = "low"
    language: str = "auto"
    """Language for item titles, notes and e-mails: ``auto`` = the language the meeting is held in
    (mixed French/English meetings are fine), or an ISO code such as ``fr`` / ``en`` to force one."""
    summary_model: str = "claude-sonnet-5-5"
    """End-of-meeting consolidation + notes (a few calls per day)."""
    summary_effort: Literal["low", "medium", "high"] = "medium"
    extract_interval_s: float = 90.0
    min_new_chars: int = 160
    context_minutes: float = 6.0
    max_output_tokens: int = 4096
    trigger_phrases: list[str] = Field(
        default_factory=lambda: [
            "action item",
            "to do",
            "todo",
            "follow up",
            "follow-up",
            "can you",
            "could you",
            "will you",
            "i'll",
            "i will",
            "let's",
            "we decided",
            "decision",
            "deadline",
            "by friday",
            "by monday",
            "by tomorrow",
            "end of day",
            "eod",
            "next week",
            # French
            "tu peux",
            "pourrais-tu",
            "est-ce que tu",
            "vous pouvez",
            "je vais",
            "je m'en occupe",
            "on a décidé",
            "il faut",
            "d'ici",
            "avant vendredi",
            "à faire",
            "semaine prochaine",
            "demain",
        ]
    )
    """Hearing one of these brings the next extraction forward."""
    trigger_delay_s: float = 12.0
    """After a trigger phrase, wait this long (to hear the rest) then extract."""
    wake_phrases: list[str] = Field(
        default_factory=lambda: ["hey claude", "okay claude", "ok claude", "hey assistant", "dis claude", "hé claude"]
    )
    """Phrases that address the assistant directly ("Hey Claude, draft the ...")."""
    commands_from: list[str] = Field(default_factory=lambda: ["mic"])
    """Sources allowed to issue wake-phrase commands (other participants can't by default)."""
    attendees: list[str] = Field(default_factory=list)
    vocabulary: list[str] = Field(default_factory=list)
    """Names/jargon that help both the transcriber and the extractor."""


class ScreenConfig(_Section):
    enabled: bool = False
    interval_s: float = 20.0
    change_threshold: float = 0.06
    """Fraction of the (downscaled) screen that must change to count as new content."""
    monitor: int = 1
    max_width: int = 1280
    model: str = "claude-haiku-5-5"
    save_images: bool = False


class DispatchConfig(_Section):
    mode: Literal["end", "immediate", "manual"] = "end"
    """When action items are pushed to external sinks:

    * ``end``       – once, deduplicated, when the meeting ends (default).
    * ``immediate`` – as soon as an item is confident and has been stable for ``stable_s``.
    * ``manual``    – only when you click "push" in the dashboard / ``voicestream push``.
    """
    min_confidence: float = 0.6
    stable_s: float = 45.0
    """In ``immediate`` mode, wait until an item hasn't changed for this long (it often gets
    refined over the next minute: owner, deadline...)."""


class AgentsConfig(_Section):
    mode: Literal["off", "suggest", "commands", "auto"] = "suggest"
    """* ``off``      – never run agents.
    * ``suggest``  – mark delegable tasks; run when you approve them (dashboard / CLI).
    * ``commands`` – also auto-run tasks you explicitly asked for ("Hey Claude, ...").
    * ``auto``     – auto-run every delegable task (within budget).
    """
    engine: Literal["agent-sdk", "api"] = "agent-sdk"
    """``agent-sdk`` = full Claude agent (files, web search) in a sandboxed workspace;
    ``api`` = single Claude call that drafts the deliverable (no tools, cheapest)."""
    model: str = "claude-opus-5-5"
    max_concurrent: int = 2
    max_turns: int = 40
    max_budget_usd: float = 1.0
    timeout_s: float = 900.0
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
    )
    allow_bash: bool = False
    workspace_root: str | None = None
    """Where agent workspaces go. Default: ``<session>/agents/``."""
    finish_wait_s: float = 180.0
    """When a meeting ends, wait this long for running agents so their results make it
    into the notes (they keep running afterwards and the notes are updated when done)."""


class BudgetConfig(_Section):
    daily_usd: float = 3.0
    """Soft cap on Claude extraction/summary + transcription spend per day. When hit,
    extraction falls back to the offline heuristic and screenshots stop."""
    agents_daily_usd: float = 5.0


class DashboardConfig(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = False


class AlwaysOnConfig(_Section):
    idle_split_minutes: float = 10.0
    """End the current meeting (summarize + push) after this much silence; the next
    speech starts a new meeting. ``0`` disables auto-splitting."""


class ModelPrice(_Section):
    input: float
    output: float
    cache_read: float
    cache_write: float


class PricingConfig(_Section):
    """USD prices used by the cost meter (checked 2026-10). Override when they change."""

    stt_per_minute: dict[str, float] = Field(
        default_factory=lambda: {
            "gpt-transcribe": 0.0045,
            "gpt-live-transcribe": 0.017,
            "gpt-realtime-whisper": 0.017,
            "gpt-4o-mini-transcribe": 0.003,
            "gpt-4o-transcribe": 0.006,
            "whisper-1": 0.006,
            "nova-3": 0.0077,
            "local": 0.0,
            "fake": 0.0,
        }
    )
    llm_per_mtok: dict[str, ModelPrice] = Field(
        default_factory=lambda: {
            "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
            "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0, cache_read=0.10, cache_write=2.5),
            "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
            "claude-fable-5-1": ModelPrice(input=10.0, output=50.0, cache_read=0.25, cache_write=12.5),
        }
    )


class Config(_Section):
    user_name: str = "Me"
    """Your name, so "I'll do it" from the mic becomes an item owned by you."""
    data_dir: str = "~/.voicestream"
    timezone: str | None = None
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    echo: EchoConfig = Field(default_factory=EchoConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    always_on: AlwaysOnConfig = Field(default_factory=AlwaysOnConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    sinks: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {"markdown": {"enabled": True}}
    )
    """Per-sink settings, validated by each sink (see ``voicestream.sinks``)."""

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str | None) -> str | None:
        if v:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"unknown time zone {v!r} (use a name like 'Europe/Amsterdam')") from None
        return v

    @property
    def data_path(self) -> Path:
        # absolute, so paths handed to the dashboard and sinks don't depend on the working directory
        return Path(self.data_dir).expanduser().absolute()

    def source_label(self, source: str) -> str:
        if source == "mic":
            return self.capture.mic.label
        if source == "system":
            return self.capture.system.label
        return source.capitalize()


# ---------------------------------------------------------------------- loading


def candidate_paths(explicit: str | os.PathLike[str] | None = None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    if env := os.environ.get("VOICESTREAM_CONFIG"):
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "voicestream.toml")
    paths.append(Path("~/.voicestream/voicestream.toml").expanduser())
    return paths


class ConfigError(ValueError):
    """The config file can't be read or doesn't validate (the message names the file and the setting)."""


def load_config(path: str | os.PathLike[str] | None = None, *, load_env: bool = True) -> Config:
    """Load config from the first existing candidate path (or defaults)."""
    data: dict[str, Any] = {}
    found: Path | None = None
    for p in candidate_paths(path):
        if p.is_file():
            found = p
            try:
                with p.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{p}: {exc} (text values need quotes, e.g. list_name = \"Réunions\")") from None
            break
        if path and p == Path(path).expanduser():
            raise FileNotFoundError(f"config file not found: {p}")
    try:
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}" for e in exc.errors()[:5])
        raise ConfigError(f"{found or 'config'}: {problems}") from None
    if load_env:
        # never overrides what is already set, so the first file to define a key wins
        load_dotenv(Path.cwd() / ".env")
        if found is not None:
            load_dotenv(found.parent / ".env")  # where `voicestream init` puts it, next to the config
        load_dotenv(cfg.data_path / ".env")
        load_dotenv(Path("~/.voicestream/.env").expanduser())  # also when data_dir points elsewhere
    return cfg


def load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader (KEY=VALUE lines). Never overrides real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def secret(name: str, *, required: bool = False) -> str | None:
    value = os.environ.get(name) or None
    if required and not value:
        raise MissingSecretError(name)
    return value


class MissingSecretError(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"environment variable {name} is not set (add it to your shell or .env)")
        self.name = name
