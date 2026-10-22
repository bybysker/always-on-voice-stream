"""The app's UI with believable sample data, to look at every screen in a browser.

    python tests/ui_demo.py                  # prints the address of each screen; Ctrl+C stops it

It starts the same app host as the UI tests (``UIApp`` in tests/test_app_ui.py: the real HTTP API
and event stream, scripted speech, fake model downloads and permission checks) once for each state,
because one app can only be in one state at a time. Meetings started by hand (the default): between
meetings, in one, with a call detected; listening all the time (automatic): not listening, listening
between meetings, in a meeting; and a new install on its first-run setup. The apps that start
meetings by hand say they have what the Mac app has (its menu bar, the floating button, Spotlight,
call detection and the keyboard shortcut), so their screens show all of it. Everything lives in a
temporary folder that is removed on exit, with a temporary home too, so no real key, Keychain item or
meeting is touched.

The sample data holds together: the past meetings are imported from transcripts with the real
``voicestream import``, so their to-dos and decisions come from what was said; the spending on disk
is on the days those meetings happened; the meeting in progress began a few minutes ago and its lines
follow each other at the pace people speak.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))  # this checkout's UI, even when another one is installed
sys.path.insert(1, str(HERE))

_HOME = tempfile.TemporaryDirectory(prefix="voicestream-demo-home-")
os.environ["HOME"] = os.environ["USERPROFILE"] = _HOME.name  # never the real ~/.voicestream
os.environ["VOICESTREAM_SECRET_STORE"] = "file"  # never the real Keychain

# imported once the home is private, so nothing below can reach the real ~/.voicestream
import pytest
from test_app_ui import UIApp, _mac_features

from voicestream import stt_models

USER = "Camille Martin"

# (minutes:seconds into the meeting, speaker, words). "Camille" is you: the import turns her lines into
# the microphone's ("Me"), everyone else's into the meeting audio's ("Others").
PAST = [
    {
        "file": "point-produit.txt", "title": "Point hebdo produit", "ago": timedelta(hours=0), "at": "09:30",
        "tldr": "La version 2 sort le 4 novembre ; le prix reste à 29 € par mois et Sam prépare la démo pour Maison Berthe.",
        "participants": ["Camille Martin", "Sam Lefèvre", "Léa Dubois"],
        "pushed": {"reminders": ""},
        "lines": [
            ("00:04", "Camille", "Bonjour à tous, on fait le point sur la version 2 et sur la démo de la semaine prochaine."),
            ("00:19", "Sam", "J'ai fini la maquette de la page d'accueil, il ne reste que les textes."),
            ("00:41", "Léa", "Côté support, trois clients ont demandé l'export en PDF ce mois-ci."),
            ("01:20", "Camille", "Je vais écrire les textes de la page d'accueil d'ici vendredi."),
            ("02:05", "Sam", "Pour le prix, on a décidé de garder 29 euros par mois."),
            ("03:12", "Léa", "Il faudra prévenir les clients actuels avant la sortie."),
            ("04:30", "Camille", "Sam, tu peux préparer la démo pour Maison Berthe avant mardi ?"),
            ("04:48", "Sam", "Oui, je m'en occupe."),
            ("06:10", "Léa", "Je vais rédiger l'e-mail aux clients d'ici jeudi."),
            ("08:02", "Camille", "Parfait. On se revoit lundi pour la date de sortie."),
        ],
    },
    {
        "file": "hiring.txt", "title": "Hiring plan with Alex", "ago": timedelta(days=1), "at": "16:00",
        "tldr": "Two engineering roles open this quarter; Alex sends the job description to the recruiter by Monday.",
        "participants": ["Camille Martin", "Alex Chen"],
        "pushed": {"reminders": "", "markdown": ""},
        "lines": [
            ("00:03", "Camille", "Quick sync on the hiring plan for the quarter."),
            ("00:22", "Alex", "I'll send the job description to the recruiter by Monday."),
            ("01:05", "Camille", "We decided to open two engineering roles this quarter."),
            ("02:40", "Alex", "Can you review the interview questions before Thursday?"),
            ("02:52", "Camille", "Sure, I will review them tomorrow."),
            ("04:15", "Alex", "The budget covers one senior and one mid-level engineer."),
            ("05:30", "Camille", "Let's keep the take-home exercise under three hours."),
        ],
    },
    {
        "file": "budget.txt", "title": "Budget review", "ago": timedelta(days=3), "at": "11:00",
        "tldr": "The marketing budget stays at €18,000 for Q4; Camille sends the final figures to Sam by Friday.",
        "participants": ["Camille Martin", "Sam Lefèvre"],
        "pushed": {"reminders": "", "hubspot": "https://app.hubspot.com/tasks/24113071"},
        "email": {"subject": "Budget review: next steps", "to": ["sam.lefevre@maisonberthe.fr"],
                  "body": "Hi Sam,\n\nThanks for today. **Next steps:**\n\n- I send you the final figures by Friday\n"
                          "- You book the room for the November offsite\n\nCamille"},
        "lines": [
            ("00:05", "Camille", "Let's go through the Q4 budget line by line."),
            ("01:10", "Sam", "Events are €4,000 over because of the trade show in Lyon."),
            ("02:30", "Camille", "We decided to keep the marketing budget at 18,000 euros for Q4."),
            ("03:45", "Camille", "I'll send you the final figures by Friday."),
            ("04:20", "Sam", "Can you book the room for the November offsite?"),
            ("05:02", "Sam", "I will book the room this week."),
            ("06:40", "Camille", "Thanks Sam, that's everything for today."),
        ],
    },
    {
        "file": "atelier-lyon.txt", "title": "Atelier Lyon — préparation", "ago": timedelta(days=6), "at": "14:30",
        "tldr": "L'atelier a lieu le 12 novembre à Lyon ; Léa réserve la salle, Camille envoie le programme.",
        "participants": ["Camille Martin", "Léa Dubois"],
        "pushed": {"reminders": "", "markdown": ""},
        "lines": [
            ("00:06", "Léa", "On a décidé de faire l'atelier le 12 novembre à Lyon."),
            ("00:50", "Camille", "Je vais envoyer le programme aux participants d'ici lundi."),
            ("02:15", "Léa", "Je réserve la salle demain matin."),
            ("03:40", "Camille", "Léa, tu peux demander les devis du traiteur avant mercredi ?"),
            ("04:05", "Léa", "D'accord."),
        ],
    },
    {
        "file": "customer-call.txt", "title": "Customer call — Maison Berthe", "ago": timedelta(days=9), "at": "10:00",
        "tldr": "Maison Berthe wants PDF exports before renewing; a follow-up call is set for next week.",
        "participants": ["Camille Martin", "Hélène Berthe"],
        "pushed": {"hubspot": "https://app.hubspot.com/contacts/meetings/91877"},
        "lines": [
            ("00:04", "Camille", "Thanks for making the time, Hélène."),
            ("00:35", "Hélène", "Our team really needs PDF exports before we renew."),
            ("02:10", "Camille", "I'll send you the roadmap for exports by Wednesday."),
            ("03:30", "Hélène", "Can we talk again next week?"),
            ("03:41", "Camille", "Yes, I will send an invite for Tuesday."),
        ],
    },
    {
        "file": "kickoff.txt", "title": "Lancement du nouveau site", "ago": timedelta(days=13), "at": "15:00",
        "tldr": None, "participants": ["Camille Martin", "Sam Lefèvre", "Léa Dubois"], "pushed": {},
        "lines": [
            ("00:05", "Sam", "Le nouveau site doit être prêt pour la sortie de la version 2."),
            ("01:20", "Camille", "On a décidé de garder la même agence pour le design."),
            ("02:45", "Léa", "Je vais lister les pages à traduire d'ici la fin de semaine."),
        ],
    },
    {
        "file": "planning.txt", "title": "Quarterly planning", "ago": timedelta(days=20), "at": "09:00",
        "tldr": "Three goals for Q4: version 2, the Lyon workshop and two hires.",
        "participants": ["Camille Martin", "Alex Chen", "Sam Lefèvre"], "pushed": {"reminders": "", "markdown": ""},
        "lines": [
            ("00:10", "Camille", "Let's agree on three goals for the quarter."),
            ("01:40", "Alex", "We decided to ship version 2 before the end of November."),
            ("03:05", "Sam", "I'll draft the launch plan by next Friday."),
            ("04:30", "Camille", "I will write the goals down and share them tomorrow."),
        ],
    },
]

# What the cost meter wrote on those days, in US dollars: Claude's notes on every meeting day, the
# speech-to-text of the two meetings transcribed online before the switch to this Mac, and the one
# agent run (the roadmap research after the customer call). Today's figure is this morning's meeting.
SPEND = {0: (0.0, 0.18, 0.0), 1: (0.0, 0.21, 0.0), 3: (0.0, 0.34, 0.0), 6: (0.0, 0.16, 0.0),
         9: (0.05, 0.42, 1.10), 13: (0.04, 0.09, 0.0), 20: (0.0, 0.27, 0.0)}

LIVE = [  # the meeting in progress, in order: (source, words); each line follows the one before
    ("mic", "Bonjour à tous, merci d'être là. On fait le point sur le lancement de la version 2, puis sur la démo de mardi."),
    ("system", "Bonjour Camille. De notre côté, la maquette est validée par toute l'équipe, on a juste deux retours sur la navigation."),
    ("system", "Il nous manque encore les textes de la page d'accueil et la page de tarifs, c'est ce qui bloque la mise en ligne."),
    ("mic", "D'accord. Les textes, c'est pour moi. Je vais envoyer les textes à Sam d'ici vendredi."),
    ("system", "Parfait, ça nous laisse le week-end pour l'intégration et une relecture lundi matin."),
    ("mic", "Côté prix, on en a parlé avec Léa la semaine dernière, mais je préfère qu'on le confirme ici."),
    ("system", "We looked at the numbers again with the sales team, and nobody wants a price change right before the launch."),
    ("system", "We decided to keep the price at 29 euros a month."),
    ("mic", "Très bien, ça simplifie la communication. Et pour la démo client, on la fait toujours mardi ?"),
    ("system", "Mardi, c'est bon pour nous. Sam prépare les données de test, avec de vrais exemples de réunions anonymisées."),
    ("mic", "Sam, tu peux préparer la démo pour Maison Berthe avant mardi ?"),
    ("system", "Oui, et je vais aussi mettre à jour la page de tarifs avec le nouveau tableau comparatif."),
    ("system", "Une question sur l'export en PDF : est-ce qu'on le montre pendant la démo, ou on attend qu'il soit fini ?"),
    ("mic", "On le montre, même en version bêta. C'est la première chose que Maison Berthe a demandée."),
    ("mic", "Hey Claude, compare the export features of Pennylane and Tiime for the roadmap."),
    ("system", "Il faudra aussi prévenir les clients actuels du changement de version, sinon le support va crouler sous les questions."),
    ("mic", "Bonne remarque. Je vais rédiger l'e-mail aux clients d'ici jeudi, et je vous le fais relire."),
    ("system", "Pour le support, Léa propose une page d'aide avec les dix questions les plus fréquentes."),
    ("mic", "Oui, très bien. Ça peut reprendre les réponses qu'on a déjà dans les tickets."),
    ("system", "Can we also move the launch review to next Monday? Thursday is too close to the release."),
    ("mic", "Yes, Monday works. I'll send the invite this afternoon."),
    ("system", "One last thing: the Lyon workshop agenda still has two empty slots in the afternoon."),
    ("mic", "Let's keep one for customer questions and decide on the other one on Monday."),
    ("system", "Ça me va. On fait un point rapide sur les risques avant de terminer ?"),
    ("mic", "Oui. Le seul vrai risque, c'est la traduction anglaise de la page d'accueil. Le reste est prêt."),
    ("system", "Alors on garde un œil dessus, et on se revoit lundi pour la revue de lancement."),
]
PAUSES = (1.1, 0.6, 2.3, 0.9, 1.7, 3.2)  # the silence between two lines, in seconds, in turn


def live_timeline() -> tuple[list[tuple[float, float, str, str]], float]:
    """The meeting's lines at the pace people speak (about 2.6 words a second), and how long ago it began."""
    out, t = [], 3.0
    for i, (source, text) in enumerate(LIVE):
        dur = max(1.5, len(text.split()) / 2.6)
        out.append((t, t + dur, source, text))
        t += dur + PAUSES[i % len(PAUSES)]
    return out, t + 20.0  # the room has been quiet for 20 seconds since the last line


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class DemoApp(UIApp):
    """A UIApp whose past meetings and spending are the sample data above.

    The meetings are imported offline with the test settings (Claude off, only the files on this Mac as
    a tool: nothing is sent anywhere); ``provider`` and ``extra`` are the settings the app then opens with.
    """

    def __init__(self, root: Path, mp: pytest.MonkeyPatch, *, provider: str, extra: str, **kw: Any) -> None:
        self.final = (provider, extra)
        super().__init__(root, mp, provider="heuristic", **kw)

    def import_meetings(self) -> None:
        from voicestream.cli import main
        from voicestream.models import FollowUpEmail
        from voicestream.storage import atomic_write, list_sessions, load_meeting

        today = datetime.now().replace(second=0, microsecond=0)
        for spec in PAST:
            hour, minute = map(int, spec["at"].split(":"))
            when = (today - spec["ago"]).replace(hour=hour, minute=minute)
            if when > today:  # this morning's meeting, opened before 09:30: it was yesterday's then
                when -= timedelta(days=1)
            text = "".join(f"[{t}] {who}: {words}\n" for t, who, words in spec["lines"])
            src = write(self.root / "imports" / spec["file"], text)
            args = ["-c", str(self.config_path), "import", str(src), "--me", "Camille", "--date",
                    when.strftime("%Y-%m-%dT%H:%M"), "--title", spec["title"]]
            done: list[int] = []
            worker = threading.Thread(target=lambda a=args, d=done: d.append(main(a)))  # it runs asyncio.run()
            with contextlib.redirect_stdout(io.StringIO()):  # the import's own report of the meeting
                worker.start()
                worker.join(60)
            assert done == [0], f"import of {spec['file']} failed"
        by_title = {spec["title"]: spec for spec in PAST}
        for folder in list_sessions(self.root / "data"):
            meeting, _ = load_meeting(folder)
            spec = by_title.get(meeting.title or "")
            if spec is None:
                continue
            meeting.tldr = spec["tldr"]
            meeting.participants = spec["participants"]
            for item in meeting.items.values():  # sent where the meeting's tools took them
                if item.kind.value == "action":
                    item.pushed = dict(spec["pushed"])
            if "email" in spec:
                e = spec["email"]
                meeting.followup_email = FollowUpEmail(subject=e["subject"], to=e["to"], body_markdown=e["body"])
            atomic_write(folder / "meeting.json", meeting.model_dump_json(indent=1))
        self.spend_history()
        provider, extra = self.final
        # speech-to-text on this Mac, with its model already downloaded (UIApp writes the files)
        text = (self.config_path.read_text(encoding="utf-8").replace('provider = "heuristic"', f'provider = "{provider}"')
                .replace('backend = "fake"', 'backend = "whisper-cpp"'))
        self.config_path.write_text(text + extra, encoding="utf-8")

    def spend_history(self) -> None:
        spend = self.root / "data" / "spend"
        today = datetime.now().date()
        for days, (stt, llm, agents) in SPEND.items():
            day = (today - timedelta(days=days)).isoformat()
            write(spend / f"{day}.json", json.dumps({"day": day, "stt_usd": stt, "llm_usd": llm, "agents_usd": agents}))


def settings_extra() -> str:
    return """
[transcription.whisper_cpp]
model = "large-v3-turbo-q5_0"
allowed_languages = ["fr", "en"]

[sinks.reminders]
enabled = true
list_name = "Réunions"
due_time = "09:00"

[sinks.hubspot]
enabled = true
owner_email = "camille@maisonberthe.fr"

[budget]
daily_usd = 2.0
agents_daily_usd = 3.0
"""


def make(root: Path, mp: pytest.MonkeyPatch, *, provider: str = "heuristic", **kw: Any) -> DemoApp:
    root.mkdir(parents=True, exist_ok=True)
    keys = {"ANTHROPIC_API_KEY": "sk-ant-demo-0000", "HUBSPOT_TOKEN": "pat-eu1-demo-0000"}
    models = ("large-v3-turbo-q5_0", stt_models.VAD_MODEL) if kw.get("meetings") else ()
    return DemoApp(root, mp, user=USER, provider=provider, extra=settings_extra(), keys=keys, models=models, **kw)


def start_meeting(app: DemoApp) -> None:
    """The meeting in progress: its lines at their times since it began, then the to-dos found in them."""
    import time

    from voicestream.models import TranscriptSegment

    if app.host.prefs.meeting_start == "manual":  # nothing starts by itself: Start meeting, as the page does
        app.host.call(app.host.meeting_start()).result(15)
    deadline = time.monotonic() + 15
    while app.host.status != "listening":
        if time.monotonic() > deadline:
            raise RuntimeError(f"the meeting host stayed {app.host.status!r}")
        time.sleep(0.05)
    cfg = app.host.cfg
    cfg.always_on.idle_split_minutes = 24 * 60  # the demo meeting stays in progress while you look

    async def speak() -> None:
        a = app.host.assistant
        assert a is not None
        now = a.clock.now()
        lines, began = live_timeline()
        start = now - began
        for i, (t0, t1, source, text) in enumerate(lines):
            seg = TranscriptSegment(source=source, speaker=cfg.source_label(source), text=text,
                                    t_start=start + t0, t_end=start + t1)
            await a._route(seg)
            if i == 0:  # the meeting opened on this line: date it back to when it really began
                session = a.current
                assert session is not None
                session.t0 = start
                session.meeting.started_at -= timedelta(seconds=now - start)
                session.meeting.segments[0] = session.meeting.segments[0].model_copy(update={"t_start": t0, "t_end": t1})
        await a.extract_now()

    app.host.call(speak()).result(30)


def main() -> int:
    mp = pytest.MonkeyPatch()
    base = Path(tempfile.mkdtemp(prefix="voicestream-demo-"))
    apps: list[DemoApp] = []
    try:
        stopped = make(base / "stopped", mp, provider="anthropic", setup_done=True, meetings=True)
        apps.append(stopped)
        listening = make(base / "listening", mp, setup_done=True, listen_on_launch=True, meetings=True)
        apps.append(listening)
        meeting = make(base / "meeting", mp, setup_done=True, listen_on_launch=True, meetings=True)
        apps.append(meeting)
        start_meeting(meeting)
        setup = make(base / "setup", mp, setup_done=False, fresh=True, meeting_start="manual")
        apps.append(setup)
        _mac_features(setup)
        # meetings started by hand, with what the Mac app has
        idle = make(base / "by-hand", mp, provider="anthropic", setup_done=True, meetings=True, meeting_start="manual")
        apps.append(idle)
        _mac_features(idle)
        recording = make(base / "by-hand-meeting", mp, setup_done=True, meetings=True, meeting_start="manual")
        apps.append(recording)
        _mac_features(recording)
        start_meeting(recording)
        call = make(base / "by-hand-call", mp, setup_done=True, meetings=True, meeting_start="manual")
        apps.append(call)
        _mac_features(call)
        assert call.host.loop is not None
        call.host.loop.call_soon_threadsafe(call.host.suggest_meeting, "zoom.us")  # a call: suggested, never started

        budget = stopped.folder_of("Budget review")
        screens = [
            ("Live, no meeting in progress", idle, "#/live"),
            ("Live, a meeting started by hand", recording, "#/live"),
            ("Live, a call detected", call, "#/live"),
            ("Settings: starting meetings", idle, "#/settings/general/starting"),
            ("Live, automatic, not listening", stopped, "#/live"),
            ("Live, automatic, listening", listening, "#/live"),
            ("Live, automatic, meeting in progress", meeting, "#/live"),
            ("Meetings", stopped, "#/meetings"),
            *((f"Meeting: {tab}", stopped, f"#/meetings/{budget}/{tab}") for tab in ("notes", "actions", "transcript", "email")),
            ("Meeting in progress, its page", meeting, f"#/meetings/{meeting.host.state()['meeting']['folder']}"),
            ("Costs", stopped, "#/costs"),
            *((f"Settings: {sec}", stopped, f"#/settings/{sec}") for sec in
              ("general", "stt", "claude", "audio", "tools", "agents", "advanced")),
            ("Setup wizard", setup, ""),
            ("Setup: how you start a meeting", setup, "#/setup/start"),
        ]
        width = max(len(name) for name, *_ in screens)
        print("Voicestream UI demo — open these in a browser (Ctrl+C stops it):\n")
        for name, app, route in screens:
            print(f"  {name.ljust(width)}  {app.url}{route}")
        print(flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for app in apps:
            try:
                app.close()
            except Exception as exc:  # noqa: BLE001 - close the others anyway
                print(f"closing: {exc}", file=sys.stderr)
        mp.undo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
