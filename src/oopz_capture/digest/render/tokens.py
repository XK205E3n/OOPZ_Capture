"""Design-token loading and validation (``design/tokens.json``)."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from .textlayout import RenderError

ROOT = Path(__file__).resolve().parent
# Fonts are not stored in Git (scripts/download_fonts.py fetches them); OOPZ_FONT_DIR overrides.
FONT_DIR = Path(os.environ.get("OOPZ_FONT_DIR") or Path(__file__).resolve().parents[4] / "assets" / "fonts")
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\Z")
_REQUIRED_TYPE = ("brand", "badge", "headline", "slogan", "meta", "section", "kicker", "lede", "body", "odd_title",
                  "topic_title", "stage_label", "flow_head", "flow_title", "flow_text", "person_name",
                  "person_title", "stat_name", "stat_value", "note", "empty")


def load_tokens(path: Path | None = None, *, width: int | None = None, max_height: int | None = None,
                scale: int | None = None) -> dict:
    p = Path(path) if path else ROOT / "design" / "tokens.json"
    tokens = json.loads(p.read_text(encoding="utf-8"))
    tokens = copy.deepcopy(tokens)
    if width is not None:
        tokens["canvas"]["width"] = int(width)
    if max_height is not None:
        tokens["canvas"]["height_limit"] = int(max_height)
    if scale is not None:
        tokens["canvas"]["scale"] = int(scale)
    validate_tokens(tokens)
    return tokens


def validate_tokens(t: dict) -> None:
    if t.get("schema_version") != "oopz.design.tokens.v2":
        raise RenderError("tokens:version")
    c = t["canvas"]
    if not (320 <= c["width"] <= 2000 and 1 <= c["scale"] <= 3 and 0 < c["height_limit"] <= 12000
            and 0 < c["png_byte_limit"] <= 10_000_000 and c["margin_x"] * 2 < c["width"]):
        raise RenderError("tokens:canvas_out_of_range")
    floor = t["type_floor_px"]
    for name in _REQUIRED_TYPE:
        s = t["type"].get(name)
        if not s or s["size"] < floor or s["lh"] < 1.0:
            raise RenderError("tokens:type_below_floor_or_missing", name)
    for name, value in t["colors"].items():
        if not _HEX.match(value):
            raise RenderError("tokens:bad_color", name)
    for key in t["person_accents"] + t["topic_accents"]:
        if key not in t["colors"]:
            raise RenderError("tokens:unknown_accent", key)
