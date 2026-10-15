"""Echo suppression: without headphones the mic re-hears the other participants, which must
not turn their words into "Me" lines (and so into the user's commitments)."""

from __future__ import annotations

from helpers import seg
from voicestream.config import EchoConfig
from voicestream.transcribe.echo import EchoFilter, containment, words

ASK = "Can you send the revised pricing deck to Acme by Friday?"
MANGLED = "can you send the revised price deck to acme by friday"  # what the mic makes of it


def test_word_containment_is_a_multiset_fraction() -> None:
    assert words("It's 3 o'clock, OK?") == ["it's", "3", "o'clock", "ok"]
    assert containment(["a", "a", "b"], ["a", "b"]) == 2 / 3
    assert containment([], ["a"]) == 0.0


def test_mic_copy_is_dropped_when_system_segment_arrives_later() -> None:
    f = EchoFilter()
    assert f.add(seg(MANGLED, "mic", t=10.4), now=100.0) == []  # held, not published yet
    system = seg(ASK, "system", t=10.0)
    assert f.add(system, now=100.5) == [system]
    assert f.dropped == 1
    assert f.tick(200.0) == [] and f.flush() == []


def test_mic_copy_is_dropped_when_system_segment_came_first() -> None:
    f = EchoFilter()
    system = seg(ASK, "system", t=10.0)
    assert f.add(system, now=100.0) == [system]
    assert f.add(seg(MANGLED, "mic", t=11.0), now=100.5) == []
    assert f.dropped == 1
    assert f.flush() == []


def test_genuine_mic_speech_is_kept_and_released_after_hold() -> None:
    f = EchoFilter(hold_s=4.0)
    f.add(seg(ASK, "system", t=10.0), now=100.0)
    mine = seg("Sure, I'll have it ready before the board meeting.", "mic", t=12.0)
    assert f.add(mine, now=101.0) == []
    assert f.tick(104.9) == []
    assert f.tick(105.0) == [mine]
    assert f.dropped == 0


def test_same_words_far_apart_in_time_are_not_echo() -> None:
    f = EchoFilter()
    f.add(seg(ASK, "system", t=10.0), now=100.0)
    later = seg(ASK, "mic", t=40.0)  # the user repeats the request half a minute later
    f.add(later, now=130.0)
    assert f.flush() == [later]


def test_held_mic_segment_is_released_once_system_audio_moves_past_it() -> None:
    f = EchoFilter()
    mine = seg("I'll draft the agenda for Monday.", "mic", t=10.0, dur=3.0)
    f.add(mine, now=100.0)
    near = seg("Great, thanks for doing that.", "system", t=18.0)  # still within window_s of the mic line
    assert f.add(near, now=100.5) == [near]
    past = seg("Next topic is the hiring plan.", "system", t=13.0 + EchoConfig().window_s + 0.5)
    assert f.add(past, now=101.0) == [mine, past]  # in transcript order


def test_flush_releases_waiting_mic_segments() -> None:
    f = EchoFilter()
    a = seg("I'll draft the agenda for Monday.", "mic", t=10.0)
    b = seg("And I'll book the room as well.", "mic", t=14.0)
    f.add(a, now=100.0)
    f.add(b, now=101.0)
    assert f.flush() == [a, b]
    assert f.flush() == []


def test_short_mic_phrase_is_echo_only_against_a_short_system_line() -> None:
    f = EchoFilter()
    f.add(seg("Sounds good to me.", "system", t=10.0), now=100.0)
    assert f.add(seg("sounds good", "mic", t=10.2), now=100.1) == []
    assert f.dropped == 1

    g = EchoFilter()
    g.add(seg("That sounds good but we need to check the budget with finance first.", "system", t=10.0), now=100.0)
    mine = seg("Sounds good.", "mic", t=11.0)  # the user agreeing, not an echo of the long line
    g.add(mine, now=100.5)
    assert g.flush() == [mine]


def test_short_mic_phrase_needs_every_word_to_match() -> None:
    f = EchoFilter()
    f.add(seg("Sounds good.", "system", t=10.0), now=100.0)
    mine = seg("Sounds great", "mic", t=10.5)
    f.add(mine, now=100.2)
    assert f.flush() == [mine]


def test_disabled_or_inactive_filter_passes_mic_straight_through() -> None:
    mic = seg(MANGLED, "mic", t=10.4)
    off = EchoFilter(cfg=EchoConfig(enabled=False))
    off.add(seg(ASK, "system", t=10.0), now=100.0)
    assert off.add(mic, now=100.5) == [mic]
    no_system = EchoFilter(system_active=False)
    assert no_system.add(mic, now=100.5) == [mic]


def test_other_sources_are_not_filtered() -> None:
    f = EchoFilter()
    f.add(seg(ASK, "system", t=10.0), now=100.0)
    other = seg(ASK, "phone", t=10.0)
    assert f.add(other, now=100.1) == [other]
