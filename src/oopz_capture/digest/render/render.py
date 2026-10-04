"""Public render entry point: ``render_digest_card``.

view -> measured layout (height known up front) -> raster -> in-memory PNG.
Hard limits are checked before anything is written; a failure raises
``RenderError`` and leaves every input untouched (nothing is truncated, shrunk or
split into several images).
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .avatar_files import load_avatars
from .layout import LayoutResult, build_layout
from .markdown import blocks_to_markdown, markdown_to_blocks
from .paint import Painter
from .textlayout import FontBook, FontSpec, RenderError
from .tokens import FONT_DIR, load_tokens
from .view import build_view, check_module_order, load_labels

MAX_CANVAS_PIXELS = 60_000_000   # supersampled pixels; ~180 MB of RGB at the 12000 px limit


@dataclass
class RenderResult:
    path: Path | None
    width: int
    height: int
    byte_count: int
    png: bytes
    markdown: str
    text_runs: int
    header_height: int
    visible_text: list[str]
    blocks: list[dict]
    modules: list[dict]
    min_font_px: float
    sha256: str
    layout_fingerprint: str
    warnings: list[str] = field(default_factory=list)
    drawn: list[dict] = field(default_factory=list)


def verify_text_consistency(blocks: Sequence[Mapping], drawn: Sequence[Mapping], markdown: str) -> None:
    """The PNG's drawn strings, the block list and the Markdown must say the same thing."""
    squash = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    by_block: dict[int, list[str]] = {}
    for d in drawn:
        by_block.setdefault(d["block"], []).append(d["text"])
    for b in blocks:
        got = by_block.get(b["id"])
        if not got or squash("".join(got)) != squash(b["text"]):
            raise RenderError("text_mismatch:png", f"block {b['id']} ({b['role']})")
    if set(by_block) - {b["id"] for b in blocks}:
        raise RenderError("text_mismatch:png", "drawn text without a block")
    parsed = markdown_to_blocks(markdown)
    if len(parsed) != len(blocks) or any(t != b["text"] or lvl != b["md"] for (lvl, t), b in zip(parsed, blocks)):
        raise RenderError("text_mismatch:markdown")


def render_digest_card(digest: Mapping, metadata: Mapping, output_path: str | Path | None = None, *,
                       width: int | None = None, max_height: int | None = None, scale: int | None = None,
                       regular_font: str | Path | None = None, bold_font: str | Path | None = None,
                       stats_rows: Sequence[Mapping] = (), stats_view: Mapping | None = None,
                       avatars: Mapping[str, str | Path] | None = None,
                       markdown_path: str | Path | None = None, tokens: Mapping | None = None) -> RenderResult:
    """Render one digest. ``digest`` is the validated ``oopz.digest.content.v2`` object.

    ``stats_view`` is the trusted, already formatted statistics block (see
    ``stats_view.build_stats_view``); ``stats_rows`` is the older, flatter form.
    """
    tk = dict(tokens) if tokens else load_tokens(width=width, max_height=max_height, scale=scale)
    if tokens and (width or max_height or scale):
        tk = load_tokens(width=width or tokens["canvas"]["width"], max_height=max_height,
                         scale=scale or tokens["canvas"]["scale"])
    labels = load_labels()
    if stats_view is None and stats_rows:
        stats_view = {"status": "available", "simulated": any(r.get("tag") for r in stats_rows), "notice": None,
                      "scope_note": None, "rows": [dict(r) for r in stats_rows], "method_note": None}
    view = build_view(digest, metadata, stats_view=stats_view, labels=labels)
    check_module_order(view)
    fonts = FontBook(FontSpec.from_tokens(tk, FONT_DIR, regular_font, bold_font), int(tk["canvas"]["scale"]))
    layout: LayoutResult = build_layout(view, tk, fonts)            # raises height_exceeded / missing_glyph
    S = int(tk["canvas"]["scale"])
    if layout.width * S * layout.height * S > MAX_CANVAS_PIXELS:
        raise RenderError("canvas_pixel_budget", "supersampled canvas would exceed the memory budget")
    images, warnings = load_avatars(avatars, {g["speaker_id"] for g in view["people"]["groups"]})
    painter = Painter(layout, tk, fonts, images)
    img = painter.paint()
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=9)
    png = buf.getvalue()
    limit = int(tk["canvas"]["png_byte_limit"])
    if len(png) > limit:
        raise RenderError("png_too_large", f"{len(png)} bytes > {limit}", bytes=len(png), limit=limit)
    markdown = blocks_to_markdown(layout.blocks)
    verify_text_consistency(layout.blocks, painter.drawn, markdown)
    warnings = warnings + layout.warnings + [f"glyph_substituted:U+{cp:04X}" for cp, n in sorted(fonts.substituted.items())]
    result = RenderResult(
        path=Path(output_path) if output_path else None, width=layout.width, height=layout.height,
        byte_count=len(png), png=png, markdown=markdown, text_runs=len(painter.drawn),
        header_height=layout.header_height, visible_text=[b["text"] for b in layout.blocks], blocks=layout.blocks,
        modules=layout.modules, min_font_px=layout.min_font_px, sha256=hashlib.sha256(png).hexdigest(),
        layout_fingerprint=layout.fingerprint(), warnings=warnings, drawn=painter.drawn)
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(png)
    if markdown_path:
        Path(markdown_path).parent.mkdir(parents=True, exist_ok=True)
        Path(markdown_path).write_text(markdown, encoding="utf-8", newline="\n")
    return result
