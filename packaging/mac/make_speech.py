"""Make the speech clips the macOS CI job transcribes with the real whisper-server.

One English and one French sentence (about 8 s each) spoken by the Mac's own voices with ``say``,
converted with ``afconvert`` to 16 kHz mono 16-bit WAV, next to the text that was spoken::

    <out>/en.wav  <out>/en.txt  <out>/fr.wav  <out>/fr.txt

That folder is what ``VOICESTREAM_WHISPER_TEST_SPEECH`` points tests/test_whisper_cpp.py at.

    python packaging/mac/make_speech.py <out>
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SENTENCES = {
    "en": (
        "Good morning everyone. Let's review the budget for the marketing campaign, "
        "and then we will send the final report to our client before Friday."
    ),
    "fr": (
        "Bonjour à tous. Nous allons commencer la réunion par le budget du projet, "
        "puis nous enverrons le compte rendu final au client avant vendredi."
    ),
}
PREFERRED = {
    "en": ("Samantha", "Alex", "Daniel", "Karen", "Moira", "Tessa"),
    "fr": ("Thomas", "Amélie", "Audrey", "Aurélie", "Jacques", "Marie"),
}
LOCALES = {"en": ("en_US", "en_GB", "en_AU"), "fr": ("fr_FR", "fr_CA", "fr_BE", "fr_CH")}
NOVELTY = {
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Deranged", "Fred", "Good News",
    "Hysterical", "Jester", "Junior", "Kathy", "Organ", "Pipe Organ", "Ralph", "Superstar", "Trinoids",
    "Whisper", "Wobble", "Zarvox",
}  # fmt: skip
"""Joke voices: whisper would be right to get them wrong."""
MIN_S, MAX_S = 4.0, 15.0

_VOICE = re.compile(r"^(?P<name>.+?)\s+(?P<locale>[a-z]{2,3}[_-][A-Za-z0-9]+)\s+#")


def parse_voices(listing: str) -> list[tuple[str, str]]:
    """``say -v '?'`` lines (``Thomas   fr_FR    # Bonjour…``; names may hold spaces and
    parentheses, e.g. ``Eddy (French (France))``) -> [(name, locale)]."""
    voices = []
    for line in listing.splitlines():
        m = _VOICE.match(line.strip())
        if m:
            voices.append((m.group("name").strip(), m.group("locale").replace("-", "_")))
    return voices


def pick_voice(voices: list[tuple[str, str]], lang: str) -> str | None:
    """A natural voice for ``lang``: a well-known one first, else any of the language's locales,
    best locale first (France before Canada...), never a joke voice."""
    usable = [(n, loc) for n, loc in voices if n not in NOVELTY and loc.split("_")[0] == lang]
    by_name = {n: loc for n, loc in usable}
    for name in PREFERRED.get(lang, ()):
        if name in by_name:
            return name
    for locale in LOCALES.get(lang, ()):
        for n, loc in usable:
            if loc == locale:
                return n
    return usable[0][0] if usable else None


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit(f"{path}: not 16 kHz mono 16-bit")
        return w.getnframes() / w.getframerate()


def speak(voice: str, text: str, wav: Path) -> float:
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "speech.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)  # noqa: S603, S607
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)  # noqa: S603, S607
    return wav_seconds(wav)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: make_speech.py <out-dir>", file=sys.stderr)
        return 2
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    voices = parse_voices(subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout)  # noqa: S603, S607
    for lang, text in SENTENCES.items():
        voice = pick_voice(voices, lang)
        if voice is None:
            print(f"no {lang} voice installed; voices: {voices}", file=sys.stderr)
            return 1
        seconds = speak(voice, text, out / f"{lang}.wav")
        (out / f"{lang}.txt").write_text(text + "\n", encoding="utf-8")
        print(f"{lang}: {voice}, {seconds:.1f} s")
        if not MIN_S <= seconds <= MAX_S:
            print(f"{lang}: {seconds:.1f} s is outside {MIN_S}-{MAX_S} s", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
