"""Digest content validator (from the V7 contract kit); errors carry their location. No API/CLI imports or credentials."""

from __future__ import annotations

from typing import Any

import re

DIGEST_VERSION = "oopz.digest.v2"

AUTHORING_VERSION = "2.1.0-review"

MAX_CARD_CHARS = 12000

UNSUPPORTED = re.compile(r"最(?!后|近|初|终)|唯一|全场第一|第一名|冠军|冠军级|全员|所有人|百分之|\d+(?:\.\d+)?\s*%|\b(?:best|worst|most|least|only|everyone|always|never)\b", re.I)

UNSAFE_PERSONAL = re.compile(r"抑郁|焦虑|精神病|自闭症|处方药|服药|服用|诊断|治疗|智障|性癖|隐私|身份证|银行卡|电话号码|家庭住址|最安静")

QUOTATION = re.compile(r"""[“”‘’「」『』\"'`<>]|https?://""", re.I)

class DigestValidationError(ValueError):
    """Safe machine-readable validation reason (never model/source text)."""

def located(where: str, function, *args, **kwargs):
    """Run a check and tag a failure with where it happened, e.g. ``anchor:not_verbatim_source@topics[1]``."""
    try:
        return function(*args, **kwargs)
    except DigestValidationError as error:
        if "@" in str(error):
            raise
        raise DigestValidationError(f"{error}@{where}") from None

def _object(value: Any, keys: set[str], code: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise DigestValidationError(code + ":fields")
    return value

def _mask(value: str, names: tuple[str, ...]) -> str:
    """Roster nicknames are trusted names, not claims: hide them before scanning for numbers or banned words."""
    for name in names:
        value = value.replace(name, "")
    return value

def _text(value: Any, maximum: int, code: str, names: tuple[str, ...] = ()) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or value != value.strip():
        raise DigestValidationError(code + ":length")
    plain = _mask(value, names)
    if any(ord(c) < 32 for c in value) or QUOTATION.search(plain) or UNSUPPORTED.search(plain) or UNSAFE_PERSONAL.search(plain):
        raise DigestValidationError(code + ":unsupported_claim_or_markup")
    return value

def no_odd_topic() -> dict:
    return {"status": "none", "title": "没有明显候选", "text": "本次可用记录中，没有可确认的明显离奇话题或概念。",
            "evidence_ids": [], "anchor": "", "participant_ids": []}

def validate_content(content: Any, bundle: dict) -> dict:
    """Validate structure, evidence existence, exact anchors and speaker identity.

    Exact anchors do not prove semantic entailment of prose. This is a bounded
    mechanical gate plus a strict authoring contract, not a quality guarantee.
    """
    _object(content, {"content", "people"}, "root")
    section = _object(content["content"], {"summary", "odd_topic", "topics", "moments", "next_hooks"}, "content")
    people = _object(content["people"], {"profiles"}, "people")
    evidence = {e["id"]: e for e in bundle["evidence"]}
    roster = {p["speaker_id"]: p for p in bundle["people"]}
    names = tuple(sorted({p["nickname"] for p in roster.values() if len(p["nickname"]) >= 2}, key=len, reverse=True))
    total_chars = 0

    def item(value, *, summary=False, person=False, odd=False, allows_stages=False):
        nonlocal total_chars
        keys = {"title", "text", "evidence_ids", "anchor"}
        if person:
            keys |= {"speaker_id", "nickname"}
        if odd:
            keys |= {"status", "participant_ids"}
        if isinstance(value, dict) and not person and "icon_category" in value:
            keys.add("icon_category")
            if not isinstance(value["icon_category"], str) or len(value["icon_category"]) > 64:
                raise DigestValidationError("icon_category:invalid")
        if isinstance(value, dict) and allows_stages and "stages" in value:
            keys.add("stages")
        _object(value, keys, "item")
        _text(value["title"], 160, "title", names)
        _text(value["text"], 1600 if summary else 1200, "text", names)
        refs = value["evidence_ids"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 6 or any(not isinstance(r, str) for r in refs):
            raise DigestValidationError("evidence:count")
        if len(set(refs)) != len(refs) or any(r not in evidence for r in refs):
            raise DigestValidationError("evidence:unknown_or_duplicate")
        cited_text = " ".join(evidence[r]["text"] for r in refs)
        source_numbers = set(re.findall(r"\d+(?:\.\d+)?", cited_text))
        claimed = _mask(value["title"] + " " + value["text"], names)
        if any(number not in source_numbers for number in re.findall(r"\d+(?:\.\d+)?", claimed)):
            raise DigestValidationError("item:unsupported_numeric_claim")
        anchor = value["anchor"]
        if not isinstance(anchor, str) or not 4 <= len(anchor) <= 80 or not any(anchor in evidence[r]["text"] for r in refs):
            raise DigestValidationError("anchor:not_verbatim_source")
        if person:
            uid = value["speaker_id"]
            if (not isinstance(uid, str) or uid not in roster or not isinstance(value["nickname"], str)
                    or not 1 <= len(value["nickname"]) <= 256 or value["nickname"] != roster[uid]["nickname"]):
                raise DigestValidationError("person:unknown_identity")
            if any(evidence[r]["kind"] != "asr_excerpt" or evidence[r]["speaker_id"] != uid for r in refs):
                raise DigestValidationError("person:wrong_speaker_evidence")
            # A single tiny excerpt is not sufficient for a person commentary.
            if sum(len(evidence[r]["text"]) for r in refs) < 16:
                raise DigestValidationError("person:insufficient_evidence")
        if odd:
            public_text = value["title"] + " " + value["text"]
            for actor in roster.values():
                for name in (actor["speaker_id"], actor["nickname"]):
                    # Conservative attribution gate; common-word nicknames can
                    # produce false positives, handled by bounded regeneration.
                    if len(name) < 2:
                        continue
                    pattern = (r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
                               if name.isascii() else re.escape(name))
                    if re.search(pattern, public_text, re.I):
                        raise DigestValidationError("odd_topic:public_attribution")
            participants = value["participant_ids"]
            if (value["status"] != "supported" or not isinstance(participants, list)
                    or len(participants) > 6 or any(not isinstance(uid, str) or uid not in roster for uid in participants)
                    or len(set(participants)) != len(participants)):
                raise DigestValidationError("odd_topic:participants")
            for uid in participants:
                if not any(evidence[r]["kind"] == "asr_excerpt" and evidence[r]["speaker_id"] == uid for r in refs):
                    raise DigestValidationError("odd_topic:unsupported_participant")
        total_chars += len(value["title"]) + len(value["text"])
        if allows_stages and "stages" in value:
            stages = value["stages"]
            if not isinstance(stages, list) or len(stages) > 8:
                raise DigestValidationError("stages:count")
            previous = -1
            for stage in stages:
                _object(stage, {"label", "icon_category", "evidence_ids", "anchor"}, "stage")
                item({"title": stage["label"], "text": stage["label"], "icon_category": stage["icon_category"],
                      "evidence_ids": stage["evidence_ids"], "anchor": stage["anchor"]})
                start = min(evidence[r]["start_ms"] for r in stage["evidence_ids"])
                if start < previous:
                    raise DigestValidationError("stages:unsupported_order")
                previous = start

    located("summary", item, section["summary"], summary=True)
    odd_topic = section["odd_topic"]
    if isinstance(odd_topic, dict) and odd_topic.get("status") == "none":
        if odd_topic != no_odd_topic():
            raise DigestValidationError("odd_topic:invalid_absence")
    else:
        located("odd_topic", item, odd_topic, odd=True)
    for field, limit in (("topics", 3), ("moments", 2), ("next_hooks", 2)):
        items = section[field]
        if not isinstance(items, list) or len(items) > limit:
            raise DigestValidationError(field + ":count")
        for index, entry in enumerate(items):
            located(f"{field}[{index}]", item, entry, allows_stages=field == "moments")
    profiles = people["profiles"]
    if not isinstance(profiles, list) or len(profiles) > 7:
        raise DigestValidationError("profiles:count")
    for index, profile in enumerate(profiles):
        located(f"profiles[{index}]", item, profile, person=True)
    if len({(p["speaker_id"], p["title"]) for p in profiles}) != len(profiles):
        raise DigestValidationError("profiles:duplicate_category")
    if total_chars > MAX_CARD_CHARS:
        raise DigestValidationError("card:text_budget")
    return content
