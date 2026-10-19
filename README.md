# voicestream

**An always-on executive assistant for your meetings.** It listens to your microphone *and* your
computer's audio (so it hears everyone on the call), transcribes everything live in French or
English, and has Claude turn it into action items, decisions and notes.

- Your tasks become **Google Calendar** reminders, **Apple Reminders** and **HubSpot** tasks.
- The notes become a **Google Doc** and a recap in **Gmail**, plus a ready-to-send follow-up
  draft for the other attendees.
- Anything else on your Mac (an app like Promethee) is reachable through **Shortcuts**, a URL
  scheme or a command.
- When someone says "il nous faut une analyse concurrentielle d'ici vendredi", a Claude agent
  can start on it while you're still in the meeting.

```
 your mic ──┐                                        ┌─ live extraction (Claude Haiku) ─┬─ Google Calendar · Apple Reminders · HubSpot · Shortcuts/apps
            ├─ VAD ─ speech-to-text ─ echo filter ───┤   (French/English, auto)         ├─ Google Docs · Gmail recap + follow-up draft
 system  ───┘   (only speech is sent)                │                                  └─ agents that do the work (Claude, sandboxed on your Mac)
 audio                                               └─ transcript.md · notes.md · TODO.md (local, always)
```

It costs **about $0.80 per day for 8 hours of meetings** with speech-to-text on your Mac (free,
the app's default): about $0.15 for live extraction and $0.60 for the wrap-up notes. Cloud
speech-to-text adds about $1.40 a day (OpenAI). `voicestream cost` shows the estimate for your
settings. Agent work is extra and has its own budget.

---

## Contents

- [Install the app (Mac)](#install-the-app-mac) · [Installer l'application](#installer-lapplication-mac)
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

## Install the app (Mac)

For a Mac with Apple Silicon (M1 or later) and macOS 14.2 or later. No terminal needed.

1. Download `Voicestream-<version>-arm64.dmg`:
   - a released version: from the [Releases page](https://github.com/bybysker/always-on-voice-stream/releases),
     under **Assets** (no GitHub account needed);
   - the latest build, if it is newer: sign in to GitHub, open the
     [mac app workflow](https://github.com/bybysker/always-on-voice-stream/actions/workflows/mac-app.yml?query=is%3Asuccess),
     click the newest run (green check mark) and, under **Artifacts**, download
     `Voicestream-…-arm64.dmg`. GitHub keeps these builds for a limited time (90 days by default).
2. Open it and drag **Voicestream** onto **Applications**. Then open Voicestream from your
   Applications folder.
3. macOS says it can't verify the app, because it isn't notarized by Apple. Close that message
   (don't choose Move to Trash), open **System Settings → Privacy & Security**, scroll down to
   "Voicestream was blocked…", click **Open Anyway**, enter your password and click **Open**.
   This is needed once for each version you download. Since macOS Sequoia, Control-click → Open
   no longer skips it.
4. Follow the setup: your name and languages, speech-to-text ("On this Mac" is free and private;
   it downloads a 547 MB model), your Claude key (saved in the macOS Keychain), then click the two
   permission tests so macOS asks for **Microphone** and **System Audio Recording**. Allow both.
   If you keep **Apple Reminders** on, macOS then asks whether Voicestream may control Reminders:
   click **OK**, or your to-dos can't reach Reminders.
5. Voicestream now lives in the menu bar (the microphone icon). Closing the window doesn't stop
   it listening: click the icon → **Open Voicestream** to bring the window back, or **Quit
   Voicestream** to stop. **Open at login** in the same menu starts it with your Mac.
6. Press **⌥⌘R** (Option-Command-R) in any app, window open or not, to start or stop listening; a
   short sound says it worked. Stopping this way wraps the meeting up and writes its notes, like
   **Stop listening**. Change the shortcut, turn it off or mute the sound in **Settings → General →
   Keyboard shortcut** (it needs no extra permission); the menu-bar menu shows it next to **Start
   listening**. In Safari ⌥⌘R also reloads a page: pick another shortcut if you use that.

**After an update**, install the new DMG the same way (step 3 again). macOS ties permissions to
each build of an app that isn't notarized, so they may have to be granted again, even when System
Settings still shows them as on. In **Privacy & Security → Microphone** and **Privacy & Security →
Screen & System Audio Recording**, remove Voicestream with the **−** button (or switch it off and
on again), open Voicestream and allow it when asked. The Keychain may also ask once whether the new
version may read your saved keys: click **Always Allow**.

To build the app yourself on a Mac: `packaging/mac/build.sh` (the comments at its top list what to
install first). To publish a release, push a `vX.Y.Z` tag that matches the version in
`pyproject.toml`, or open **Actions → mac app → Run workflow** and type that tag: the build is
tested, then attached to a new release with the notes in `packaging/mac/release-notes.md`.

### Installer l'application (Mac)

Pour un Mac Apple Silicon (M1 ou plus récent) sous macOS 14.2 ou plus. Aucun terminal nécessaire.

1. Téléchargez `Voicestream-<version>-arm64.dmg` :
   - une version publiée : sur la [page Releases](https://github.com/bybysker/always-on-voice-stream/releases),
     sous **Assets** (aucun compte GitHub nécessaire) ;
   - la dernière construction, si elle est plus récente : connectez-vous à GitHub, ouvrez le
     [workflow mac app](https://github.com/bybysker/always-on-voice-stream/actions/workflows/mac-app.yml?query=is%3Asuccess),
     cliquez sur l'exécution la plus récente (coche verte) puis, sous **Artifacts**, téléchargez
     `Voicestream-…-arm64.dmg`. GitHub ne garde ces fichiers qu'un temps (90 jours par défaut).
2. Ouvrez-le et faites glisser **Voicestream** sur **Applications**, puis ouvrez Voicestream
   depuis votre dossier Applications.
3. macOS indique qu'il ne peut pas vérifier l'app, car elle n'est pas notariée par Apple. Fermez ce
   message (sans choisir « Placer dans la corbeille »), ouvrez **Réglages Système →
   Confidentialité et sécurité**, descendez jusqu'à « L'ouverture de Voicestream a été
   bloquée… », cliquez sur **Ouvrir quand même**, saisissez votre mot de passe puis cliquez sur
   **Ouvrir**. À refaire une fois pour chaque version téléchargée. Depuis macOS Sequoia, le
   Contrôle-clic → Ouvrir ne suffit plus.
4. Suivez l'assistant : votre nom et vos langues, la transcription (« Sur ce Mac » est gratuit et
   privé ; un modèle de 547 Mo est téléchargé), votre clé Claude (enregistrée dans le trousseau de
   macOS), puis cliquez sur les deux tests d'autorisation pour que macOS demande l'accès au
   **Micro** et à l'**Enregistrement audio du système**. Autorisez les deux. Si vous gardez
   **Rappels Apple**, macOS demande ensuite si Voicestream peut contrôler Rappels : cliquez sur
   **OK**, sinon vos tâches ne pourront pas y arriver.
5. Voicestream vit ensuite dans la barre des menus (l'icône de micro). Fermer la fenêtre ne coupe
   pas l'écoute : cliquez sur l'icône → **Ouvrir Voicestream** pour retrouver la fenêtre, ou
   **Quitter Voicestream** pour arrêter. **Ouvrir à la connexion**, dans le même menu, le lance
   avec votre Mac.
6. Appuyez sur **⌥⌘R** (Option-Commande-R) dans n'importe quelle app, fenêtre ouverte ou non, pour
   démarrer ou arrêter l'écoute ; un petit son le confirme. Arrêter ainsi clôture la réunion et
   rédige ses notes, comme **Arrêter l'écoute**. Modifiez le raccourci, désactivez-le ou coupez le
   son dans **Réglages → Général → Raccourci clavier** (aucune autorisation supplémentaire) ; le
   menu de la barre des menus l'affiche à côté de **Démarrer l'écoute**. Dans Safari, ⌥⌘R recharge
   aussi la page : choisissez un autre raccourci si vous vous en servez.

**Après une mise à jour**, installez le nouveau DMG de la même façon (étape 3 à refaire). macOS
associe les autorisations à chaque version d'une app non notariée : il faut parfois les accorder à
nouveau, même si Réglages Système les affiche encore comme activées. Dans **Confidentialité et
sécurité → Micro** et **Confidentialité et sécurité → Enregistrement de l'écran et de l'audio du
système**, retirez Voicestream avec le bouton **−** (ou désactivez-le puis réactivez-le), rouvrez
Voicestream et autorisez-le quand il le demande. Le trousseau peut aussi demander une fois si la
nouvelle version peut lire vos clés enregistrées : cliquez sur **Toujours autoriser**.

## Quick start

These steps assume a Mac with macOS 14.2 or later.

```bash
brew install uv ffmpeg
uv tool install --python 3.12 "voicestream[all] @ git+https://github.com/bybysker/always-on-voice-stream"
#   or, from a clone:  uv venv --python 3.12 && uv pip install -e ".[all]" && source .venv/bin/activate

voicestream init            # writes ~/.voicestream/voicestream.toml and ~/.voicestream/.env
open -e ~/.voicestream/.env # ANTHROPIC_API_KEY=…  OPENAI_API_KEY=…
voicestream doctor          # checks keys, microphone, system audio, your tools, budget
voicestream run             # start listening (Ctrl+C to stop)
```

Homebrew's Python refuses a plain `pip install` (it is "externally managed"), which is why the
install goes through `uv tool` (or `pipx`), or a virtual environment for a clone. Either way the
`voicestream` command is the only thing you need afterwards.

In `~/.voicestream/voicestream.toml`, set `user_name` to your own name, ideally as your meeting
tools show it (`"Alex Martin"`). The assistant then knows that "je m'en occupe" in your microphone
means *you* own that task. Then switch on the
tools you want under [Sending items to your tools](#sending-items-to-your-tools). The
[example for a Mac + Google + HubSpot setup](#example-mac--google--hubspot--reminders) is a
good start.

The first run asks for three macOS permissions. Grant them to the terminal app you run
voicestream from:

| Permission | Why | Where to fix it if you said no |
|---|---|---|
| **Microphone** | Your voice | System Settings → Privacy & Security → Microphone |
| **System Audio Recording** | The other participants | Privacy & Security → Screen & System Audio Recording. Restart the terminal afterwards. |
| **Automation → Reminders** | Only if you enable Apple Reminders | Privacy & Security → Automation |

The terminal shows the live transcript and every item as it's captured. A dashboard URL is
printed too (`http://127.0.0.1:8765/?token=…`). The dashboard is where you approve agents and
push or edit items.

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
| **macOS 14.2+** | A Core Audio tap of the system output (`catap`, installed with `[audio]`) | Grant **System Audio Recording** to your terminal on the first run, then restart the terminal. |
| macOS before 14.2 | A virtual loopback device | Run `brew install blackhole-2ch`. In Audio MIDI Setup, create a Multi-Output Device with your speakers first, then BlackHole, and select it as the output. Set `capture.system.device = "BlackHole 2ch"`. |
| **Windows** | WASAPI loopback of your default output (`soundcard`) | Nothing. Install with `[audio]`. |
| **Linux** | PulseAudio/PipeWire monitor of your output (`soundcard`, or ffmpeg) | Nothing in most cases. Otherwise set `capture.system.method = "ffmpeg"`. |

If a permission is missing, macOS hands over silence instead of an error. voicestream notices a
microphone that only ever delivers digital silence and tells you to check the permission. It also
warns when system audio has stayed digitally silent for two minutes while you talked: on a call,
that usually means the System Audio Recording permission is missing, and until it comes through
your mic's copy of the others counts as you, so "Hey Claude" commands wait for your OK.
`voicestream devices` lists every input and prints the instructions for your OS.

Wear headphones if you can. The echo filter handles speaker bleed, but clean channels give
better transcripts.

### Languages

`brain.language = "auto"` (the default) writes titles, notes and emails in the language each
meeting is held in, so mixed French/English days just work. Set `"fr"` or `"en"` to force one
language. The transcriber detects the language itself; set `transcription.language` only if
every meeting is in the same language.

Deadlines are understood in both languages:

- "d'ici vendredi"
- "fin de la semaine prochaine"
- "dans quinze jours"
- "le 1er décembre"
- "by Friday"
- "end of next week"

Say "**Dis Claude, …**" or "**Hey Claude, …**" to talk to the assistant directly.

## Speech-to-text backends

| Backend | `transcription.backend` | Price | Notes |
|---|---|---|---|
| whisper.cpp, on your Mac | `whisper-cpp` (the app's choice) | free | `whisper-server` on the Metal GPU; nothing leaves the Mac. Get a model in the app's setup or with `voicestream models download` (547 MB, `large-v3-turbo-q5_0`). French and English are detected; `allowed_languages` keeps a short clip from coming out as another language. |
| OpenAI REST | `openai` (config default) | $0.0045/min (`gpt-transcribe`) | One request per utterance. Stateless and robust, with about 1–2 s latency. |
| OpenAI Realtime | `openai-realtime` | $0.0045/min, or $0.017 with `gpt-live-transcribe` | Streams while you speak, with live captions. The socket rotates before the 60-minute cap, and anything lost on a reconnect is re-sent. |
| Deepgram | `deepgram` | about $0.0092/min (`nova-3`, multilingual) | Streaming; with `language` unset it follows French and English in the same meeting (`language=multi`). Your `vocabulary` list becomes key terms. |
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
| `manual` | Push only from the dashboard, or with `voicestream push last` (the newest finished meeting). |

Items that were already pushed stay in sync. Later edits, completion and dismissal are propagated
where the tool's API allows, also after the meeting has ended. A push that fails for a passing
reason (the network still coming back after the Mac wakes up) is retried a few times over the next
minute.

### Your tools

| Sink | What happens | Setup |
|---|---|---|
| `google` | Your tasks become Google Calendar reminders at 09:00 on their due date. Each meeting gets a Google Doc in a "Meeting notes" folder. Gmail sends a recap to you and saves a follow-up **draft** for the attendees; drafts are never sent. | [Google bridge](#google-calendar-docs-and-gmail) |
| `reminders` | Your tasks go into an Apple Reminders list ("Meetings"), with due date and alert. | `enabled = true`, then allow Automation once. |
| `hubspot` | The meeting is logged on the contacts and companies that took part in it. Your follow-ups become HubSpot tasks. | [HubSpot key](#hubspot) |
| `shortcuts` | Runs a macOS Shortcut per task or meeting, with the data as JSON. Use it for any app with Shortcuts actions, such as Promethee. | [Apps on your Mac](#promethee-and-other-apps-on-your-mac) |
| `command` | Runs a command or opens a URL scheme per task, e.g. `open "promethee://add?title={title_url}"`. | [Apps on your Mac](#promethee-and-other-apps-on-your-mac) |
| `markdown` | Keeps `~/.voicestream/TODO.md` and `notes/` locally. Works with Obsidian or any editor. | On by default |

Also available, off unless you configure them: `webhook` (Zapier, Make, n8n or any local app),
`slack`, `notion`, `monday`, `linear`, `github` and `todoist`.

### Google Calendar, Docs and Gmail

Google is reached through a small Apps Script "bridge" that runs as you on your own account. You
paste it once. There is no Google Cloud project, no OAuth client and no token that expires.

1. Go to <https://script.google.com>, click **New project** and name it "voicestream bridge".
2. Copy the script to your clipboard, then select everything in the editor, paste, and save
   (⌘S):

   ```bash
   voicestream google-bridge | pbcopy
   ```

3. Make a secret:

   ```bash
   voicestream google-bridge --secret
   ```

4. In the editor, open **Project Settings** (⚙) and find **Script Properties**. Click **Add
   script property**, enter `SECRET` with the secret as its value, then **Save script
   properties**.
5. Click **Deploy** → **New deployment**, then **Select type** (⚙) → **Web app**. Set
   **Execute as: Me** and **Who has access: Anyone**, then click **Deploy**.
6. Click **Authorize access** and pick your account. On the warning screen, click **Advanced**
   → **Go to voicestream bridge (unsafe)** → **Allow**. The warning appears because it's your own
   unpublished script.
7. Add the Web app URL (it ends in `/exec`) and the secret to `~/.voicestream/.env`:

   ```
   VOICESTREAM_GOOGLE_URL=https://script.google.com/macros/s/…/exec
   VOICESTREAM_GOOGLE_SECRET=…
   ```

8. Enable `[sinks.google]` (see the example below) and run `voicestream doctor`. It pings the
   bridge and shows your account.

How the bridge stays safe:

- The secret travels only to `script.google.com`.
- The bridge only edits calendar events and Docs it created itself.
- The recap goes only to your own address.
- Follow-ups are drafts.

To update the bridge later, paste the new code (`voicestream google-bridge | pbcopy`), then use
**Deploy** → **Manage deployments** →
✎ → **New version** → **Deploy**. The URL stays the same.

### HubSpot

1. In HubSpot, create a key: **Settings** → **Integrations** → **Service keys** (or a private
   app).
2. Give it these scopes:
   - `crm.objects.contacts.write`
   - `crm.objects.contacts.read`
   - `crm.objects.companies.read`
   - `crm.objects.companies.write`
   - `crm.objects.owners.read`
3. Add `HUBSPOT_TOKEN=…` to `~/.voicestream/.env`.
4. Set `owner_email` in `[sinks.hubspot]` to your HubSpot login, so tasks are assigned to you.

Participants are matched to HubSpot contacts and companies by name, accents ignored. Only people
who took part count, not people who were merely talked about. A record is associated only when the
match is unique, and contacts are never created.

### Promethee and other apps on your Mac

First find out what the app exposes:

| Check | Command or place |
|---|---|
| Shortcuts actions | Search for the app's name in the Shortcuts action search |
| A URL scheme | `plutil -p /Applications/Promethee.app/Contents/Info.plist \| grep -A4 CFBundleURLSchemes` |
| AppleScript support | `sdef /Applications/Promethee.app` |

**With Shortcuts** (recommended):

1. Create a shortcut, e.g. "Promethee – nouvelle tâche".
2. Add these actions:
   - *Get Dictionary from Shortcut Input*
   - *Get Dictionary Value* `item`, then `title` (also available: `detail`, `owner`, `due_date`)
   - the Promethee action
   - optionally *Stop and Output* the new object's id
3. Test it:

   ```bash
   shortcuts run "Promethee – nouvelle tâche" --input-path sample.json
   ```

4. Set `item_shortcut` in `[sinks.shortcuts]`.

**With a URL scheme or a command-line tool**, use `[sinks.command]`. Placeholders such as
`{title}`, `{title_url}`, `{due}`, `{owner}`, `{detail}` and `{json_path}` are filled in per
argument, and no shell is ever used, so meeting text can't inject commands.

### Example: Mac + Google + HubSpot + Reminders

```toml
user_name = "Alex Martin"          # your name as Zoom/Meet/Teams show it ("Alex" works for live meetings too)
timezone = "Europe/Amsterdam"

[brain]
language = "auto"                  # French or English, per meeting
attendees = ["Sarah", "Sam"]       # people you meet often (helps spelling and owners)

[agents]
mode = "commands"                  # "Dis Claude, …" runs immediately; other tasks wait for your OK

[sinks.google]
enabled = true                     # Calendar reminders for your tasks + Doc + Gmail recap/draft

[sinks.reminders]
enabled = true
list_name = "Réunions"

[sinks.hubspot]
enabled = true
owner_email = "you@example.com"    # your HubSpot login: tasks get assigned to you

[sinks.shortcuts]
enabled = true
item_shortcut = "Promethee – nouvelle tâche"

[sinks.markdown]
enabled = true
```

By default `google`, `reminders` and `hubspot` only take items **you** own (`owners = "mine"`).
Your calendar and CRM stay yours; the other participants' tasks are listed in the notes and the
follow-up draft. Google Calendar and Apple Reminders both remind you of your tasks, so keep one:
set `calendar = false` under `[sinks.google]` if you prefer Reminders.

On first use, check these on your Mac:

- From a clone with the `dev` extra, run
  `VOICESTREAM_TEST_REMINDERS=1 .venv/bin/python -m pytest tests/test_sink_mac.py -k real`. It creates,
  completes and deletes one test reminder.
- Make sure your Promethee shortcut doesn't ask questions. A shortcut waiting for input is
  stopped after `timeout_s` and not retried.

### Claude as a destination

Tasks an AI can do now get a **Run agent** button in the dashboard, or `run A3` in the
terminal. Examples: research, a competitive analysis, a draft email or proposal, a slide outline,
a script. The agent runs Claude on your Mac in a private folder and reports back into the notes,
usually before the meeting ends. See the next section.

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

- Wake-phrase commands count only from **your** microphone (`brain.commands_from`), and only a line
  in which you say the wake phrase yourself: answering "sure" to someone else's "Hey Claude…" is
  not a command. Other participants cannot trigger agents. While system audio is silent (see
  [Capturing system audio](#capturing-system-audio-what-the-others-say)), commands wait for your OK.
- Agents run in `dontAsk` permission mode with `--restricted`. File tools are confined to the
  task's workspace and edits are allowed only under `./`.
- No Bash unless you enable it, and then only inside a sandbox that must be available
  (`failIfUnavailable`).
- No user or project settings and no MCP servers are loaded.
- Your other tools' API tokens are scrubbed from the agent's environment, and agents may not read
  voicestream's `.env` files. What an agent leaves in its folder is only picked up if it is a real
  file inside that folder, never a link to something outside it.
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

You can also add items by hand or end the meeting from it. Between meetings it shows the last one,
so you can still edit, push or dismiss its items after it ended.

`voicestream app` runs the [Mac app](#install-the-app-mac) from a terminal: its window and
menu-bar icon on a Mac (with the `mac` extra installed), your browser elsewhere. `--browser` opens
it in the browser on a Mac too; `--no-open` only serves it and prints its address. Ctrl+C stops
it, wrapping up a meeting in progress first (press it twice to stop at once).

In the terminal, type `help`, or one of these:

| Command | What it does |
|---|---|
| `run A3` | Approve and run the agent for item A3 |
| `reject A3` | Decline the agent for A3 |
| `dismiss A2` / `restore A2` | Hide a wrong item / undo |
| `push A1` | Push A1 to your sinks now |
| `add Call Sam by Friday` | Add an action item for you, due Friday (`add Send the deck @Sam` gives it to Sam) |
| `extract` | Run extraction now |
| `end` | End the meeting (summary and push) |
| `status` | Show current items and spend |
| `quit` | Stop listening |

## Recordings and existing transcripts

```bash
voicestream replay meeting.m4a                  # one file = you (any format ffmpeg reads)
voicestream replay me.wav others.wav            # two files = mic + system, with echo filtering
voicestream import zoom_transcript.vtt          # .vtt/.srt from Zoom/Meet/Teams, or "Name: text" .txt
voicestream import call.vtt --me "Alex Martin" --date 2026-10-12T14:00
voicestream sessions                            # list recorded meetings
voicestream summarize last                      # re-run the notes
voicestream push last --sink notion             # push a saved meeting
```

`replay` runs as fast as possible by default. Use `--speed 1` for real time, which also shows it
live on the dashboard with `--dashboard`; the dashboard then stays open after the end, to review,
edit and push, until you press Ctrl+C. Use `run --store-audio` to keep the raw audio of live
meetings for later replay.

`import` treats the lines labelled with your name as yours. A first name in `user_name` matches
your full display name ("Alex" → "Alex Martin") unless two speakers share it; `--me` gives the
label exactly. The meeting is dated from the file's time minus its length (meeting tools write the
transcript when the call ends), or from `--date`, so deadlines like "d'ici vendredi" are counted
from the meeting day.

`push last` takes the newest *finished* meeting. It refuses one that is still being recorded
(`--force` overrides), records each push as soon as it is made, so an interrupted push never
creates an item twice, and exits with status 1 when a push failed.

## Costs and budget

`voicestream cost` shows today's spend and an estimate for a typical day. Defaults for 8 hours
of meetings, with about 60% speech:

| Part | Model | Per day |
|---|---|---|
| Speech-to-text | on your Mac (`whisper-cpp`), or `gpt-transcribe` | free, or ≈ $1.40 |
| Live extraction | `claude-haiku-5-5`, cached, every ~90 s while people talk | ≈ $0.10–0.50 |
| Wrap-up and notes | `claude-sonnet-5-5`, once per meeting | ≈ $0.05–0.15 per meeting |
| Screen descriptions (optional) | `claude-haiku-5-5` | ≈ $0.05 |
| Agents (optional) | `claude-opus-5-5` | ≈ $0.02–0.30 per task, capped |

That is about $0.76 a day (≈ $15 a month over 20 working days) with speech-to-text on your Mac,
and about $2.20 with OpenAI's. `budget.daily_usd` (default $3) is a soft cap on transcription plus Claude. When it's reached,
extraction falls back to an offline rule-based extractor and screenshots stop. Spend is tracked
per day in `~/.voicestream/spend/`.

## Privacy and consent

- **Tell people you're recording.** Many jurisdictions require consent from everyone on a call
  before it is recorded or transcribed.
- Audio is never stored unless you pass `--store-audio`. Only speech segments are sent to your
  speech-to-text provider, and only text is sent to Claude. Screenshots are off by default and are
  described once, never stored unless `screen.save_images` is set.
- Transcripts, notes and items live in `~/.voicestream/` on your machine. voicestream keeps that
  folder readable by your account only, and `voicestream doctor` warns if it isn't.
- Use `transcription.backend = "local"` to keep audio fully offline.

## Configuration reference

`voicestream init` writes a commented `voicestream.toml`; see
[`src/voicestream/config.example.toml`](src/voicestream/config.example.toml). The config file is
looked up in this order:

1. `--config`
2. `$VOICESTREAM_CONFIG`
3. `./voicestream.toml`
4. `~/.voicestream/voicestream.toml`

Secrets are read only from the environment or a `.env` file: in the current directory, next to
the config file, in `data_dir` and in `~/.voicestream/` (the first one that sets a key wins). A
mistake in the config file is reported with the file and the setting, e.g.
`error: ~/.voicestream/voicestream.toml: brain.langage: Extra inputs are not permitted`.

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
| `budget.daily_usd` | `3.0` | Soft daily cap on transcription plus Claude |
| `budget.agents_daily_usd` | `5.0` | Daily cap on all agents together |
| `always_on.idle_split_minutes` | `10` | Silence that ends a meeting |
| `screen.enabled` | `false` | Describe shared slides when they change |
| `echo.enabled` | `true` | Drop the mic's copy of your speakers' audio |

## Development

```bash
uv venv --python 3.12 && uv pip install -e ".[dev,agents]"
.venv/bin/python -m pytest
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
