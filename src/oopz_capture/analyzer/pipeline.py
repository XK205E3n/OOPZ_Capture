"""Full-coverage analysis of one session.

    runs --split--> windows --model--> window notes --(merge groups when there are many)--> one digest

Every run of the transcript is shown to the model in exactly one window; nothing is sampled away.
Every model answer (window notes, merged notes, final digest) has the digest content-v2 shape and is
checked by the same validator (evidence ids exist, anchors are verbatim, people cite only their own
speech).  A rejected answer is retried with the located error; a window that still fails is reported in
``coverage`` instead of silently disappearing.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..digest.contract import DigestValidationError, _text, no_odd_topic, validate_content
from . import prompts
from .backend import BackendError, parse_json_object
from .transcript import UNKNOWN_MEMBER, Session, clock
from .windows import Window, split_windows

ATTEMPTS = 4          # model answers tried per call before giving up
MAX_PROFILES = 4      # people block is about as big as the topics block; none is fine when nobody stood out
MIN_TOPICS = 3        # the editor may drop unreadable entries but keeps at least this many (if the draft had them)
MIN_PROFILES = 3
MAX_STAGES = 4        # the stage track is one row of at most this many
MERGE_ABOVE = 12      # more windows than this are merged in groups before the final digest
MERGE_GROUP = 8


class AnalysisError(RuntimeError):
    """The final digest could not be produced; ``units`` and ``calls`` keep what was learned on the way."""

    def __init__(self, message: str, units: list | None = None, calls: list | None = None):
        super().__init__(message)
        self.units, self.calls = units or [], calls or []


@dataclass
class Analysis:
    content: dict        # oopz.digest.content.v2 with real speaker ids
    bundle: dict         # {"people", "evidence"} the content was validated against
    units: list[dict]    # one entry per window: id, time, notes (or None), error
    coverage: dict
    calls: list[dict]
    flow: list[dict]     # "how the session went": one {"time", "text"} per final-level unit
    labels: dict         # corner tags written by the editor: {"odd", "topics", "moments"} (empty if the editor failed)
    edited: bool         # False when the editor step failed and the plain draft is used


class Aliases:
    """Real speaker ids are 32 hex characters; the model works with s1, s2, ... instead."""

    def __init__(self, roster: list[dict]):
        self.roster = roster
        self.alias = {p["speaker_id"]: f"s{i}" for i, p in enumerate(roster, 1)}
        self.real = {a: r for r, a in self.alias.items()}

    def people(self) -> list[dict]:
        return [{"speaker_id": self.alias[p["speaker_id"]], "nickname": p["nickname"]} for p in self.roster]

    def evidence(self, item: dict, *, brief: bool = False) -> dict:
        value = {"id": item["id"], "speaker_id": self.alias.get(item.get("speaker_id", ""), ""), "text": item["text"]}
        return value if brief else value | {"kind": item["kind"]}

    def to_real(self, content: dict) -> dict:
        content = copy.deepcopy(content)
        for profile in content.get("people", {}).get("profiles", []):
            profile["speaker_id"] = self.real.get(profile.get("speaker_id"), profile.get("speaker_id"))
        odd = content.get("content", {}).get("odd_topic")
        if isinstance(odd, dict) and isinstance(odd.get("participant_ids"), list):
            odd["participant_ids"] = [self.real.get(p, p) for p in odd["participant_ids"]]
        return content

    def to_alias(self, value):
        """Copy of a notes block with real speaker ids replaced, for showing to the model."""
        text = json.dumps(value, ensure_ascii=False)
        for real, alias in self.alias.items():
            text = text.replace(real, alias)
        return json.loads(text)


def normalize(content: dict) -> dict:
    """Settle details that never change what the card says: the fixed "no odd topic" block, the
    icon_category that only topics and moments may carry, duplicate or surplus evidence ids."""
    section = content.get("content", {})
    if isinstance(section.get("odd_topic"), dict) and section["odd_topic"].get("status") == "none":
        section["odd_topic"] = no_odd_topic()
    for profile in content.get("people", {}).get("profiles", []):
        if isinstance(profile, dict):
            profile.pop("icon_category", None)
    for entry in _entries(content):
        refs = entry.get("evidence_ids")
        if isinstance(refs, list) and all(isinstance(r, str) for r in refs):
            entry["evidence_ids"] = list(dict.fromkeys(refs))[:6]
    return content


def _entries(content: dict):
    section = content.get("content", {})
    found = [section.get("summary"), section.get("odd_topic")]
    for field in ("topics", "moments", "next_hooks"):
        found += section.get(field) if isinstance(section.get(field), list) else []
    found += content.get("people", {}).get("profiles", []) if isinstance(content.get("people"), dict) else []
    return [e for e in found if isinstance(e, dict)]


PUNCTUATION = re.compile(r"[，。！？、；…,.!?;]")


def check_style(content: dict) -> None:
    for index, entry in enumerate(_entries(content)):
        if len(entry.get("text", "")) > 15 and not PUNCTUATION.search(entry["text"]):
            raise ValueError(f"style:text of '{entry.get('title', '')}' has no punctuation; break it into readable "
                             "sentences with full-width commas and periods")
    for moment in content["content"]["moments"]:
        if len(moment.get("stages", [])) > MAX_STAGES:
            raise ValueError(f"style:moment has {len(moment['stages'])} stages but at most {MAX_STAGES} fit in one row; "
                             "keep the most important ones")
    for profile in content["people"]["profiles"]:
        if profile["nickname"].startswith(UNKNOWN_MEMBER):
            raise ValueError(f"style:profiles must not feature {UNKNOWN_MEMBER} (an audio track not matched to a member); "
                             "remove that entry")
    count = len(content["people"]["profiles"])
    if count > MAX_PROFILES:
        raise ValueError(f"style:people.profiles has {count} entries but at most {MAX_PROFILES} are wanted; "
                         "keep only the people with the most notable contribution")


def check_named(content: dict, roster: list[dict]) -> None:
    """Topics and moments must say who did it: a roster nickname appears in the title or text."""
    names = [p["nickname"] for p in roster if not p["nickname"].startswith(UNKNOWN_MEMBER)]
    for field in ("topics", "moments"):
        for index, entry in enumerate(content["content"][field]):
            if not any(name in entry["title"] + entry["text"] for name in names):
                raise ValueError(f"style:{field}[{index}] names nobody; write who did it with a nickname copied "
                                 "exactly from people (not an unidentified member)")


REAL_LINE = re.compile(r"r\d+$")     # ids of spoken lines; window summaries (w01, s01) are not events
SAME_STORY = 0.5                    # two blocks citing at least this share of the smaller one's lines tell one story


def _lines(entry: dict) -> set[str]:
    ids = list(entry.get("evidence_ids", []))
    for stage in entry.get("stages", []):
        ids += stage["evidence_ids"]
    return {i for i in ids if REAL_LINE.match(i)}


def _shared(a: set[str], b: set[str]) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _blocks(content: dict) -> list[tuple[str, dict, bool]]:
    """(name, entry, is_person) for every block that must stand for its own story; hooks are exempt
    (they point at what stayed open, which may sit right after a story)."""
    section = content["content"]
    found = [] if section["odd_topic"].get("status") == "none" else [("odd_topic", section["odd_topic"], False)]
    for field in ("topics", "moments"):
        found += [(f"{field}[{i}]", e, False) for i, e in enumerate(section[field])]
    return found + [(f"profiles[{i}]", p, True) for i, p in enumerate(content["people"]["profiles"])]


def check_distinct(content: dict) -> None:
    """No two blocks of the card may cite (nearly) the same spoken lines: one event is told once."""
    items = [(name, _lines(entry), person) for name, entry, person in _blocks(content)]
    for j, (later, lines_j, person_j) in enumerate(items):
        for earlier, lines_i, _ in items[:j]:
            if _shared(lines_i, lines_j) >= SAME_STORY:
                shared = ", ".join(sorted(lines_i & lines_j))
                advice = ("tell another thing this person did, citing other lines of theirs, or leave this person out"
                          if person_j else "choose a different event from the evidence, or leave this block out")
                raise ValueError(f"style:{later} tells the same story as {earlier} (both cite {shared}); one event is "
                                 f"told in one block only: {advice}")


def drop_repeats(content: dict) -> None:
    """Last resort when the model keeps repeating itself: keep the odd topic, then the moments, then the
    topics, and drop a topic or moment that retells one already kept.  People are left alone."""
    section = content["content"]
    kept = [] if section["odd_topic"].get("status") == "none" else [_lines(section["odd_topic"])]
    for field in ("moments", "topics"):
        survivors = []
        for entry in section[field]:
            lines = _lines(entry)
            if any(_shared(other, lines) >= SAME_STORY for other in kept):
                continue
            kept.append(lines)
            survivors.append(entry)
        section[field] = [e for e in section[field] if any(e is s for s in survivors)]


def _strip(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items()}
    return value


class Recorder:
    def __init__(self):
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def add(self, **entry) -> None:
        with self._lock:
            self.calls.append(entry)
            print(f"[{entry['stage']} {entry['unit']}] attempt {entry['attempt']}: {entry['seconds']}s, "
                  f"{entry['input_chars']} chars in, {(entry['error'] or 'accepted')[:160]}", flush=True)


def _complete(backend, system: str, body: str) -> tuple[str, dict]:
    if hasattr(backend, "complete_with_usage"):
        return backend.complete_with_usage(system, body)
    return backend.complete(system, body), {}


def ask(backend, system: str, request: dict, aliases: Aliases, bundle: dict, recorder: Recorder, *,
        stage: str, unit: str, extra=None, prepare=None, distinct: bool = False) -> dict:
    """Ask until the answer passes the validator (at most ATTEMPTS times); returns content with real ids.
    ``distinct``: blocks must not retell one event; the last attempt repairs instead of rejecting."""
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    note, last = "", ""
    for attempt in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        reply, usage = _complete(backend, system, body + note)
        try:
            parsed = _strip(parse_json_object(reply))
            content = normalize(aliases.to_real(prepare(parsed) if prepare else parsed))
            validate_content(content, bundle)
            check_style(content)
            if distinct:
                check_distinct(content) if attempt < ATTEMPTS else drop_repeats(content)
            if extra:
                extra(content)
        except ValueError as error:          # bad JSON or DigestValidationError
            last = str(error)
            recorder.add(stage=stage, unit=unit, attempt=attempt, seconds=round(time.monotonic() - started, 1),
                         input_chars=len(body), output_chars=len(reply), usage=usage, error=last, reply=reply)
            note = ("\n\n【上一次输出被程序拒绝】原因：" + last + "（@ 后面是出错位置）。上一次的输出如下，"
                    "请只修正出错的位置，重新输出完整的JSON：\n" + reply)
            continue
        recorder.add(stage=stage, unit=unit, attempt=attempt, seconds=round(time.monotonic() - started, 1),
                     input_chars=len(body), output_chars=len(reply), usage=usage, error=None)
        return content
    raise AnalysisError(f"{stage} {unit}: rejected {ATTEMPTS} times, last reason {last}")


def cited_ids(notes: dict) -> list[str]:
    section = notes["content"]
    entries = [section["summary"], section["odd_topic"], *section["topics"], *section["moments"],
               *section["next_hooks"], *notes["people"]["profiles"]]
    ids: list[str] = []
    for entry in entries:
        for ref in entry["evidence_ids"]:
            if ref not in ids:
                ids.append(ref)
    return ids


def _summary_evidence(unit: dict) -> dict:
    return {"id": unit["id"], "kind": "window_summary", "speaker_id": "", "start_ms": unit["start_ms"],
            "end_ms": unit["end_ms"], "text": unit["notes"]["content"]["summary"]["text"]}


def analyze_window(backend, session: Session, window: Window, total: int, aliases: Aliases,
                   recorder: Recorder) -> dict:
    unit = {"id": f"w{window.index:02d}", "start_ms": window.start_ms, "end_ms": window.end_ms,
            "time": f"{clock(session, window.start_ms)}-{clock(session, window.end_ms)}",
            "notes": None, "error": None}
    evidence = [run.as_evidence() for run in window.runs]
    request = {"mode": "window", "window": window.index, "of": total, "time": unit["time"],
               "people": aliases.people(), "evidence": [aliases.evidence(e, brief=True) for e in evidence]}
    try:
        unit["notes"] = ask(backend, prompts.system_prompt("window"), request, aliases,
                            {"people": session.roster, "evidence": evidence}, recorder,
                            stage="window", unit=unit["id"])
    except (AnalysisError, BackendError) as error:
        unit["error"] = str(error)
    return unit


def synthesis_request(mode: str, units: list[dict], pool: dict, aliases: Aliases,
                      coverage: dict) -> tuple[dict, dict]:
    """(request for the model, bundle for the validator) for merging ``units``, which all have notes."""
    evidence = {unit["id"]: _summary_evidence(unit) for unit in units}
    for unit in units:
        for ref in cited_ids(unit["notes"]):
            evidence.setdefault(ref, pool[ref])

    def view(unit: dict) -> dict:
        notes = unit["notes"]
        section = notes["content"]
        odd = section["odd_topic"]

        def brief(entry: dict) -> dict:
            return {k: entry[k] for k in ("title", "text", "evidence_ids")}

        return {"id": unit["id"], "time": unit["time"], "summary": section["summary"]["text"],
                "topics": [brief(t) for t in section["topics"]],
                "odd_topic": None if odd["status"] == "none" else brief(odd),
                "people": [brief(p) | {"speaker_id": p["speaker_id"]} for p in notes["people"]["profiles"]]}

    ordered = sorted(evidence.values(), key=lambda e: (e["start_ms"], e["id"]))
    request = {"mode": mode, "people": aliases.people(), "coverage": coverage,
               "windows": aliases.to_alias([view(u) for u in units]),
               "evidence": [aliases.evidence(e) for e in ordered]}
    return request, {"people": aliases.roster, "evidence": ordered}


def merge_group(backend, group: list[dict], number: int, pool: dict, aliases: Aliases, coverage: dict,
                recorder: Recorder) -> dict:
    request, bundle = synthesis_request("section", group, pool, aliases, coverage)
    unit = {"id": f"s{number:02d}", "start_ms": group[0]["start_ms"], "end_ms": group[-1]["end_ms"],
            "time": f"{group[0]['time'].split('-')[0]}-{group[-1]['time'].split('-')[1]}", "notes": None,
            "error": None}
    unit["notes"] = ask(backend, prompts.system_prompt("section"), request, aliases, bundle, recorder,
                        stage="merge", unit=unit["id"])
    pool[unit["id"]] = _summary_evidence(unit)
    return unit


def check_labels(labels, content: dict, flow: list[dict]) -> dict:
    """Corner tags written by the editor; they must match the (possibly shortened) content one to one."""
    if not isinstance(labels, dict) or set(labels) != {"odd", "topics", "moments", "timeline"}:
        raise DigestValidationError("labels:fields expected odd, topics, moments, timeline")
    section = content["content"]
    for name, size in (("topics", len(section["topics"])), ("moments", len(section["moments"])),
                       ("timeline", len(flow))):
        if not isinstance(labels[name], list) or len(labels[name]) != size:
            raise DigestValidationError(f"labels:{name} must be a list with {size} entries (one per entry), "
                                        f"got {len(labels[name]) if isinstance(labels[name], list) else 'a non-list'}")
    out = {"odd": "" if section["odd_topic"]["status"] == "none" else _text(labels["odd"], 12, "labels.odd")}
    for name in ("topics", "moments"):
        out[name] = [_text(item, 12, f"labels.{name}[{i}]") for i, item in enumerate(labels[name])]
    out["timeline"] = [_text(item, 20, f"labels.timeline[{i}]") for i, item in enumerate(labels["timeline"])]
    return out


def edit_content(backend, content: dict, bundle: dict, flow: list[dict], aliases: Aliases, recorder: Recorder,
                 fit=None, coverage: dict | None = None) -> tuple[dict, dict]:
    """Rewrite the validated draft as poster copy.  Returns (content, labels); evidence is untouched
    (the same validator runs again), entries the editor judges unreadable are dropped."""
    pool = {e["id"]: e for e in bundle["evidence"]}
    ids = [ref for entry in _entries(content) for ref in entry["evidence_ids"]]
    for entry in _entries(content):
        for stage in entry.get("stages", []):
            ids += stage["evidence_ids"]
    ids = list(dict.fromkeys(ids))
    request = {"mode": "editor", "people": aliases.people(), "draft": aliases.to_alias(content),
               "flow": [item["text"] for item in flow],
               "evidence": [aliases.evidence(pool[i], brief=True) for i in ids if i in pool]}
    held: dict = {}

    def prepare(parsed: dict) -> dict:
        held["labels"] = parsed.pop("labels", None)
        return parsed

    def extra(edited: dict) -> None:
        for name, wanted in (("topics", MIN_TOPICS), ("profiles", MIN_PROFILES)):
            before = len(content["content"][name] if name == "topics" else content["people"][name])
            after = len(edited["content"][name] if name == "topics" else edited["people"][name])
            if after < min(wanted, before):
                raise ValueError(f"style:{name} has {after} entries but at least {min(wanted, before)} are required; "
                                 "rewrite the weak ones instead of deleting them")
        check_named(edited, aliases.roster)
        held["checked"] = check_labels(held["labels"], edited, flow)
        if fit:
            fit(edited, bundle, coverage or {"missing": []}, [{"time": f["time"], "text": t}
                                                            for f, t in zip(flow, held["checked"]["timeline"])])

    edited = ask(backend, prompts.system_prompt("editor"), request, aliases, bundle, recorder,
                 stage="edit", unit="all", extra=extra, prepare=prepare)
    return edited, held["checked"]


def analyze_session(session: Session, backend, *, parallelism: int = 3,
                    windows: list[Window] | None = None, fit=None, units: list[dict] | None = None) -> Analysis:
    """``units`` (window results saved by an earlier run) skips the window stage.
    ``fit(content, bundle, coverage, flow)`` may raise ValueError (e.g. card too tall) to send the final digest back."""
    windows = windows if windows is not None else split_windows(session.runs)
    aliases, recorder = Aliases(session.roster), Recorder()
    pool = {run.id: run.as_evidence() for run in session.runs}
    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        units = units if units is not None else list(executor.map(
            lambda w: analyze_window(backend, session, w, len(windows), aliases, recorder), windows))
        done = [u for u in units if u["notes"]]
        coverage = {"windows_total": len(units), "windows_analyzed": len(done),
                    "missing": [{"window": u["id"], "time": u["time"]} for u in units if not u["notes"]],
                    "transcript": "自动语音识别结果，可能有错别字"}
        if not done:
            raise AnalysisError("no window could be analysed", units, recorder.calls)
        pool.update({u["id"]: _summary_evidence(u) for u in done})
        level, number = done, 0
        while len(level) > MERGE_ABOVE:
            groups = [level[i:i + MERGE_GROUP] for i in range(0, len(level), MERGE_GROUP)]
            numbered = list(enumerate(groups, number + 1))
            number += len(groups)
            level = list(executor.map(
                lambda pair: merge_group(backend, pair[1], pair[0], pool, aliases, coverage, recorder), numbered))
    request, bundle = synthesis_request("final", level, pool, aliases, coverage)
    flow = [{"time": u["time"], "text": u["notes"]["content"]["summary"]["title"]} for u in level]
    try:
        content = ask(backend, prompts.system_prompt("final"), request, aliases, bundle, recorder,
                      stage="final", unit="all", distinct=True)
    except AnalysisError as error:
        raise AnalysisError(str(error), units, recorder.calls) from None
    try:
        edited, labels = edit_content(backend, content, bundle, flow, aliases, recorder, fit, coverage)
    except (AnalysisError, BackendError) as error:     # keep the checked draft rather than lose the run
        print(f"EDITOR FAILED, plain draft is used: {error}", flush=True)
        return Analysis(content, bundle, units, coverage, recorder.calls, flow, {}, False)
    flow = [{"time": item["time"], "text": text} for item, text in zip(flow, labels["timeline"])]
    return Analysis(edited, bundle, units, coverage, recorder.calls, flow, labels, True)
