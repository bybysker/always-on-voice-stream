"""A small PDF writer for the documents you download: a meeting's summary and its transcript.

No dependencies (like :mod:`.render`), so nothing more goes into the Mac app. The text is set in the
PDF's standard Helvetica, which every PDF reader has: nothing is embedded, and a summary is a few
kilobytes. Its WinAnsi encoding covers French and English in full — accents, ``œ``, curly quotes,
dashes, ``…`` and ``€``. Any other character becomes its plain letter (``ł`` → ``l``, ``ő`` → ``o``)
or, failing that, ``?``: a name from elsewhere never stops a download.

A :class:`Document` is filled from top to bottom (a title, headings, paragraphs, bullets, a to-do
table, transcript lines) and lays itself out on A4 pages as it goes: lines wrap on real character
widths (Adobe's metrics for Helvetica), a heading never ends a page, a table's header is repeated
on the next page, and each page is numbered in its footer ("Page 2 of 3").
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

A4 = (595.28, 841.89)
"""Points (1/72 inch)."""

# Adobe's widths for codes 32-255 in WinAnsiEncoding (1/1000 of the font size). The oblique faces
# have the upright ones' widths.
_REGULAR = """\
278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556
278 278 584 584 584 556 1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667
611 722 667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 556 222 222 500 222 833
556 556 556 556 333 500 278 556 500 722 500 500 500 334 260 334 584 350 556 350 222 556 333 1000 556 556
333 1000 667 333 1000 350 611 350 350 222 222 333 333 350 556 1000 333 1000 500 333 944 350 500 667 278
333 556 556 556 556 260 556 333 737 370 556 584 333 737 333 400 584 333 333 333 556 537 278 333 333 365
556 834 834 834 611 667 667 667 667 667 667 1000 722 667 667 667 667 278 278 278 278 722 722 778 778 778
778 778 584 778 722 722 722 722 667 667 611 556 556 556 556 556 556 889 500 556 556 556 556 278 278 278
278 556 556 556 556 556 556 556 584 611 556 556 556 556 500 556 500
"""
_BOLD = """\
278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556
333 333 584 584 584 611 975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667
611 722 667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 611 278 278 556 278 889
611 611 611 611 389 556 333 611 556 778 556 556 500 389 280 389 584 350 556 350 278 556 500 1000 556 556
333 1000 667 333 1000 350 611 350 350 278 278 500 500 350 556 1000 333 1000 556 333 944 350 500 667 278
333 556 556 556 556 280 556 333 737 370 556 584 333 737 333 400 584 333 333 333 611 556 278 333 333 365
556 834 834 834 611 722 722 722 722 722 722 1000 722 667 667 667 667 278 278 278 278 722 722 778 778 778
778 778 584 778 722 722 722 722 667 667 611 556 556 556 556 556 556 889 556 556 556 556 556 278 278 278
278 611 611 611 611 611 611 611 584 611 611 611 611 611 556 611 556
"""
_WIDTHS = {False: [0] * 32 + [int(w) for w in _REGULAR.split()], True: [0] * 32 + [int(w) for w in _BOLD.split()]}

FONTS = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Helvetica-Oblique", "F4": "Helvetica-BoldOblique"}

Color = tuple[float, float, float]


def _rgb(hex_color: str) -> Color:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


# the app's own colours (dashboard/static/app.css): near-black text, one blue, light rules
TEXT = _rgb("#141414")
MUTED = _rgb("#5f5f5f")
HUE = _rgb("#1d5fbf")
RULE = _rgb("#dcdcdc")

# characters WinAnsi lacks that have an obvious stand-in (others: their letter without its accent)
_STAND_INS = {chr(code): sub for code, sub in {
    0x0142: "l", 0x0141: "L", 0x0111: "d", 0x0110: "D", 0x0131: "i", 0x0127: "h", 0x0126: "H", 0x0167: "t", 0x0166: "T",
    0x2010: "-", 0x2011: "-", 0x2012: chr(0x2013), 0x2015: chr(0x2014), 0x2212: "-", 0x2032: "'", 0x2033: '"',
    0x202F: chr(0xA0), 0x2007: chr(0xA0),  # the narrow no-break space of French typography: still no break
    0x2192: "->", 0x2190: "<-", 0x2264: "<=", 0x2265: ">=", 0x2260: "!=", 0x2248: "~",
}.items()}  # fmt: skip
_SPACES = re.compile("[" + "".join(re.escape(chr(c)) for c in (
    0x20, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x85, 0x1680, *range(0x2000, 0x2007), 0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x205F, 0x3000,
)) + "]+")
"""White space that is only a gap between words (the non-breaking ones are kept)."""


def clean(text: str) -> str:
    """``text`` as Helvetica can set it: every character in WinAnsi, white space as single spaces (kept
    at its ends: a span's text runs on into the next span's)."""
    out: list[str] = []
    for ch in _SPACES.sub(" ", text):
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            sub = _STAND_INS.get(ch)
            if sub is None:
                sub = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            try:
                sub.encode("cp1252")
            except UnicodeEncodeError:
                # a symbol or an emoji goes; a letter from another script keeps its place
                sub = "" if unicodedata.category(ch)[0] in "SC" else "?"
            out.append(sub)
        else:
            out.append(ch)
    return _DOUBLE_SPACE.sub(" ", "".join(out))  # where an emoji was between two words


_DOUBLE_SPACE = re.compile(" {2,}")


def text_width(text: str, size: float, bold: bool = False) -> float:
    """The width of already :func:`clean` text, in points."""
    w = _WIDTHS[bold]
    return sum(w[b] for b in text.encode("cp1252")) * size / 1000


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    color: Color = TEXT

    @property
    def font(self) -> str:
        return {(False, False): "F1", (True, False): "F2", (False, True): "F3", (True, True): "F4"}[(self.bold, self.italic)]


_BOLD_MARK = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*")


def spans(text: str, *, color: Color = TEXT, italic: bool = False) -> list[Span]:
    """Plain text as spans, ``**bold**`` set bold (the model sometimes writes it)."""
    out: list[Span] = []
    pos = 0
    for m in _BOLD_MARK.finditer(text):
        if m.start() > pos:
            out.append(Span(text[pos : m.start()], italic=italic, color=color))
        out.append(Span(m.group(1), bold=True, italic=italic, color=color))
        pos = m.end()
    if pos < len(text):
        out.append(Span(text[pos:], italic=italic, color=color))
    return out


Line = list[Span]


def wrap(parts: Sequence[Span], width: float, size: float) -> list[Line]:
    """Greedy line breaking at spaces (a word wider than ``width`` is cut). Text must be :func:`clean`."""
    words: list[tuple[str, Span]] = []  # tokens: a word or a single space, with its span's style
    for sp in parts:
        for tok in re.split(r"( )", clean(sp.text)):
            if tok:
                words.append((tok, sp))
    lines: list[Line] = []
    cur: list[tuple[str, Span]] = []
    cur_w = 0.0

    def push() -> None:
        nonlocal cur, cur_w
        while cur and cur[-1][0] == " ":
            cur.pop()
        if cur:
            lines.append(_merge(cur))
        cur, cur_w = [], 0.0

    for tok, sp in words:
        w = text_width(tok, size, sp.bold)
        if tok == " ":
            if cur:
                cur.append((tok, sp))
                cur_w += w
            continue
        if w > width:  # a word wider than a whole line (a URL): cut, its first piece on the line begun
            if cur and width - cur_w < width / 4:
                push()
            for piece in _cut(tok, width - cur_w, width, size, sp.bold):
                piece_w = text_width(piece, size, sp.bold)
                if cur and cur_w + piece_w > width:
                    push()
                cur.append((piece, sp))
                cur_w += piece_w
        elif cur_w + w <= width or not cur:
            cur.append((tok, sp))
            cur_w += w
        else:
            push()
            cur.append((tok, sp))
            cur_w = w
    push()
    return lines


def _cut(word: str, first: float, width: float, size: float, bold: bool) -> list[str]:
    pieces, cur, room = [], "", first
    for ch in word:
        if cur and text_width(cur + ch, size, bold) > room:
            pieces.append(cur)
            cur, room = "", width
        cur += ch
    if cur:
        pieces.append(cur)
    return pieces


def _merge(tokens: list[tuple[str, Span]]) -> Line:
    out: Line = []
    for tok, sp in tokens:
        if out and (out[-1].bold, out[-1].italic, out[-1].color) == (sp.bold, sp.italic, sp.color):
            out[-1] = Span(out[-1].text + tok, sp.bold, sp.italic, sp.color)
        else:
            out.append(Span(tok, sp.bold, sp.italic, sp.color))
    return out


def _pdf_string(text: str) -> bytes:
    raw = text.encode("cp1252")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _info_string(text: str) -> bytes:
    """A document-information string: UTF-16 with its byte order mark, so a title keeps every character."""
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode() + b">"


def _num(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s if s != "-0" else "0"


def _color(c: Color, stroke: bool = False) -> str:
    return f"{_num(c[0])} {_num(c[1])} {_num(c[2])} {'RG' if stroke else 'rg'}"


@dataclass
class Column:
    title: str
    width: float
    """Points; ``0``: whatever the others leave."""


class Document:
    """A PDF filled from top to bottom. ``footer``: the text at the foot of every page (the title and
    date), ``page_label``: its page number, with ``{n}`` and ``{total}``."""

    def __init__(
        self,
        *,
        title: str,
        author: str = "",
        subject: str = "",
        lang: str = "en",
        footer: str = "",
        page_label: str = "Page {n} of {total}",
        created: datetime | None = None,
        page_size: tuple[float, float] = A4,
        margin: float = 56.0,
    ) -> None:
        self.meta = {"title": title, "author": author, "subject": subject}
        self.lang = lang
        self.footer = clean(footer)
        self.page_label = page_label
        self.created = created or datetime.now(timezone.utc)
        self.w, self.h = page_size
        self.margin = margin
        self.left, self.right = margin, self.w - margin
        self.top, self.bottom = self.h - margin, margin + 22  # room for the footer
        self.pages: list[list[str]] = []
        self.y = 0.0
        self._new_page()

    # ------------------------------------------------------------------ layout

    @property
    def width(self) -> float:
        return self.right - self.left

    def _new_page(self) -> None:
        self.pages.append([])
        self.y = self.top

    def _ensure(self, height: float) -> None:
        """Start a new page unless ``height`` points still fit on this one (a fresh page takes anything)."""
        if self.y - height < self.bottom and self.y < self.top:
            self._new_page()

    def _op(self, op: str) -> None:
        self.pages[-1].append(op)

    def _text_line(self, line: Line, x: float, y: float, size: float) -> None:
        ops = [f"BT {_num(x)} {_num(y)} Td"]
        font = color = None
        for sp in line:
            if sp.font != font:
                ops.append(f"/{sp.font} {_num(size)} Tf")
                font = sp.font
            if sp.color != color:
                ops.append(_color(sp.color))
                color = sp.color
            ops.append(_pdf_string(sp.text).decode("latin-1") + " Tj")
        ops.append("ET")
        self._op(" ".join(ops))

    def _rule(self, x1: float, x2: float, y: float, color: Color = RULE, width: float = 0.6) -> None:
        self._op(f"{_color(color, stroke=True)} {_num(width)} w {_num(x1)} {_num(y)} m {_num(x2)} {_num(y)} l S")

    def _lines(self, lines: list[Line], x: float, size: float, leading: float) -> None:
        for line in lines:
            self._ensure(leading)
            self.y -= leading
            self._text_line(line, x, self.y + (leading - size) * 0.45, size)

    # ---------------------------------------------------------------- content

    def space(self, points: float) -> None:
        self.y = max(self.bottom, self.y - points) if self.y < self.top else self.y

    def title(self, text: str, kicker: str = "") -> None:
        """The document's title, under a small blue ``kicker`` ("MEETING SUMMARY")."""
        if kicker:
            self._lines(wrap([Span(kicker.upper(), bold=True, color=HUE)], self.width, 8.5), self.left, 8.5, 12)
            self.space(4)
        self._lines(wrap([Span(text, bold=True)], self.width, 20), self.left, 20, 25)

    def meta_line(self, text: str) -> None:
        self.space(2)
        self._lines(wrap([Span(text, color=MUTED)], self.width, 9.5), self.left, 9.5, 14)

    def separator(self) -> None:
        self.space(12)
        self._rule(self.left, self.right, self.y)
        self.space(6)

    def heading(self, text: str) -> None:
        """A section's heading: kept with at least two lines of what follows it."""
        self.space(14)
        self._ensure(16 + 2 * 15)
        self._lines(wrap([Span(text, bold=True)], self.width, 12), self.left, 12, 16)
        self.space(3)

    def paragraph(self, text: str | Sequence[Span], *, size: float = 10.5, color: Color = TEXT, after: float = 4) -> None:
        parts = spans(text, color=color) if isinstance(text, str) else text
        self._lines(wrap(parts, self.width, size), self.left, size, size * 1.45)
        self.space(after)

    def bullets(self, items: Iterable[str], *, size: float = 10.5) -> None:
        indent = 14.0
        leading = size * 1.45
        for item in items:
            lines = wrap(spans(item), self.width - indent, size)
            if not lines:
                continue
            self._ensure(leading)
            self._text_line([Span("•", color=HUE)], self.left + 2, self.y - leading + (leading - size) * 0.45, size)
            self._lines(lines, self.left + indent, size, leading)
            self.space(2.5)

    def checklist(self, columns: Sequence[Column], rows: Sequence[tuple[bool, Sequence[str]]], *, size: float = 10) -> None:
        """A to-do table: a box (ticked when done) in front of each row, ``columns`` after it."""
        box = 16.0
        flex = self.width - box - sum(c.width for c in columns)
        widths = [c.width or flex for c in columns]
        xs = [self.left + box]
        for w in widths[:-1]:
            xs.append(xs[-1] + w)
        pad, leading = 6.0, size * 1.4

        def header() -> None:
            self._ensure(14 + leading + 2 * pad)
            self.y -= 12
            for col, x in zip(columns, xs, strict=True):
                self._text_line([Span(col.title.upper(), bold=True, color=MUTED)], x, self.y + 2, 7.5)
            self.y -= 4
            self._rule(self.left, self.right, self.y, color=TEXT, width=0.6)

        header()
        for done, cells in rows:
            wrapped = [wrap(spans(cell, color=MUTED if done and i == 0 else TEXT), w - 8, size) or [[Span("")]]
                       for i, (cell, w) in enumerate(zip(cells, widths, strict=True))]
            height = max(len(c) for c in wrapped) * leading + 2 * pad
            if self.y - height < self.bottom:
                self._new_page()
                header()
            top = self.y
            # the box, level with the first line
            by = top - pad - leading + (leading - size) * 0.45 - 0.5
            self._op(f"{_color(MUTED, stroke=True)} 0.8 w {_num(self.left + 1)} {_num(by)} 8.5 8.5 re S")
            if done:
                cx, cy = self.left + 1, by
                self._op(f"{_color(HUE, stroke=True)} 1.4 w {_num(cx + 1.8)} {_num(cy + 4.4)} m {_num(cx + 3.7)} "
                         f"{_num(cy + 2.2)} l {_num(cx + 7.2)} {_num(cy + 7)} l S")
            for lines, x in zip(wrapped, xs, strict=True):
                self.y = top - pad
                self._lines(lines, x, size, leading)
            self.y = top - height
            self._rule(self.left, self.right, self.y)

    def transcript_line(self, stamp: str, speaker: str, text: str, *, size: float = 9.5) -> None:
        """``[12:04]  Julie  what she said``, the words wrapped under themselves."""
        gutter = 40.0
        leading = size * 1.42
        lines = wrap([Span(speaker + "\xa0 ", bold=True), Span(text)], self.width - gutter, size)
        if not lines:
            return
        self._ensure(leading)
        self._text_line([Span(stamp, color=MUTED)], self.left, self.y - leading + (leading - size) * 0.45, size - 1)
        self._lines(lines, self.left + gutter, size, leading)
        self.space(3)

    # ------------------------------------------------------------------ output

    def to_bytes(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")  # filled in once the pages are known
        pages = add(b"")
        fonts = {key: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode())
                 for key, name in FONTS.items()}
        resources = "<< /Font << " + " ".join(f"/{k} {n} 0 R" for k, n in fonts.items()) + " >> >>"
        total = len(self.pages)
        kids = []
        for n, ops in enumerate(self.pages, start=1):
            content = "\n".join([*ops, *self._footer_ops(n, total)]).encode("latin-1")
            data = zlib.compress(content, 9)
            stream = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
            kids.append(add(
                f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {_num(self.w)} {_num(self.h)}] "
                f"/Resources {resources} /Contents {stream} 0 R >>".encode()
            ))
        objects[pages - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {total} >>".encode()
        lang = re.sub(r"[^A-Za-z-]", "", self.lang)[:12] or "en"
        objects[catalog - 1] = (f"<< /Type /Catalog /Pages {pages} 0 R /Lang ({lang}) "
                                "/ViewerPreferences << /DisplayDocTitle true >> >>").encode()
        stamp = self.created.astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        info_parts = [b"/Producer " + _info_string("Voicestream"), b"/CreationDate (" + stamp.encode() + b")"]
        for key, value in (("Title", self.meta["title"]), ("Author", self.meta["author"]), ("Subject", self.meta["subject"])):
            if value:
                info_parts.append(f"/{key} ".encode() + _info_string(value))
        info = add(b"<< " + b" ".join(info_parts) + b" >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)

    def _footer_ops(self, n: int, total: int) -> list[str]:
        y = self.margin - 4
        ops = [f"{_color(RULE, stroke=True)} 0.5 w {_num(self.left)} {_num(y + 12)} m {_num(self.right)} {_num(y + 12)} l S"]
        label = clean(self.page_label.format(n=n, total=total))
        right = self.right - text_width(label, 8)
        room = right - self.left - 16
        footer = self.footer
        while footer and text_width(footer, 8) > room:  # a long title: shortened, never over the page number
            footer = footer[:-2].rstrip() + "…" if len(footer) > 2 else ""
        parts = [(footer, self.left), (label, right)] if footer else [(label, right)]
        for text, x in parts:
            ops.append(f"BT {_num(x)} {_num(y)} Td /F1 8 Tf {_color(MUTED)} {_pdf_string(text).decode('latin-1')} Tj ET")
        return ops
