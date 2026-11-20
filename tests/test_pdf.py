"""The PDF writer (voicestream/pdf.py): read back with a few lines of parsing, no PDF library."""

from __future__ import annotations

import re
import zlib
from datetime import datetime, timezone

from voicestream.pdf import Column, Document, Span, clean, spans, text_width, wrap


def objects(pdf: bytes) -> dict[int, bytes]:
    """Every object by number, checked against the cross-reference table (what a reader trusts)."""
    assert pdf.startswith(b"%PDF-1.4\n") and pdf.rstrip().endswith(b"%%EOF")
    xref_at = int(re.search(rb"startxref\n(\d+)\n%%EOF", pdf).group(1))
    assert pdf[xref_at:].startswith(b"xref\n")
    count = int(re.match(rb"xref\n0 (\d+)\n", pdf[xref_at:]).group(1))
    rows = pdf[xref_at:].split(b"\n")[2 : 2 + count]
    out = {}
    for n, row in enumerate(rows[1:], start=1):
        offset = int(row[:10])
        assert pdf[offset:].startswith(f"{n} 0 obj\n".encode()), n  # each offset is exact
        out[n] = pdf[offset : pdf.index(b"\nendobj\n", offset)]
    return out


def page_texts(pdf: bytes) -> list[str]:
    """Each page's text, in drawing order (WinAnsi decoded)."""
    objs = objects(pdf)
    pages = []
    for body in objs.values():
        if b"/Type /Page " in body:
            content = int(re.search(rb"/Contents (\d+) 0 R", body).group(1))
            raw = objs[content]
            data = zlib.decompress(raw[raw.index(b"stream\n") + 7 : raw.rindex(b"\nendstream")])
            strings = re.findall(rb"\(((?:\\.|[^\\)])*)\) Tj", data)
            pages.append(" ".join(re.sub(rb"\\(.)", rb"\1", s).decode("cp1252") for s in strings))
    return pages


def test_helvetica_widths_are_adobes() -> None:
    assert text_width(" ", 1000) == 278 and text_width("a", 1000) == 556 and text_width("M", 1000) == 833
    assert text_width("i", 1000) == 222 and text_width("i", 1000, bold=True) == 278
    assert text_width("é—€", 1000) == 556 + 1000 + 556  # WinAnsi's own codes for the accents, dashes, euro
    assert text_width("Bonjour", 10) == text_width("Bonjour", 1000) / 100


def test_characters_outside_winansi_never_stop_a_document() -> None:
    assert clean("Œuvre « déjà » — 1 000 € … ’") == "Œuvre « déjà » — 1 000 € … ’"  # all of French
    assert clean("Łukasz Wróbel, Dvořák, Ősz") == "Lukasz Wróbel, Dvorák, Osz"  # the letter, without the accent WinAnsi lacks
    assert clean("le 18 nov. : lancement") == "le 18 nov.\xa0: lancement"  # French spacing still doesn't break
    assert clean("ok 🚀 go") == "ok go"  # an emoji goes, with its space
    assert clean("Иван") == "????"  # another script: its place kept
    assert clean("a\n\tb  c ") == "a b c "  # white space: single spaces, the ends kept
    for text in ("Łukasz 🚀 ж ‐ ≤", "\x00\x07 tab\there"):
        clean(text).encode("cp1252")  # whatever came in, it can be set


def test_lines_wrap_at_spaces_and_keep_the_space_between_spans() -> None:
    lines = wrap(spans("les textes arrivent **vendredi**, et la mise en ligne est confirmée"), 120, 10)
    text = [" ".join("".join(s.text for s in line).split()) for line in lines]
    assert " ".join(text) == "les textes arrivent vendredi, et la mise en ligne est confirmée"
    assert all(sum(text_width(s.text, 10, s.bold) for s in line) <= 120 for line in lines)
    bold = [s.text for line in lines for s in line if s.bold]
    assert bold == ["vendredi"]
    # a span ending in a space runs on into the next one: never "arriventvendredi"
    joined = "".join(s.text for line in wrap(spans("arrivent **vendredi**"), 500, 10) for s in line)
    assert joined == "arrivent vendredi"


def test_a_word_wider_than_the_line_is_cut() -> None:
    url = "https://example.com/" + "a" * 200
    lines = wrap([Span("see " + url)], 100, 10)
    assert len(lines) > 3 and "".join(s.text for line in lines for s in line) == "see " + url
    assert all(text_width("".join(s.text for s in line), 10) <= 100 for line in lines)


def test_a_document_is_a_valid_pdf_with_its_title_and_page_numbers() -> None:
    doc = Document(title="Lancement v2 — Acme", author="Alex Martin", subject="Compte rendu", lang="fr",
                   footer="Lancement v2 · 4 novembre 2026", page_label="Page {n} sur {total}",
                   created=datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc))
    doc.title("Lancement v2 (Acme)", kicker="Compte rendu")
    doc.meta_line("mercredi 4 novembre 2026 · 42 min")
    doc.heading("Points abordés")
    doc.bullets([f"Point {i} : les textes (relus) arrivent \\ vendredi" for i in range(80)])
    pdf = doc.to_bytes()
    objs = objects(pdf)
    texts = page_texts(pdf)
    assert len(texts) == len(doc.pages) >= 2
    assert re.search(rb"/Count %d\b" % len(texts), b"".join(objs.values()))
    assert texts[0].startswith("COMPTE RENDU Lancement v2 (Acme) mercredi 4 novembre 2026")
    assert "Point 0 : les textes (relus) arrivent \\ vendredi" in texts[0]  # parentheses and backslash escaped
    for n, text in enumerate(texts, start=1):
        assert text.endswith(f"Lancement v2 · 4 novembre 2026 Page {n} sur {len(texts)}")
    info = next(body for body in objs.values() if b"/Producer" in body)
    title = re.search(rb"/Title <FEFF([0-9A-F]+)>", info).group(1)
    assert bytes.fromhex(title.decode()).decode("utf-16-be") == "Lancement v2 — Acme"  # any character, in the metadata
    assert b"/CreationDate (D:20261104140000Z)" in info
    assert b"/Lang (fr)" in pdf and pdf == doc.to_bytes()  # the same document, the same bytes


def test_a_heading_is_never_the_last_line_of_a_page() -> None:
    doc = Document(title="t")
    while doc.y > doc.bottom + 40:  # fill the page until a heading and two lines no longer fit
        doc.paragraph("filler line")
    doc.heading("Next steps")
    doc.paragraph("first step")
    texts = page_texts(doc.to_bytes())
    assert "Next steps" not in texts[0] and texts[1].startswith("Next steps first step")


def test_a_checklist_repeats_its_header_on_each_page_and_ticks_done_rows() -> None:
    doc = Document(title="t")
    rows = [(i % 3 == 0, [f"Task number {i} with a longer title that wraps onto a second line of its cell", "Julie Dupont", "Fri 13 Nov"])
            for i in range(40)]
    doc.checklist([Column("Task", 0), Column("Owner", 110), Column("Due", 80)], rows)
    pdf = doc.to_bytes()
    texts = page_texts(pdf)
    assert len(texts) >= 2 and all(text.startswith("TASK OWNER DUE") for text in texts)
    assert sum(text.count("Julie Dupont") for text in texts) == 40  # no row lost or split across pages
    content = b"".join(zlib.decompress(m) for m in re.findall(rb"stream\n(.*?)\nendstream", pdf, re.S))
    assert content.count(b" re S") == 40 and content.count(b" l S") > 40  # a box per row, a tick in done ones


def test_transcript_lines_hang_under_the_words() -> None:
    doc = Document(title="t")
    doc.transcript_line("01:05", "Julie", "word " * 60)
    pdf = doc.to_bytes()
    text = page_texts(pdf)[0]
    assert text.startswith("01:05 Julie\xa0  word word")  # (spans joined with a space here)
    data = zlib.decompress(re.search(rb"stream\n(.*?)\nendstream", pdf, re.S).group(1))
    xs = [float(x) for x in re.findall(rb"BT ([\d.]+) [\d.]+ Td", data)]
    assert xs[0] == doc.left and set(xs[1:-2]) == {doc.left + 40}  # the stamp, then every line of words indented
