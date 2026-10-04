"""validate -> trusted stats -> render -> save digest.png + digest.md (+ local sidecar).

Offline. No provider, no network, no sending. The save step is atomic: either
both final files and the sidecar are installed, or the previous output stays
exactly as it was.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

import PIL

from . import __version__
from .render import RenderResult, render_digest_card
from .stats_view import build_stats_view
from .view import load_labels

MANIFEST_VERSION = "oopz.digest.render.v2"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(model: Mapping, bundle: Mapping | None, metadata: Mapping, stats: Mapping | None, out_dir: Path, *,
             avatars: Mapping[str, str | Path] | None = None, case: str | None = None,
             tokens: Mapping | None = None) -> dict:
    """Produce ``out_dir/digest.png`` and ``out_dir/digest.md``; return the manifest dict.

    ``bundle`` is the evidence bundle used by ``validate_content``; production passes it
    so the model JSON is verified before it is ever rendered.
    """
    if bundle is not None:
        from ..contract import validate_content
        validate_content(model, bundle)
    # fill-in files carry no statistics: say nothing about them instead of printing an "unavailable" note
    sv = None if (stats is None and metadata.get("hide_stats_when_unavailable"))         else build_stats_view(stats, metadata=metadata, labels=load_labels())
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=out_dir) as tmp:
        tmp = Path(tmp)
        res: RenderResult = render_digest_card(model, metadata, tmp / "digest.png", markdown_path=tmp / "digest.md",
                                               stats_view=sv, avatars=avatars, tokens=tokens)
        kinds = [m["kind"] for m in res.modules]
        manifest = {
            "schema_version": MANIFEST_VERSION, "contract": "oopz.digest.content.v2",
            "view_contract": "oopz.design.view.v2", "renderer": __version__, "pillow": PIL.__version__,
            "synthetic": bool(metadata.get("synthetic")), "case": case,
            "width": res.width, "height": res.height, "png_bytes": res.byte_count,
            "min_font_px": res.min_font_px, "visible_text_blocks": len(res.blocks), "text_runs": res.text_runs,
            "content_modules": kinds, "odd_topic_position": kinds.index("odd_topic") + 1,
            "stats": (sv or {}).get("status", "none"), "warnings": res.warnings,
            "layout_fingerprint": res.layout_fingerprint,
            "sha256": {"digest.png": res.sha256, "digest.md": _sha(tmp / "digest.md")},
        }
        side = tmp / "sidecar"
        side.mkdir()
        (side / "render.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (side / "visible_text.json").write_text(json.dumps(res.blocks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (side / "text_bounds.json").write_text(json.dumps(
            [{"block": d["block"], "text": d["text"], "size": d["size"], "bbox": d["bbox"]} for d in res.drawn],
            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        # re-read what was just written: the final files must match what was verified in memory
        if _sha(tmp / "digest.png") != res.sha256 or (tmp / "digest.md").read_text(encoding="utf-8") != res.markdown:
            raise RuntimeError("written files differ from verified render")
        names = ["digest.png", "digest.md"]
        backups = {n: (out_dir / n).read_bytes() for n in names if (out_dir / n).exists()}
        old_side = out_dir / "sidecar"
        installed: list[str] = []
        try:
            for n in names:
                os.replace(tmp / n, out_dir / n)
                installed.append(n)
            if old_side.exists():
                shutil.rmtree(old_side)
            shutil.copytree(side, old_side)
        except Exception:
            for n in installed:
                if n in backups:
                    (out_dir / n).write_bytes(backups[n])
                else:
                    (out_dir / n).unlink(missing_ok=True)
            raise
    return manifest
