# Said & Done on X: how we post

This is the guide for the X posts about **Said & Done**. The app is still called Voicestream in the
download, the code and the menu bar. A scheduled task reads this file every week, before it writes
the next 8 posts.

## The name

- **Said & Done**. Always written with the `&`, never "Said and Done" or "S&D".
- The promise behind it: what's said in a meeting gets done. Notes come second.
- Until the app itself is renamed, a post that sends people to the download says "(still called
  Voicestream in the download)". Posts that don't send anyone to the download don't mention the
  old name.

## 8 posts a week

| # | Day | Language | Kind |
|---|-----|----------|------|
| 1 | Mon | EN | Shipped: the biggest change of the week, said as what it does for you |
| 2 | Mon | FR | Pourquoi : un problème de réunion que l'app règle |
| 3 | Tue | EN | Trust: privacy, consent, what stays on the Mac, what you approve |
| 4 | Wed | FR | Livré : une autre nouveauté de la semaine |
| 5 | Thu | EN | Under the hood: how one part works (speech-to-text, Claude, agents, MCP) |
| 6 | Fri | FR | Chiffres : coût, temps, nombre de tâches… seulement des chiffres vrais |
| 7 | Sat | EN | Small detail: a shortcut, a button, a sentence the app says |
| 8 | Sun | FR | Question : on demande l'avis des gens (prochain connecteur, prochaine fonction) |

4 in English, 4 in French. If a week ships nothing new, use the posts marked "Shipped" or "Livré"
for features the earlier weeks didn't cover yet. Never post the same feature twice in 4 weeks:
check `social/posts/`.

## Rules

- **280 characters at most** per post. No hashtag soup: one hashtag at most, usually none.
- **Only true facts**: what the code, README, release notes or commits say. Never invent usage
  ("I use it every day"), numbers or user quotes. A personal story the author must write is left
  as `[à compléter : …]` / `[fill in: …]` so it can't be posted by accident.
- **Say what it does for the person**, not how it's coded. Commit messages are written for
  developers ("The shortcut is named only next to what a press does"): translate them.
- **Privacy comes first.** Never make it sound like the app secretly records people. Meetings start
  when you start them; speech-to-text can run on the Mac; to-dos can wait for your OK.
- **Beta is called beta.** Don't sell what hasn't been tested (Promethee's own server wasn't
  tested yet in 0.4.0-beta.1).
- First person singular ("I built", "j'ai ajouté"), plain words, no "revolutionary", no "game
  changer", no rocket emoji.

## Facts to draw from

- Listens to your mic and the computer's audio, so it hears everyone on the call. Transcribes live
  in French or English, detected as people speak.
- Claude turns it into to-dos, decisions and notes: Google Calendar, Apple Reminders, HubSpot,
  a Google Doc, a Gmail recap and a follow-up draft for the attendees, Promethee (via MCP, beta),
  other Mac apps through Shortcuts.
- Agents: when someone says "we need a competitor analysis by Friday", a Claude agent can start on
  it during the meeting, sandboxed on the Mac, with its own budget.
- About $0.80 a day for 8 hours of meetings with speech-to-text on the Mac (free, the default).
- Mac app (Apple Silicon, macOS 14.2+): menu bar, floating button, ⌥⌘R from any app, Spotlight.
- Timeline: 0.2.0 Oct 19 2026 (Mac app redesign, ⌥⌘R) · 0.3.0 Oct 22 (meetings start when you say
  so, call detection) · Oct 26 (updates itself, names people) · 0.4.0-beta.1 Oct 30 (Promethee,
  you approve the to-do list) · Oct 30 (speakers told apart by voice, mute only yourself).
