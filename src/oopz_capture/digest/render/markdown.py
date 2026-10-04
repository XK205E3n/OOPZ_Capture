"""Literal Markdown for exactly the visible text blocks, plus the inverse parser.

Heading markers (# .. ####) and blank lines are the only Markdown structure. Every
character of every block is escaped so untrusted names/text cannot become HTML,
links, images, tables, emphasis or code. ``escape_literal`` produces the same
bytes as the upstream ``oopz_capture.markdown_literal`` exporter (tested).
"""
from __future__ import annotations

import html
import re
from typing import Iterable, Mapping

_PUNCT = re.compile(r"([\\`*_{}\[\]()#+.!|~=:/@-])")
_UNPUNCT = re.compile(r"\\([\\`*_{}\[\]()#+.!|~=:/@-])")
_HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "p": ""}


def escape_literal(text: str) -> str:
    return _PUNCT.sub(r"\\\1", html.escape(text, quote=False))


def unescape_literal(text: str) -> str:
    return html.unescape(_UNPUNCT.sub(r"\1", text))


def blocks_to_markdown(blocks: Iterable[Mapping]) -> str:
    paras = [_HEADING[b["md"]] + escape_literal(b["text"]) for b in blocks]
    return "\n\n".join(paras) + "\n"


def markdown_to_blocks(md: str) -> list[tuple[str, str]]:
    """Inverse of ``blocks_to_markdown``: [(md_level, text), ...]."""
    out = []
    for para in md.rstrip("\n").split("\n\n"):
        m = re.match(r"(#{1,4}) (.*)\Z", para, re.S)
        if m:
            out.append((f"h{len(m.group(1))}", unescape_literal(m.group(2))))
        else:
            out.append(("p", unescape_literal(para)))
    return out
