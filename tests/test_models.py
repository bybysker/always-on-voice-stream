"""Core data types: ownership checks and meeting ids."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voicestream import models
from voicestream.models import Item, ItemKind, Meeting


def _item(owner: str | None) -> Item:
    return Item(id="A1", kind=ItemKind.ACTION, title="Send the deck", owner=owner)


@pytest.mark.parametrize(
    ("owner", "user_name", "mine"),
    [
        ("Alex", "Alex", True),
        ("alex ", "Alex", True),
        ("Alex Martin", "Alex", False),  # could be a client who shares your first name
        ("Alex Dupont", "Alex Martin", False),
        ("Alex", "Alex Martin", True),  # people say your first name
        ("Alex Martin", "alex martin", True),
        ("Élodie", "Elodie Durand", True),  # accents don't matter
        ("Moi", "Alex", True),
        ("me", "Alex", True),
        ("Myself", "", True),
        ("Sam", "Alex", False),
        ("Sam", "", False),
        (None, "Alex", False),
        ("", "", False),  # nobody's, even with no user name configured
        ("   ", "Alex", False),  # used to raise IndexError (hand-edited or imported meetings)
        ("Alex", "   ", False),
        ("   ", "   ", False),
    ],
)
def test_is_mine(owner: str | None, user_name: str, mine: bool) -> None:
    assert _item(owner).is_mine(user_name) is mine


def test_meetings_started_in_the_same_second_get_distinct_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    second = datetime(2026, 10, 14, 15, 30, 0, tzinfo=UTC)
    ticks = iter(range(10))
    monkeypatch.setattr(models, "utcnow", lambda: second + timedelta(microseconds=next(ticks)))
    first, other = Meeting(), Meeting()
    assert first.id != other.id
    assert first.id.startswith("20261014-153000")  # still sorts by start time
    assert sorted([other.id, first.id]) == [first.id, other.id]
