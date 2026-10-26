"""Who you are talking to, from how you speak to them: "Sam, can you…", "Thanks Sam", "Merci Julie".

The moment one of your lines (the microphone, ``Me``) addresses someone by name, that name joins the
meeting's participants — no model call, no waiting for the next extraction. Only a name in a
*vocative* position counts, never one you only talk about ("I'll ask Sam", "Sam said", "chez Marc",
"avec Paul"):

* after a greeting or thanks that opens the clause: "Hi Sam", "Thanks, Sam!", "Bonjour Julie",
  "Salut Marc, ça va ?", "Merci Julie", "Hi Sam and Julie";
* after an acknowledgment, closing the clause: "D'accord Julie", "OK Sam.", "Oui Julie, c'est ça";
* opening a sentence, then a comma and words put to that person: "Sam, can you…", "Julie, tu peux…",
  "Sam, what do you think?";
* closing a question or an exclamation after a comma: "What do you think, Sam?", "…, Julie ?".

Those are the *strong* patterns. Weaker ones — "Sam, the deck is ready.", "That's fine, Sam.",
"Alors, Julie, on fait quoi ?", "Sam?" — count only for a name already known: one of your attendees
(``brain.attendees``), someone from an earlier meeting (:mod:`voicestream.known_people`), or a
participant Claude found. A capitalized word nobody knows needs a strong pattern and must get past a
stoplist: common words that open a sentence ("Honestly, can you…"), days, months, teams, "Claude",
your own name, the words of the wake phrases.

Limits, by design: whisper's punctuation is what the patterns read, so a line it transcribes without
commas finds less; a place or a product called like a person ("Bonjour Paris") passes; a name said to
someone who isn't in the meeting (on the phone to them, say) is taken as a participant. Every name
found this way is shown as a guess you can remove, and Claude's extraction has the last word on the
weak ones (:func:`finalize`).

The rest of the module keeps a meeting's people in order: one list of names (``Meeting.participants``,
what the notes and your tools read), where each came from (``Meeting.people``), and the names you
took out (``Meeting.people_removed``), which nothing adds back.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from ..models import Meeting, PersonInfo, TranscriptSegment


def fold(text: str) -> str:
    """Case- and accent-insensitive form ("Élodie" == "elodie"), apostrophes made plain."""
    text = unicodedata.normalize("NFKD", text.replace("’", "'").strip().lower())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def _words(*groups: str) -> frozenset[str]:
    return frozenset(fold(w) for g in groups for w in g.split())


def _phrases(*phrases: str) -> tuple[tuple[str, ...], ...]:
    # longest first: "thank you" before "thank"
    return tuple(sorted((tuple(fold(p).split()) for p in phrases), key=len, reverse=True))


GREETINGS = _phrases(
    "hi", "hey", "hello", "hiya", "morning", "good morning", "good afternoon", "good evening", "welcome",
    "thanks", "thanks again", "thank you", "thanks a lot", "thank you so much", "thanks so much", "many thanks", "cheers",
    "bye", "goodbye", "bye bye", "see you", "see ya", "congrats", "congratulations", "sorry",
    "bonjour", "bonsoir", "salut", "coucou", "hello", "merci", "merci encore", "merci beaucoup", "merci bien", "mille mercis",
    "au revoir", "à plus", "à bientôt", "à demain", "bienvenue", "bravo", "félicitations", "pardon", "désolé", "désolée",
)
"""Words that open a clause and are said *to* someone: the name right after them is who."""
ACKS = _phrases(
    "ok", "okay", "yes", "yeah", "yep", "no", "nope", "right", "alright", "all right", "sure", "great", "perfect",
    "exactly", "fine", "got it", "understood", "agreed", "noted", "cool", "nice",
    "d'accord", "oui", "ouais", "non", "parfait", "super", "génial", "entendu", "exactement", "bien sûr", "voilà",
    "très bien", "ça marche", "top", "nickel", "c'est noté", "noté", "absolument",
)
"""An answer, then a name closing the clause: "D'accord Julie", "OK Sam."."""
LEAD_INS = _words("and so well oh ah eh hm hmm um uh now then but et alors donc bon ben bah euh mais puis enfin ah oh")
"""Filler before a greeting or a name that still leaves it opening the clause ("So, Sam, can you…")."""
YOU = _words(
    "you your yours you're you've you'll you'd yourself please can could would will do did does are have should "
    "shall what what's how how's when where why which tell let go send check look take give share wait hold "
    "thanks thank listen remember make try just any anything",
    "tu t' toi te ton ta tes vous votre vos est-ce qu'est-ce peux-tu pourrais-tu pouvez-vous pourriez-vous as-tu "
    "avez-vous es-tu êtes-vous sais-tu savez-vous dis-moi dites-moi regarde regardez écoute écoutez attends "
    "attendez merci s'il vas-y allez-y envoie envoyez peux pourrais pouvez comment quand où pourquoi quoi qu'en "
    "tiens tenez qu'est-ce-que",
)
"""Words that open what is said to the person just named: "Sam, *can* you…", "Julie, *tu* peux…"."""
THEY = _words("he she they him them his her their it's was were and or nor "
              "il elle ils elles lui leur leurs son sa ses et ou ni était a")
"""Words after "Name," that make it the subject talked about ("Sam, he said…") or a list ("Sam, and Julie")."""
TITLES = _words("mr mrs ms miss dr doctor monsieur madame mademoiselle mme mlle")

STOP = _words(
    # English words that open a sentence or answer
    "so well okay ok now yes yeah yep no nope right alright sure great good nice cool perfect awesome excellent "
    "fine actually honestly also and but or then first second third next last finally basically listen look "
    "sorry please wait hold anyway anyways besides meanwhile otherwise however therefore still yet maybe perhaps "
    "sometimes obviously clearly again just really very thanks thank hello hi hey cheers bye goodbye welcome "
    "oh ah uh um hmm wow the a an this that these those it its i we you they he she me us them my our your "
    "their his her what why how when where who which if because since while after before here there everyone "
    "everybody someone somebody anyone anybody nobody nothing everything something all both each every some "
    "any many much more most other another such same true false exactly absolutely definitely totally indeed "
    "agreed done deal understood noted got gotcha correct wrong interesting amazing fantastic brilliant lovely "
    "sweet fair enough sounds let lets let's go come see question answer thing things point quick instead "
    "ideally tonight today tomorrow yesterday morning afternoon evening night week weekend month year time "
    "dear sir madam ma'am boss man dude bro buddy mate friend friends guys folks people team ladies gentlemen "
    "world god lord jesus christ mister miss missus doctor guy girls boys kids "
    "marketing sales finance design engineering product support legal ops it hr dev devs tech management "
    "everyone's whatever whoever cool super nope yup hmm mhm aha huh yo",
    # French
    "bon alors donc oui non ouais si voilà voila bref enfin ensuite puis bien très parfait super génial top "
    "nickel impeccable excellent d'accord dac ok entendu ça ca cela ceci ce c'est là ici voici ah oh euh hum "
    "bah ben hein quoi sinon mais et ou aussi également vraiment juste maintenant aujourd'hui demain hier "
    "écoute écoutez dis dites attends attendez désolé désolée pardon excuse excusez merci bonjour bonsoir "
    "salut coucou bienvenue revoir plus tous toutes tout le la les un une des du de je tu il elle on nous "
    "vous ils elles moi toi lui eux mon ton son ma ta sa mes tes ses notre votre leur nos vos leurs qui que "
    "quand comment pourquoi où quel quelle car parce puisque comme avec pour sans chez dans sur sous après "
    "avant pendant depuis déjà encore toujours jamais rien personne quelqu'un chacun certes effectivement "
    "exactement absolument cool chouette allez tiens tenez regarde regardez voyons madame monsieur "
    "mademoiselle messieurs mesdames chef patron gars équipe monde mec perso soir matin semaine week-end "
    "mois an année heure question point truc chose dieu bravo félicitations",
    # days and months
    "monday tuesday wednesday thursday friday saturday sunday january february march april may june july "
    "august september october november december",
    "lundi mardi mercredi jeudi vendredi samedi dimanche janvier février mars avril mai juin juillet août "
    "septembre octobre novembre décembre",
    # the assistant and the voice assistants people call out to
    "claude assistant siri alexa google",
)
"""Capitalized words that are not names in the places this module looks (a known name gets past it)."""
_ADVERB = re.compile(r"(?:ally|ously|ately|ively|ently|antly|fully|ment)$")
SENTENCE_END = frozenset(".!?…;:")


@dataclass(frozen=True, slots=True)
class Addressed:
    """A name you addressed: ``strong`` for a pattern that leaves little doubt, ``known`` when the name
    was known before (then it is in the form it was known by)."""

    name: str
    strong: bool
    known: bool


# ----------------------------------------------------------------------- tokens

_TOKEN = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*'?|\d+|[,.!?…;:]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.replace("’", "'"))


def _is_punct(tok: str) -> bool:
    return not tok[0].isalnum()


def _name_shaped(tok: str) -> bool:
    """A capitalized word, or capitalized parts joined by hyphens ("Jean-Pierre"): never an acronym
    ("CEO"), a contraction ("D'accord", "Sam's") or a word starting lower case."""
    parts = tok.split("-")
    return all(len(p) >= 2 and p.isalpha() and p[0].isupper() and any(c.islower() for c in p) for p in parts)


def _sentences(tokens: list[str]) -> list[tuple[list[str], str]]:
    """``(tokens without the end mark, end mark)``; commas stay in."""
    out: list[tuple[list[str], str]] = []
    cur: list[str] = []
    for tok in tokens:
        if _is_punct(tok) and any(c in SENTENCE_END for c in tok):
            if cur:
                out.append((cur, "?" if "?" in tok else "!" if "!" in tok else "."))
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append((cur, ""))
    return out


class _Known:
    """Known names by their folded form, and by first name when only one known person has it."""

    def __init__(self, names: Iterable[str]) -> None:
        self.full: dict[str, str] = {}
        firsts: dict[str, set[str]] = {}
        for name in names:
            name = " ".join(name.split())
            key = fold(name)
            if not key or "@" in key:
                continue
            self.full.setdefault(key, name)
            firsts.setdefault(key.split()[0], set()).add(name)
        self.first = {k: next(iter(v)) for k, v in firsts.items() if len(v) == 1}
        self.shared = {k for k, v in firsts.items() if len(v) > 1}

    def match(self, words: list[str]) -> str | None:
        key = fold(" ".join(words))
        if key in self.full:
            return self.full[key]
        if len(words) == 1:
            if key in self.first:
                return self.first[key]
            if key in self.shared:
                return words[0]  # two known Sams: the one said, not a guess at which
        return None


# --------------------------------------------------------------------- patterns


def _ends_with(seq: list[str], phrases: tuple[tuple[str, ...], ...]) -> int:
    """Length of the phrase ``seq`` ends with (0: none)."""
    folded = [fold(t) for t in seq]
    for p in phrases:
        if len(p) <= len(folded) and tuple(folded[len(folded) - len(p):]) == p:
            return len(p)
    return 0


def _opens_clause(before: list[str]) -> bool:
    """Nothing but filler (or a greeting or an answer) since the sentence or the last comma began."""
    rest = before[:]
    while rest:
        if rest[-1] == ",":
            return True
        n = _ends_with(rest, GREETINGS) or _ends_with(rest, ACKS) or (1 if fold(rest[-1]) in LEAD_INS else 0)
        if not n:
            return False
        rest = rest[:-n]
    return True


class _Finder:
    def __init__(self, known: Iterable[str], exclude: Iterable[str]) -> None:
        self.known = _Known(known)
        self.exclude: set[str] = set()
        for name in exclude:
            key = fold(name)
            if key:
                self.exclude.add(key)
                self.exclude.add(key.split()[0])  # "Camille" when you are "Camille Martin"

    def candidate(self, toks: list[str], i: int) -> tuple[str, int, bool] | None:
        """``(name, how many tokens, known)`` for a name starting at ``toks[i]``."""
        if not _name_shaped(toks[i]):
            return None
        two = toks[i : i + 2]
        if len(two) == 2 and _name_shaped(two[1]) and (full := self.known.match(two)):
            return None if self._excluded(full) else (full, 2, True)
        # a second capitalized word makes it a full name ("Thanks Sam Taylor"), even after a known first name
        surname = len(two) == 2 and _name_shaped(two[1]) and fold(two[1]) not in STOP and fold(two[1]) not in self.exclude
        one = self.known.match([toks[i]])
        if one is not None and not surname:
            return None if self._excluded(one) else (one, 1, True)
        key = fold(toks[i])
        if key in self.exclude or (one is None and (key in STOP or (len(key) >= 7 and _ADVERB.search(key)))):
            return None
        return (f"{two[0]} {two[1]}", 2, False) if surname else (toks[i], 1, False)

    def _excluded(self, name: str) -> bool:
        key = fold(name)
        return key in self.exclude or key.split()[0] in self.exclude

    def strength(self, toks: list[str], i: int, n: int, end: str) -> str | None:
        """``"strong"``, ``"weak"`` or ``None`` (not addressed) for the name ``toks[i:i+n]``."""
        before, after = toks[:i], toks[i + n :]
        closes = not after or after[0] == ","
        # "Hi Sam", "Thanks, Sam!", "Hi Sam and Julie", "Merci Julie pour…"
        head = self._strip_names(before)
        if head and head[-1] == ",":
            head = head[:-1]
        g = _ends_with(head, GREETINGS)
        if g and _opens_clause(head[:-g]) and (closes or fold(after[0]) in {"and", "et", "for", "pour", "again", "encore"}):
            return "strong"
        # "D'accord Julie", "OK Sam."
        if (a := _ends_with(head, ACKS)) and _opens_clause(head[:-a]) and closes:
            return "strong"
        # "Sam, can you…" / "Julie, tu peux…" / "So, Sam, what do you think?"
        if _opens_clause(before) and after and after[0] == ",":
            rest = after[1:]
            if not rest:
                return "weak"
            first = fold(rest[0])
            if first in YOU or first.startswith("t'"):
                return "strong"
            if first in THEY or (_name_shaped(rest[0]) and len(rest) > 1 and (rest[1] == "," or fold(rest[1]) in {"and", "et", "or", "ou"})):
                return None  # talked about, or a list of names
            return "weak"
        # "What do you think, Sam?" / "That's fine, Sam."
        if before and before[-1] == "," and not after:
            if end in "?!" and end:
                return "strong"
            said = {fold(t) for t in before}
            return "weak" if said & (YOU | {"thanks", "thank", "merci"}) or any(t.startswith("t'") for t in said) else None
        # "Je pense, Julie, que tu…"
        if before and before[-1] == "," and after and after[0] == "," and len(after) > 1:
            first = fold(after[1])
            return "strong" if first in YOU or first.startswith("t'") else None
        # "Sam?"
        if not before and not after and end in "?!" and end:
            return "weak"
        return None

    def _strip_names(self, before: list[str]) -> list[str]:
        """Drop the names (and the "," / "and" between them) at the end of ``before``: "Hi Sam and"
        → "Hi", so Julie in "Hi Sam and Julie" is greeted too."""
        rest = before[:]
        while len(rest) >= 2 and (rest[-1] == "," or fold(rest[-1]) in {"and", "et"}) and _name_shaped(rest[-2]) and fold(rest[-2]) not in STOP:
            rest = rest[:-2]
        return rest


def addressed(text: str, known: Iterable[str] = (), exclude: Iterable[str] = ()) -> list[Addressed]:
    """The people ``text`` (one of your lines) speaks to, in the order they come.

    ``known``: names known already (attendees, earlier meetings, this meeting's participants), in the
    form to return them; ``exclude``: names that are never someone you address here (your own, the
    assistant's, the wake phrases' words)."""
    finder = _Finder(known, exclude)
    found: dict[str, Addressed] = {}
    for sent, end in _sentences(_tokens(text)):
        # "Bonjour Monsieur Martin": the title goes, the name stays
        toks = [t for k, t in enumerate(sent) if not (fold(t) in TITLES and k + 1 < len(sent) and _name_shaped(sent[k + 1]))]
        i = 0
        while i < len(toks):
            if _is_punct(toks[i]):
                i += 1
                continue
            cand = finder.candidate(toks, i)
            if cand is None:
                i += 1
                continue
            name, n, known_name = cand
            how = finder.strength(toks, i, n, end)
            if how == "strong" or (how == "weak" and known_name):
                key = fold(name)
                prev = found.get(key)
                if prev is None or (how == "strong" and not prev.strong):
                    found[key] = Addressed(name, how == "strong", known_name)
            i += n
    return list(found.values())


# ------------------------------------------------------------- a meeting's people


def same_person(a: str, b: str) -> bool:
    """"Sam" and "Sam Lefèvre" are the same person; "Sam Lefèvre" and "Sam Martin" are not."""
    ka, kb = fold(a), fold(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    wa, wb = ka.split(), kb.split()
    return (len(wa) == 1 and wa[0] == wb[0]) or (len(wb) == 1 and wb[0] == wa[0])


def is_user(name: str, user_name: str) -> bool:
    key, me = fold(name), fold(user_name)
    if key in {"me", "moi", "i", "myself", "self", "user", "the user", "assistant", "claude", "ai", "the assistant", "agent", ""}:
        return True
    return bool(me) and (key == me or key == me.split()[0])


def _find(meeting: Meeting, name: str) -> str | None:
    """The participant ``name`` is (exactly the same first, then "Sam" for "Sam Lefèvre"); ``None``
    when nobody, or when it could be two of them."""
    key = fold(name)
    for p in meeting.participants:
        if fold(p) == key:
            return p
    hits = [p for p in meeting.participants if same_person(p, name)]
    return hits[0] if len(hits) == 1 else None


def is_removed(meeting: Meeting, name: str) -> bool:
    return any(same_person(r, name) for r in meeting.people_removed)


def add_person(
    meeting: Meeting, name: str, user_name: str = "", *, heard: bool = False, strong: bool = False,
    model: bool = False, you: bool = False,
) -> bool:
    """Add ``name`` to the meeting's participants, or note one more source for someone already there.
    Returns whether anything changed. A name you removed comes back only when you add it yourself."""
    name = " ".join(name.split())[:80]
    if not name or is_user(name, user_name) or len(name) < 2:
        return False
    if is_removed(meeting, name):
        if not you:
            return False
        meeting.people_removed = [r for r in meeting.people_removed if not same_person(r, name)]
    existing = _find(meeting, name)
    if existing is None and any(same_person(p, name) for p in meeting.participants):
        return False  # "Sam" with two Sams in the meeting: already there, whichever it is
    if existing is None:
        meeting.participants.append(name)
        meeting.people[name] = PersonInfo(heard=heard, strong=strong and heard, model=model, you=you)
        return True
    old = meeting.people.get(existing)
    info = old.model_copy() if old is not None else PersonInfo(model=True)  # saved before people existed: Claude's
    info.heard |= heard
    info.strong |= strong and heard
    info.model |= model
    info.you |= you
    target = existing
    if len(fold(name).split()) > len(fold(existing).split()) and (model or you):
        target = name  # the full name, once Claude or you give it
        meeting.participants[meeting.participants.index(existing)] = name
        meeting.people.pop(existing, None)
    changed = target != existing or old is None or info != old
    meeting.people[target] = info
    return changed


def remove_person(meeting: Meeting, name: str) -> bool:
    """Take ``name`` out of the meeting for good (see :func:`add_person`). Returns whether it was there."""
    key = fold(name)
    gone = [p for p in meeting.participants if fold(p) == key] or [p for p in meeting.participants if same_person(p, name)]
    for p in gone:
        meeting.participants.remove(p)
        meeting.people.pop(p, None)
    if key and not any(fold(r) == key for r in meeting.people_removed):
        meeting.people_removed.append(" ".join(name.split())[:80])
    return bool(gone)


def finalize(meeting: Meeting, final: Iterable[str], user_name: str = "") -> None:
    """Claude's final word on who took part (the end-of-meeting summary). It replaces what the live
    extraction guessed, except for what you said: names you added stay, and so do names you addressed
    in a strong pattern ("Thanks Sam", "Sam, can you…") — Claude may well have missed someone who never
    spoke. A name only heard in a weak pattern, that Claude doesn't list, goes."""
    final = [f for f in (" ".join(x.split()) for x in final) if f]
    keep: list[str] = []
    for p in meeting.participants:
        info = meeting.people.get(p) or PersonInfo(model=True)
        if info.you or (info.heard and info.strong) or any(same_person(p, f) for f in final):
            keep.append(p)
    meeting.participants = keep
    meeting.people = {p: meeting.people[p] for p in keep if p in meeting.people}
    for f in final:
        add_person(meeting, f, user_name, model=True)


def other_side(meeting: Meeting, sure: bool = False) -> str | None:
    """The one other person in the meeting, when there is exactly one: the meeting audio is them.

    ``sure``: only when that person is more than a guess from a weak pattern (Claude or you confirmed
    them, or you addressed them unmistakably) — what Claude reads, so it doesn't just agree with a guess."""
    if len(meeting.participants) != 1:
        return None
    name = meeting.participants[0]
    info = meeting.people.get(name)
    if sure and info is not None and not (info.confirmed() or info.strong):
        return None
    return name


def speaker_label(seg: TranscriptSegment, other: str | None, others_label: str = "Others") -> str:
    """``seg``'s speaker for the notes and the summary: the meeting audio's lines carry the other
    person's name when there is only one (:func:`other_side`); the stored label stays as it is."""
    if other and seg.source != "mic" and seg.speaker == others_label:
        return other
    return seg.speaker


def people_event(meeting: Meeting) -> dict[str, object]:
    """What ``meeting.people`` events (and the people routes) say about a meeting's people."""
    return {
        "meeting_id": meeting.id,
        "participants": list(meeting.participants),
        "people": {k: v.model_dump() for k, v in meeting.people.items()},
        "people_removed": list(meeting.people_removed),
    }
