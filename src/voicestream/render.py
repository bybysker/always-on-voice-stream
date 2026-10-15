"""Markdown → HTML for e-mail bodies (meeting recaps, follow-up drafts).

The Markdown comes from the transcript and the model, so it's untrusted. Every piece
of input text goes through :func:`html.escape` before it's emitted, and the only tags
in the output are the fixed ones written here: no input can produce a tag, an
attribute or an event handler. Links survive only for ``http(s)://`` and ``mailto:``
URLs; anything else (``javascript:``, ``data:``…) keeps its label as plain text.

It covers what the notes use: ``#`` headings, ``-``/``*``/``1.`` lists (nested by
indentation), ``- [ ]``/``- [x]`` checklists, ``>`` quotes, fenced code, ``---``,
``**bold**``, ``*italic*``/``_italic_``, ``~~strike~~``, ``code`` and links. Lines of
a paragraph keep their breaks (e-mail sign-offs rely on them). No dependencies.
"""

from __future__ import annotations

import re
from html import escape

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_CHECK = re.compile(r"^\[([ xX])\]\s+")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TOKEN = re.compile(r"`([^`\n]+)`|\[([^\]\n]+)\]\(([^()\s]+)\)")
_SAFE_URL = re.compile(r"(?i)^(?:https?://|mailto:)[^\s<>\"'`]+$")
_EMPHASIS = [
    (re.compile(r"\*\*(\S(?:.*?\S)?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"__(\S(?:.*?\S)?)__"), r"<strong>\1</strong>"),
    (re.compile(r"~~(\S(?:.*?\S)?)~~"), r"<del>\1</del>"),
    (re.compile(r"(?<![\w*])\*(\S(?:.*?\S)?)\*(?![\w*])"), r"<em>\1</em>"),
    (re.compile(r"(?<!\w)_(\S(?:.*?\S)?)_(?!\w)"), r"<em>\1</em>"),
]
_BOXES = {" ": "☐", "x": "☑", "X": "☑"}


def markdown_to_html(md: str) -> str:
    """Render ``md`` as an HTML fragment that is safe to put in an e-mail body."""
    out: list[str] = []
    para: list[str] = []
    quote: list[str] = []
    lists: list[tuple[int, str]] = []  # open lists: (indent, "ul" | "ol"), each with an open <li>
    fence: list[str] | None = None

    def flush() -> None:
        if para:
            out.append("<p>" + "<br>\n".join(para) + "</p>")
            para.clear()
        if quote:
            out.append("<blockquote><p>" + "<br>\n".join(quote) + "</p></blockquote>")
            quote.clear()
        while lists:
            out.append(f"</li></{lists.pop()[1]}>")

    for line in md.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if fence is not None:
            if _FENCE.match(line):
                out.append("<pre><code>" + escape("\n".join(fence)) + "</code></pre>")
                fence = None
            else:
                fence.append(line)
            continue
        if _FENCE.match(line):
            flush()
            fence = []
        elif not line.strip():
            flush()
        elif m := _HEADING.match(line):
            flush()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif _RULE.match(line):
            flush()
            out.append("<hr>")
        elif line.lstrip().startswith(">"):
            if not quote:
                flush()
            quote.append(_inline(line.lstrip()[1:].strip()))
        elif m := _ITEM.match(line):
            if not lists:
                flush()
            indent, tag, text = len(m.group(1).expandtabs(4)), "ol" if m.group(2)[0].isdigit() else "ul", m.group(3)
            if box := _CHECK.match(text):
                text = f"{_BOXES[box.group(1)]} {text[box.end():]}"
            while lists and (indent < lists[-1][0] or (indent == lists[-1][0] and tag != lists[-1][1])):
                out.append(f"</li></{lists.pop()[1]}>")
            if lists and indent == lists[-1][0]:
                out.append(f"</li><li>{_inline(text)}")
            else:
                out.append(f"<{tag}><li>{_inline(text)}")
                lists.append((indent, tag))
        elif lists and line[:1].isspace():
            out.append(f"<br>{_inline(line.strip())}")  # continuation of the open list item
        else:
            if quote or lists:
                flush()
            para.append(_inline(line.strip()))
    if fence is not None:
        out.append("<pre><code>" + escape("\n".join(fence)) + "</code></pre>")
    flush()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Inline Markdown (code, links, emphasis) → escaped HTML."""
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        out.append(_emphasis(escape(text[pos : m.start()])))
        if m.group(1) is not None:
            out.append(f"<code>{escape(m.group(1))}</code>")
        else:
            label, url = _emphasis(escape(m.group(2))), m.group(3)
            out.append(f'<a href="{escape(url)}">{label}</a>' if _SAFE_URL.match(url) else label)
        pos = m.end()
    out.append(_emphasis(escape(text[pos:])))
    return "".join(out)


def _emphasis(escaped: str) -> str:
    for pattern, repl in _EMPHASIS:
        escaped = pattern.sub(repl, escaped)
    return escaped
