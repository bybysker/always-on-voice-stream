# voicestream

**An always-on executive assistant for your meetings.** It listens to your microphone *and* your
computer's audio (so it hears everyone on the call), transcribes everything live, and has Claude
turn it into action items, decisions and notes. Those go straight into the tools you already use,
like Notion, Monday, Linear, GitHub, Todoist, Slack, or anything with a webhook. When someone says
"we need a competitive analysis by Friday", an agent can start on it while you're still in the
meeting.

```
 your mic ──┐                                       ┌─ live extraction (Claude Haiku) ─┬─ Notion / Monday / Linear / GitHub / Todoist / Slack / webhook
            ├─ VAD ─ speech-to-text ─ echo filter ──┤                                  ├─ agents that do the work (Claude Opus, sandboxed)
 system  ───┘   (only speech is sent)               └─ transcript.md · notes.md · TODO.md (local, always)
 audio
```

It costs **about $1–2 per day for 8 hours of meetings**: roughly $1.40 for speech-to-text and
$0.10–0.50 for live extraction, plus a few cents per meeting for summaries. Agent work is extra
and has its own budget.

---

## Contents

- [Quick start](#quick-start)
- [What happens during a meeting](#what-happens-during-a-meeting)
- [Capturing system audio](#capturing-system-audio-what-the-others-say)
- [Speech-to-text backends](#speech-to-text-backends)
- [Sending items to your tools](#sending-items-to-your-tools)
- [Agents that do the work mid-meeting](#agents-that-do-the-work-mid-meeting)
- [Dashboard and terminal commands](#dashboard-and-terminal-commands)
- [Recordings and existing transcripts](#recordings-and-existing-transcripts)
- [Costs and budget](#costs-and-budget)
- [Privacy and consent](#privacy-and-consent)
- [Configuration reference](#configuration-reference)
- [Development](#development)

## Quick start

```bash
pip install "voicestream[all] @ git+https://github.com/bybysker/always-on-voice-stream"
#   or, from a clone:  pip install -e ".[all]"
#   minimal install: pip install -e ".[audio,dashboard]"

voicestream init            # writes ~/.voicestream/voicestream.toml and ~/.voicestream/.env
$EDITOR ~/.voicestream/.env # ANTHROPIC_API_KEY=…  OPENAI_API_KEY=…
voicestream doctor          # checks keys, microphone, system audio, sinks, budget
voicestream run             # start listening (Ctrl+C to stop)
```

Set `user_name` in `voicestream.toml` to your own name. The assistant then knows that "I'll send
the deck" in your microphone means *you* own that task.

The terminal shows the live transcript and every item as it's captured. A dashboard URL is
printed too (`http://127.0.0.1:8765/?token=…`). The dashboard is where you approve agents and push
or edit items.

`voicestream run` is meant to stay on all day. After 10 minutes of silence
(`always_on.idle_split_minutes`) the current meeting is closed: final extraction, summary, push.
The next time someone speaks, a new meeting starts. Pass `--once` for a single meeting.

## What happens during a meeting

1. **Capture.** Your mic is captured as `Me` and the computer's audio as `Others`. Speaker
   attribution comes from the audio channel, so no diarization is needed.
2. **Voice activity detection.** An adaptive noise-floor VAD cuts the stream into utterances.
   **Only speech is uploaded**; silence never leaves your machine and is never billed.
3. **Speech-to-text.** Each utterance is transcribed by OpenAI `gpt-transcribe` (default), the
   OpenAI Realtime API, Deepgram, or a local Whisper model.
4. **Echo filter.** Without headphones, your mic also hears your speakers. Mic lines that just
   repeat what system audio said at the same moment are dropped, so other people's words aren't
   attributed to you.
5. **Live extraction.** About every 90 seconds of conversation, Claude Haiku 5.5 reads the new
   lines and returns only what changed: new or updated **action items** (owner, due date resolved
   to a real date, priority, verbatim evidence), **decisions**, **open questions** and **notes**.
   - Trigger phrases ("can you…", "by Friday", "action item") make it check sooner.
   - Saying "**Hey Claude, …**" into your own mic makes it react within seconds.
   - The transcript is sent as append-only blocks with prompt caching, so each call re-reads the
     earlier meeting at about a tenth of the price.
6. **Wrap-up.** When the meeting ends, Claude Sonnet 5.5 reads the whole transcript once. It
   merges duplicates, fixes owners and deadlines, catches anything missed, and writes the notes.
   Items and notes are then pushed to your sinks.

Each meeting gets a folder in `~/.voicestream/sessions/2026-10-14_153000-weekly-sync/` with these
files:

| File | Contents |
|---|---|
| `transcript.md` | Written live as people talk |
| `transcript.jsonl` | Same transcript, one JSON segment per line |
| `meeting.json` | Everything: items, screen notes, summary, push references |
| `notes.md` | The final meeting notes |
| `agents/<id>-…/` | Each agent's brief (`TASK.md`), deliverables and `RESULT.md` |

`~/.voicestream/TODO.md` is a running checklist across all meetings.

## Capturing system audio (what the others say)

| OS | How | Setup |
|---|---|---|
| **Windows** | WASAPI loopback of your default output (`soundcard`) | Nothing. Install with `[audio]`. |
| **Linux** | PulseAudio/PipeWire monitor of your output (`soundcard`, or ffmpeg) | Nothing in most cases. Otherwise set `capture.system.method = "ffmpeg"` with `ffmpeg_input = "-f pulse -i $(pactl get-default-sink).monitor"`. |
| **macOS** | A virtual loopback device | See the steps below. |

On macOS:

1. Run `brew install blackhole-2ch`.
2. In **Audio MIDI Setup**, click **+**, then **Create Multi-Output Device**. Tick your
   speakers or headphones **and** BlackHole.
3. Select the Multi-Output Device as your sound output.
4. Set `capture.system.device = "BlackHole 2ch"` in `voicestream.toml`.
5. Allow your terminal to use the microphone in **System Settings → Privacy & Security**.

`voicestream devices` lists every input, marks the ones that look like loopback devices, and
prints the instructions for your OS. ffmpeg works everywhere as a fallback:
`--source "ffmpeg:-f avfoundation -i :BlackHole 2ch@system"`.

Wear headphones if you can. The echo filter handles speaker bleed, but clean channels give better
transcripts.

## Speech-to-text backends

| Backend | `transcription.backend` | Price | Notes |
|---|---|---|---|
| OpenAI REST | `openai` (default) | $0.0045/min (`gpt-transcribe`) | One request per utterance. Stateless and robust, with about 1–2 s latency. |
| OpenAI Realtime | `openai-realtime` | $0.0045/min, or $0.017 with `gpt-live-transcribe` | Streams while you speak, with live captions. The socket rotates before the 60-minute cap, and anything lost on a reconnect is re-sent. |
| Deepgram | `deepgram` | about $0.0077/min (`nova-3`) | Streaming. Your `vocabulary` list becomes key terms. |
| Local Whisper | `local` | free | `faster-whisper` on your CPU or GPU. Nothing leaves the machine. |

OpenAI's `gpt-4o-mini-transcribe` is cheaper at $0.003/min, but it is deprecated and shuts down
on 2027-02-26. Put names and jargon in `brain.vocabulary`. They are passed to the transcriber as
keywords and to Claude as context.

## Sending items to your tools

Every sink has these filters:

| Filter | What it does |
|---|---|
| `kinds` | Which item kinds to send (`action`, `decision`, `question`, `note`) |
| `owners` | `"all"` or `"mine"` (only items owned by you) |
| `min_confidence` | Skip items below this confidence |
| `notes` | Also publish the meeting notes, where the sink supports it |

**When** items are pushed is set by `dispatch.mode`:

| Mode | Behavior |
|---|---|
| `end` (default) | Push once, deduplicated, when the meeting ends. |
| `immediate` | Push as soon as an item is confident and has stopped changing for `stable_s`. |
| `manual` | Push only from the dashboard, or with `voicestream push last`. |

Items that were already pushed stay in sync. Later edits, completion and dismissal are propagated
where the tool's API allows.

<!-- SINKS-TABLE -->

## Agents that do the work mid-meeting

When an action item is something an AI could do right now, the extractor marks it as delegable
and writes a self-contained brief. Examples: research, a competitive analysis, a draft email,
spec or proposal, a slide outline, a script or prototype.

What happens next depends on `agents.mode`:

| Mode | Behavior |
|---|---|
| `off` | Agents never run. |
| `suggest` (default) | The item shows **Run agent** in the dashboard (or type `run A3` in the terminal). You can edit the brief first. |
| `commands` | Requests you make yourself ("Hey Claude, pull together a comparison of Globex and Initech pricing") run automatically. Others are suggested. |
| `auto` | Every delegable task runs automatically, within budget. |

Results land in the dashboard, in the meeting notes under "Work done by agents during the
meeting", and in `agents/<id>-…/`. This often happens before the meeting is over.

**Engines:**

- `agent-sdk` (default) runs a full Claude agent through the
  [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python), with web search, web
  fetch and its own file workspace. Install it with `[agents]`.
- `api` makes a single Claude call that drafts the deliverable. It is cheapest and needs no extra
  package.

**Safety.** Meeting speech is untrusted input: anyone on the call can say anything. The
safeguards:

- Wake-phrase commands count only from **your** microphone (`brain.commands_from`). Other
  participants cannot trigger agents.
- Agents run in `dontAsk` permission mode with `--restricted`. File tools are confined to the
  task's workspace and edits are allowed only under `./`.
- No Bash unless you enable it, and then only inside a sandbox that must be available
  (`failIfUnavailable`).
- No user or project settings and no MCP servers are loaded.
- Your other tools' API tokens are scrubbed from the agent's environment.
- Each run is capped by turns, dollars and wall-clock time. All agents together are capped per
  day by `budget.agents_daily_usd`.
- Agents cannot email, schedule or message anyone. They prepare drafts and say what's left for
  you to do.

## Dashboard and terminal commands

The dashboard (`dashboard.enabled`, bound to 127.0.0.1 and protected by a per-run token) shows:

- the live transcript with captions
- input levels
- the item lists, with push, dismiss, edit and run-agent buttons
- agent progress and results
- today's spend against your budget

You can also add items by hand or end the meeting from it.

In the terminal, type `help`, or one of these:

| Command | What it does |
|---|---|
| `run A3` | Approve and run the agent for item A3 |
| `reject A3` | Decline the agent for A3 |
| `dismiss A2` / `restore A2` | Hide a wrong item / undo |
| `push A1` | Push A1 to your sinks now |
| `add Call Sam by Friday` | Add an action item by hand |
| `extract` | Run extraction now |
| `end` | End the meeting (summary and push) |
| `status` | Show current items and spend |
| `quit` | Stop listening |

## Recordings and existing transcripts

```bash
voicestream replay meeting.m4a                  # one file = you (any format ffmpeg reads)
voicestream replay me.wav others.wav            # two files = mic + system, with echo filtering
voicestream import zoom_transcript.vtt          # .vtt/.srt from Zoom/Meet/Teams, or "Name: text" .txt
voicestream sessions                            # list recorded meetings
voicestream summarize last                      # re-run the notes
voicestream push last --sink notion             # push a saved meeting
```

`replay` runs as fast as possible by default. Use `--speed 1` for real time, which also shows it
live on the dashboard with `--dashboard`. Use `run --store-audio` to keep the raw audio of live
meetings for later replay.

## Costs and budget

`voicestream cost` shows today's spend and an estimate for a typical day. Defaults for 8 hours
of meetings, with about 60% speech:

| Part | Model | Per day |
|---|---|---|
| Speech-to-text | `gpt-transcribe` | ≈ $1.40 |
| Live extraction | `claude-haiku-5-5`, cached, every ~90 s while people talk | ≈ $0.10–0.50 |
| Wrap-up and notes | `claude-sonnet-5-5`, once per meeting | ≈ $0.05–0.15 per meeting |
| Screen descriptions (optional) | `claude-haiku-5-5` | ≈ $0.05 |
| Agents (optional) | `claude-opus-5-5` | ≈ $0.02–0.30 per task, capped |

`budget.daily_usd` (default $2) is a soft cap on transcription plus Claude. When it's reached,
extraction falls back to an offline rule-based extractor and screenshots stop. Spend is tracked
per day in `~/.voicestream/spend/`.

## Privacy and consent

- **Tell people you're recording.** Many jurisdictions require consent from everyone on a call
  before it is recorded or transcribed.
- Audio is never stored unless you pass `--store-audio`. Only speech segments are sent to your
  speech-to-text provider, and only text is sent to Claude. Screenshots are off by default and are
  described once, never stored unless `screen.save_images` is set.
- Transcripts, notes and items live in `~/.voicestream/` on your machine.
- Use `transcription.backend = "local"` to keep audio fully offline.

## Configuration reference

`voicestream init` writes a commented `voicestream.toml`; see
[`src/voicestream/config.example.toml`](src/voicestream/config.example.toml). The config file is
looked up in this order:

1. `--config`
2. `$VOICESTREAM_CONFIG`
3. `./voicestream.toml`
4. `~/.voicestream/voicestream.toml`

Secrets are read only from the environment or a `.env` file in the current directory or
`~/.voicestream/`.

| Setting | Default | What it does |
|---|---|---|
| `user_name` | `"Me"` | Your name, used for ownership of your commitments |
| `capture.sources` | `["mic", "system"]` | Also accepts `file:<path>[@name]` and `ffmpeg:<args>[@name]` |
| `transcription.backend` | `openai` | See [Speech-to-text backends](#speech-to-text-backends) |
| `brain.model` / `brain.summary_model` | `claude-haiku-5-5` / `claude-sonnet-5-5` | Live extraction and wrap-up models |
| `brain.extract_interval_s` | `90` | Seconds between extraction calls |
| `brain.wake_phrases` | `["hey claude", …]` | Phrases that address the assistant directly |
| `brain.vocabulary` | `[]` | Names and jargon for the transcriber and Claude |
| `dispatch.mode` | `end` | When items are pushed |
| `agents.mode` | `suggest` | See [Agents](#agents-that-do-the-work-mid-meeting) |
| `agents.engine` | `agent-sdk` | Full agent, or `api` for a single call |
| `agents.model` | `claude-opus-5-5` | Model for agent tasks |
| `agents.max_budget_usd` | `1.0` | Cap per agent task |
| `budget.daily_usd` | `2.0` | Soft daily cap on transcription plus Claude |
| `budget.agents_daily_usd` | `5.0` | Daily cap on all agents together |
| `always_on.idle_split_minutes` | `10` | Silence that ends a meeting |
| `screen.enabled` | `false` | Describe shared slides when they change |
| `echo.enabled` | `true` | Drop the mic's copy of your speakers' audio |

## Development

```bash
pip install -e ".[dev,agents]"
pytest
```

Everything is tested offline. The tests use synthetic audio, mock HTTP transports, local
WebSocket servers standing in for the OpenAI Realtime and Deepgram protocols, and a scripted
stand-in for Claude.

Package layout under `src/voicestream/`:

| Path | Contents |
|---|---|
| `audio/` | Capture sources, the streaming resampler, VAD and the speech gate |
| `transcribe/` | Speech-to-text backends and the echo filter |
| `brain/` | Prompts, strict wire schemas, extractor, tracker, summarizer, offline heuristic |
| `sinks/` | Integrations |
| `agents/` | Agent runner |
| `dashboard/` | Web UI |
| `session.py` | The always-on orchestrator |
| `cli.py` | The `voicestream` command |
