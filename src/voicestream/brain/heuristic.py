"""Offline, rule-based extraction.

Used when no Claude API key is configured, when the daily budget is exhausted,
and in tests. It only catches explicit phrasings ("I'll send...", "can you...",
"we decided...", "Hey Claude, ..." and their French equivalents "je vais…",
"tu peux…", "on a décidé…", "Dis Claude, …"), so treat it as a safety net, not a
peer of the LLM extractor.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..models import ItemKind, TranscriptSegment
from .tracker import ExtractedItem, ExtractionResult

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

_SELF_COMMIT = re.compile(
    r"\b(?:(?:i'll|i will|i'm going to|i am going to|let me|i can take|i'll take|i need to|i have to|i should"
    r"|je m'en occupe[,:]?|je vais|je dois|il faut que je|je peux m'en charger[,:]?)\s+"
    r"|(?:je vais m'occuper|je m'occupe|je me charge) d(?:e\s+|'))(?P<task>.+)",
    re.I,
)
_WE_COMMIT = re.compile(
    r"\b(?:we need to|we have to|we should|we must|let's make sure to|let's"
    r"|il faut qu'on|il faut|il faudrait|on doit|on devrait|nous devons)\s+(?P<task>.+)",
    re.I,
)
_REQUEST_WORDS = (
    r"can you|could you|would you|will you|please|can someone|could someone"
    r"|est-ce que tu peux|est-ce que tu pourrais|est-ce que vous pouvez|tu peux|tu pourrais|peux-tu|pourrais-tu"
    r"|vous pouvez|pouvez-vous|pourriez-vous|merci de|quelqu'un peut"
)
_REQUEST = re.compile(rf"\b(?:{_REQUEST_WORDS})\s+(?P<task>.+?)(?:\?|$)", re.I)
# Capitalised sentence openers that aren't someone being addressed ("So can you…", "Alors, tu peux…").
# "Ben" is left out: as a name it is far more common than the French filler.
_NOT_NAMES = (
    r"um+|uh+|so|okay|ok|yeah|yes|no|and|also|then|just|actually|basically|well|right|alright|now|but|oh|hey|hi"
    r"|sorry|thanks|great|cool|maybe|everyone|guys|euh+|bon|alors|donc|du coup|en fait|et|oui|non|bref|voilà"
    r"|merci|salut|super|parfait|écoute|dis"
)
# A capitalised name ("Sarah", "Élodie", "Jean-Pierre") opening the sentence, after any fillers. The
# patterns using it are case-sensitive on purpose (only their keywords are (?i:…)), or any word would do.
_LEAD_NAME = (
    rf"^(?:(?i:{_NOT_NAMES})[,\s]+)*(?!(?i:{_NOT_NAMES})\b)(?P<name>[A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)?)"
)
_NAMED_REQUEST = re.compile(rf"{_LEAD_NAME},?\s+(?i:{_REQUEST_WORDS})\s+(?P<task>.+?)(?:\?|$)")
_NAMED_COMMIT = re.compile(rf"{_LEAD_NAME}\s+(?:will|is going to|needs to|should|va|doit|devrait)\s+(?P<task>.+)")
# "action items: …", "TODO update the wiki". "follow up", "action" and "à faire" are also ordinary words
# ("take action on…", "I'll follow up with…"), so they only count as a label before a separator; an
# imperative "Follow up with legal…" opening the sentence is a task in itself (verb included).
_LABEL = r"action items?|to-?dos?|points? d'actions?"
_EXPLICIT_ITEM = re.compile(
    rf"\b(?:(?:{_LABEL}|to dos?|follow[- ]ups?|à faire|actions?)(?:\s*(?:(?:is|are|for|pour)\b|[:,–—-]))+"
    rf"|(?:{_LABEL})\b|^(?=follow[- ]up (?:with|on|about|regarding)\b))\s*(?P<task>.+)",
    re.I,
)
_DECISION = re.compile(
    r"\b(?:we decided|we've decided|we agreed|we've agreed|decision is|let's go with|we'll go with|we're going with|agreed that"
    r"|on a décidé|nous avons décidé|c'est décidé|décision|on part sur|on valide|on est d'accord|on retient)"
    r"\s*(?:to|that|on|de|d'|que|qu'|pour|:)?\s*(?P<what>.+)",
    re.I,
)
_OPEN_Q = re.compile(
    r"\b(?:open question|we need to figure out|not sure (?:who|what|how|when|whether)|tbd"
    r"|question ouverte|à clarifier|à voir|on ne sait pas (?:qui|quand|comment|si))\b(?P<what>.*)",
    re.I,
)
_DUE = re.compile(
    r"\b(?P<due>(?:by|before|until)\s+(?:next|this) \w+|"
    # after "on", only time words: "on this feature" is the task's object, not a deadline
    r"(?:by|before|on|until)\s+(?:end of (?:the )?(?:day|week|month)|eod|eow|tomorrow|today|tonight|"
    r"(?:next|this) (?:week(?:end)?|month|morning|afternoon|evening|monday|tuesday|wednesday|thursday|friday|saturday"
    r"|sunday)|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\.? \d{1,2}(?:st|nd|rd|th)?)|"
    r"tomorrow|end of (?:the )?(?:day|week)|eod|eow|next week|in (?:\d+|a|two|three|a few|a couple of) (?:days?|weeks?)"
    r"|(?:d'ici|avant|pour) (?:la fin de (?:la )?(?:semaine|journée|mois)|demain|ce soir|lundi|mardi|mercredi|jeudi"
    r"|vendredi|samedi|dimanche|la semaine prochaine|le \d{1,2}(?:er)?(?: \w+)?)(?: prochaine?)?"
    r"|demain|après-demain|ce soir|la semaine prochaine|en fin de journée"
    r"|dans (?:\d+|deux|trois|huit|quinze|quelques) (?:jours|semaines))(?!\w)",
    re.I,
)
_FILLER = re.compile(
    r"^(?:um+|uh+|so|okay|ok|yeah|and|also|then|just|actually|basically|euh+|bon|alors|donc|ben|du coup|en fait|et)[,\s]+",
    re.I,
)
_SKIP_TASKS = re.compile(
    r"^(?:see|think|guess|mean|say|be honest|be fair|go|do it|do that|try|check|look)\b[\s.!?]*$"
    r"|^(?:get started|start|begin|kick off|move on|wrap up|wrap it up|recap|dive in|jump in|go ahead|"
    r"take a (?:step back|look|break)|see|think about it|circle back|discuss|know|share (?:my|the|your) screen)\b"
    r"|^(?:commencer|démarrer|y aller|passer à|faire court|faire simple|partager (?:mon |l')écran|voir|regarder|réfléchir)\b",
    re.I,
)


def _clean_task(task: str) -> str:
    task = task.replace("’", "'").strip().strip(":;,-–—").rstrip(".!?").strip()
    task = _DUE.sub("", task).strip().rstrip(",;").strip()
    task = re.sub(r"\s+", " ", task)
    while (m := _FILLER.match(task)) is not None:
        task = task[m.end():]
    return task[:1].upper() + task[1:] if task else task


def extract_heuristic(
    segments: Sequence[tuple[int, TranscriptSegment]],
    *,
    user_name: str,
    wake_phrases: Sequence[str] = (),
    commands_from: Sequence[str] = ("mic",),
) -> ExtractionResult:
    """``segments`` are ``(ref, segment)`` pairs; refs are echoed in ``segment_refs``."""
    items: list[ExtractedItem] = []
    wake = [w.lower() for w in wake_phrases]
    for ref, seg in segments:
        is_me = seg.source == "mic"
        text = seg.text.strip().replace("’", "'")
        low = text.lower()

        # Direct commands to the assistant: "Hey Claude, draft a ..."
        if seg.source in commands_from:
            hit = next((w for w in wake if w in low), None)
            if hit:
                cmd = text[low.index(hit) + len(hit):].lstrip(" ,.:;-")
                if len(cmd.split()) >= 2:
                    items.append(
                        ExtractedItem(
                            kind=ItemKind.ACTION,
                            title=_clean_task(cmd)[:120],
                            detail="" if len(cmd) <= 120 else cmd,
                            owner="Assistant",
                            confidence=0.9,
                            evidence=text[:200],
                            segment_refs=[ref],
                            delegate=True,
                            agent_prompt=cmd,
                            explicit_request=True,
                        )
                    )
                    continue

        for sentence in _SENT_SPLIT.split(text):
            sentence = sentence.strip()
            if len(sentence) < 8:
                continue
            due_m = _DUE.search(sentence)
            due = due_m.group("due") if due_m else None

            if m := _DECISION.search(sentence):
                what = _clean_task(m.group("what"))
                if len(what.split()) >= 2:
                    items.append(_mk(ItemKind.DECISION, what, None, None, 0.6, sentence, ref))
                continue
            if m := _OPEN_Q.search(sentence):
                what = _clean_task(m.group("what")) or _clean_task(sentence)
                items.append(_mk(ItemKind.QUESTION, what, None, None, 0.5, sentence, ref))
                continue

            owner: str | None = None
            task: str | None = None
            conf = 0.55
            if m := _EXPLICIT_ITEM.search(sentence):
                task, conf = m.group("task"), 0.75
                first_person = re.search(r"\b(?:(?:i|me|my|je|moi|mon|ma|mes)\b|j')", task, re.I)
                owner = user_name if is_me and first_person else None
                inner = _SELF_COMMIT.match(task) or _WE_COMMIT.match(task)
                if inner:
                    task = inner.group("task")
            elif m := _NAMED_REQUEST.search(sentence):
                owner, task, conf = m.group("name"), m.group("task"), 0.7
            elif m := _NAMED_COMMIT.search(sentence):
                owner, task, conf = m.group("name"), m.group("task"), 0.6
            elif m := _SELF_COMMIT.search(sentence):
                task = m.group("task")
                owner = user_name if is_me else (seg.speaker if seg.speaker not in ("Others", "Them") else None)
                conf = 0.65 if due else 0.55
            elif m := _REQUEST.search(sentence):
                task = m.group("task")
                # someone else asking "can you" is usually asking me; me asking is someone else's task
                owner = None if is_me else user_name
                conf = 0.6
            elif m := _WE_COMMIT.search(sentence):
                task, conf = m.group("task"), 0.5

            if not task:
                continue
            task = _clean_task(task)
            if len(task.split()) < 2 or _SKIP_TASKS.match(task):
                continue
            items.append(_mk(ItemKind.ACTION, task, owner, due, conf, sentence, ref))
    return ExtractionResult(new_items=items)


def _mk(
    kind: ItemKind, title: str, owner: str | None, due: str | None, conf: float, evidence: str, ref: int
) -> ExtractedItem:
    return ExtractedItem(
        kind=kind,
        title=title[:160],
        owner=owner,
        due_text=due,
        confidence=conf,
        evidence=evidence[:200],
        segment_refs=[ref],
    )
