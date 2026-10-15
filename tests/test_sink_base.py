from __future__ import annotations

from voicestream.config import Config
from voicestream.models import Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import Sink, SinkConfig, item_markdown


class _Sink(Sink):
    name = "test"

    async def push_item(self, item, meeting):  # type: ignore[override]
        return "ref"


def _item(**kw: object) -> Item:
    base: dict[str, object] = dict(id="A1", kind=ItemKind.ACTION, title="Envoyer le devis", owner="Alex",
                                   due_date="2026-10-16", due_text="d'ici vendredi", confidence=0.9)
    return Item(**{**base, **kw})


def test_item_markdown_follows_the_meeting_language() -> None:
    fr = item_markdown(_item(evidence="je l'envoie d'ici vendredi"), Meeting(title="Point Acme", language="fr"))
    assert "**Responsable:** Alex" in fr and "**Échéance:** 2026-10-16 (d'ici vendredi)" in fr
    assert fr.endswith("_Capturé par voicestream depuis Point Acme._")
    en = item_markdown(_item(), Meeting(title="Acme sync", language="en"))
    assert "**Owner:** Alex" in en and en.endswith("_Captured by voicestream from Acme sync._")
    assert "**Owner:**" in item_markdown(_item(), Meeting())  # unknown language → English


def test_cancelled_items_are_never_created(cfg: Config) -> None:
    sink = _Sink(SinkConfig(enabled=True), cfg)
    assert sink.accepts(_item(), 0.6)
    assert not sink.accepts(_item(status=ItemStatus.CANCELLED), 0.6)
    assert not sink.accepts(_item(dismissed=True), 0.6)
    assert sink.accepts(_item(status=ItemStatus.DONE), 0.6)  # completed during the meeting: still worth logging
