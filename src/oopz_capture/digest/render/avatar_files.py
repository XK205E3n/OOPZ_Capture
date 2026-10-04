"""Caller-verified local avatar files -> small in-memory images.

The mapping speaker_id -> path comes from the trusted run layer, never from model
output, and is keyed by the stable id, never by array position. Any problem
(missing, too big, wrong format, decompression bomb) means "use the neutral
placeholder" and is reported as a warning code - the digest still renders.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Mapping

from PIL import Image

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4_000_000
MAX_SIDE = 4096
ALLOWED = {"PNG", "JPEG", "WEBP"}


def load_avatars(avatars: Mapping[str, str | Path] | None, wanted: set[str]) -> tuple[dict[str, Image.Image], list[str]]:
    images: dict[str, Image.Image] = {}
    warnings: list[str] = []
    for sid, ref in (avatars or {}).items():
        if sid not in wanted:
            continue  # avatars for people who have no commentary are not drawn
        try:
            images[sid] = _load(Path(ref))
        except (OSError, ValueError, Image.DecompressionBombError, Image.UnidentifiedImageError) as exc:
            warnings.append(f"avatar_rejected:{sid}:{type(exc).__name__}")
    return images, warnings


def _load(path: Path) -> Image.Image:
    if not path.is_file():
        raise ValueError("not_a_file")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("too_many_bytes")
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw), formats=sorted(ALLOWED)) as im:
        w, h = im.size
        if w < 16 or h < 16 or w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
            raise ValueError("bad_dimensions")
        im.load()
        im = im.convert("RGBA")
    flat = Image.new("RGB", im.size, (15, 26, 46))
    flat.paste(im, mask=im.getchannel("A"))
    side = min(flat.size)  # centre crop to a square
    left, top = (flat.width - side) // 2, (flat.height - side) // 2
    return flat.crop((left, top, left + side, top + side))
