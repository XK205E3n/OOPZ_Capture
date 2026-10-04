"""Model content + trusted metadata + trusted statistics -> ``oopz.design.view.v2``.

The view is the single normalised, fully sanitised description of what a reader
will see. Layout (PNG) and Markdown are both generated from it, so nothing can be
visible in one output and missing from the other.

Trust boundaries:
* ``content``   - untrusted model output (already schema/evidence validated upstream,
                  but this module re-checks shape and treats every string as text).
* ``metadata``  - trusted run layer: labels, simulation flag, footer note, timeline.
* ``stats``     - trusted deterministic numbers; the model can never supply them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .icons import normalize_category
from .textlayout import RenderError, sanitize_text

VIEW_VERSION = "oopz.design.view.v2"
ROOT = Path(__file__).resolve().parent

_ARROW = re.compile(r"\s*(?:→|->)\s*")
_NO_ODD = {"status": "none", "title": "没有明显候选",
           "text": "本次可用记录中，没有可确认的明显离奇话题或概念。"}


def load_labels() -> dict:
    return json.loads((ROOT / "design" / "labels.zh-CN.json").read_text(encoding="utf-8"))


def _text(value: Any, where: str, *, limit: int = 4000, required: bool = True) -> str:
    if not isinstance(value, str):
        raise RenderError("view:bad_type", where)
    out = sanitize_text(value)
    if required and not out:
        raise RenderError("view:empty_text", where)
    if len(out) > limit:
        raise RenderError("view:text_too_long", where, length=len(out), limit=limit)
    return out


def validate_metadata(metadata: Any) -> dict:
    """Shape checks for the trusted metadata block (see schema/digest_metadata.schema.json)."""
    if not isinstance(metadata, Mapping):
        raise RenderError("metadata:not_object")
    session = metadata.get("session")
    if not isinstance(session, Mapping):
        raise RenderError("metadata:session_missing")
    for key in ("date_label", "time_label"):
        _text(session.get(key), f"metadata.session.{key}", limit=120)
    if "synthetic" in metadata and not isinstance(metadata["synthetic"], bool):
        raise RenderError("metadata:synthetic_not_bool")
    for key in ("topic_labels", "moment_labels"):
        v = metadata.get(key, [])
        if not isinstance(v, list) or len(v) > 16:
            raise RenderError("metadata:labels_shape", key)
        for item in v:
            _text(item, f"metadata.{key}", limit=80)
    tl = metadata.get("timeline", [])
    if not isinstance(tl, list) or len(tl) > 12:
        raise RenderError("metadata:timeline_shape")
    for item in tl:
        if not isinstance(item, Mapping):
            raise RenderError("metadata:timeline_item")
        _text(item.get("title"), "metadata.timeline.title", limit=120)
        _text(item.get("text"), "metadata.timeline.text", limit=400)
    for key in ("source_note", "summary_label"):
        if metadata.get(key) is not None:
            _text(metadata[key], f"metadata.{key}", limit=600)
    return dict(metadata)


def _item_text(item: Any, where: str, *, limit: int) -> tuple[str, str]:
    if not isinstance(item, Mapping):
        raise RenderError("view:item_not_object", where)
    return _text(item.get("title"), f"{where}.title", limit=300), _text(item.get("text"), f"{where}.text", limit=limit)


def build_view(content: Mapping, metadata: Mapping, *, stats_view: Mapping | None = None,
               labels: Mapping | None = None) -> dict:
    labels = dict(labels or load_labels())
    metadata = validate_metadata(metadata)
    for key in labels.get("metadata_overridable", []):
        override = (metadata.get("labels") or {}).get(key) if isinstance(metadata.get("labels"), Mapping) else None
        if isinstance(override, str) and sanitize_text(override):
            labels[key] = sanitize_text(override)
    if not isinstance(content, Mapping) or "content" not in content or "people" not in content:
        raise RenderError("view:content_shape")
    section = content["content"]
    if not isinstance(section, Mapping):
        raise RenderError("view:content_shape")
    synthetic = bool(metadata.get("synthetic", False))

    headline, summary_text = _item_text(section.get("summary"), "summary", limit=2000)
    modules: list[dict] = []
    summary_kicker = sanitize_text(metadata.get("summary_label") or "") or labels["summary_label"]
    modules.append({"kind": "summary", "kicker": summary_kicker, "text": summary_text})

    odd = section.get("odd_topic")
    if isinstance(odd, Mapping) and odd.get("status") == "supported":
        title, text = _item_text(odd, "odd_topic", limit=1600)
        modules.append({"kind": "odd_topic", "status": "supported", "kicker": labels["odd_topic"],
                        "title": title, "text": text, "icon": normalize_category(odd.get("icon_category"))})
    else:  # absent or explicit "none": keep the module, say so honestly
        src = odd if isinstance(odd, Mapping) and odd.get("status") == "none" else _NO_ODD
        title, text = _item_text(src, "odd_topic", limit=1600)
        modules.append({"kind": "odd_topic", "status": "none", "kicker": labels["odd_topic"],
                        "title": title, "text": text, "icon": "other"})

    for i, m in enumerate(_list(section, "moments", 8)):
        title, text = _item_text(m, f"moments[{i}]", limit=1600)
        stages = []
        raw = m.get("stages") if isinstance(m, Mapping) else None
        if isinstance(raw, list):
            for j, st in enumerate(raw[:12]):
                if not isinstance(st, Mapping):
                    raise RenderError("view:stage_not_object", f"moments[{i}].stages[{j}]")
                stages.append({"label": _text(st.get("label"), f"moments[{i}].stages[{j}].label", limit=120),
                               "icon": normalize_category(st.get("icon_category"))})
        elif _ARROW.search(title):
            stages = [{"label": p, "icon": "other"} for p in _ARROW.split(title) if p.strip()]
        compact = lambda s: re.sub(r"\s|→|->", "", s)  # noqa: E731
        repeats = bool(stages) and compact(title) == compact("".join(s["label"] for s in stages))
        mlabels = metadata.get("moment_labels", [])
        modules.append({"kind": "moment", "kicker": sanitize_text(mlabels[i]) if i < len(mlabels) else labels["moment"],
                        "title": None if repeats else title, "stages": stages, "text": text,
                        "icon": normalize_category(m.get("icon_category"))})

    tlabels = metadata.get("topic_labels", [])
    for i, t in enumerate(_list(section, "topics", 12)):
        title, text = _item_text(t, f"topics[{i}]", limit=1600)
        modules.append({"kind": "topic", "kicker": sanitize_text(tlabels[i]) if i < len(tlabels) else labels["topic"],
                        "title": title, "text": text, "icon": normalize_category(t.get("icon_category"))})
    for i, t in enumerate(_list(section, "next_hooks", 12)):
        title, text = _item_text(t, f"next_hooks[{i}]", limit=1600)
        modules.append({"kind": "next_hook", "kicker": labels["next_hook"], "title": title, "text": text,
                        "icon": normalize_category(t.get("icon_category"))})

    timeline = metadata.get("timeline") or []
    if timeline:
        modules.append({"kind": "flow", "heading": labels["flow_head"],
                        "items": [{"title": _text(t["title"], "timeline.title"), "text": _text(t["text"], "timeline.text")}
                                  for t in timeline]})

    people = content["people"]
    profiles = people.get("profiles") if isinstance(people, Mapping) else None
    if not isinstance(profiles, list):
        raise RenderError("view:people_shape")
    groups: dict[str, dict] = {}
    for i, p in enumerate(profiles):
        title, text = _item_text(p, f"profiles[{i}]", limit=1600)
        sid = p.get("speaker_id")
        if not isinstance(sid, str) or not sid:
            raise RenderError("view:person_id_missing", f"profiles[{i}]")
        nick = _text(p.get("nickname"), f"profiles[{i}].nickname", limit=256)
        g = groups.setdefault(sid, {"speaker_id": sid, "nickname": nick, "comments": []})
        if g["nickname"] != nick:  # one stable id must never show two identities
            raise RenderError("view:person_identity_conflict", f"profiles[{i}]")
        g["comments"].append({"title": title, "text": text})

    view = {
        "schema_version": VIEW_VERSION,
        "synthetic": synthetic,
        "brand": labels["brand"],
        "badge": labels["badge_synthetic"] if synthetic else None,
        "headline": headline,
        "meta": f"{_text(metadata['session']['date_label'], 'date')}  ·  {_text(metadata['session']['time_label'], 'time')}",
        "sections": {"content": labels["section_content"], "people": labels["section_people"]},
        "modules": modules,
        "people": {"empty_text": labels["people_empty"], "groups": list(groups.values())},
        "stats": dict(stats_view) if stats_view else None,
        "stats_heading": labels["stats_heading"],
        "stats_unavailable": labels["stats_unavailable"],
        "footer": sanitize_text(metadata["source_note"]) if metadata.get("source_note") else None,
    }
    check_module_order(view)
    return view


def _list(section: Mapping, key: str, limit: int) -> list:
    v = section.get(key, [])
    if not isinstance(v, list):
        raise RenderError("view:list_shape", key)
    if len(v) > limit:
        raise RenderError("view:too_many_items", key, count=len(v), limit=limit)
    return v


def content_module_kinds(view: Mapping) -> list[str]:
    """Counting rule for 'content module': every entry of ``view['modules']`` in
    reading order. Masthead (brand, badge, headline, date line), section headings,
    the people section and the footer are not content modules."""
    return [m["kind"] for m in view["modules"]]


def check_module_order(view: Mapping) -> None:
    kinds = content_module_kinds(view)
    if "odd_topic" not in kinds or kinds.index("odd_topic") not in (1, 2):
        raise RenderError("view:odd_topic_position", "odd topic must be the 2nd or 3rd content module",
                          kinds=kinds)
