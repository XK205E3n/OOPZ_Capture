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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..digest.contract import validate_content
from . import prompts
from .backend import BackendError, parse_json_object
from .transcript import Session, clock
from .windows import Window, split_windows

ATTEMPTS = 3          # model answers tried per call before giving up
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
                  f"{entry['input_chars']} chars in, {entry['error'] or 'accepted'}", flush=True)


def ask(backend, system: str, request: dict, aliases: Aliases, bundle: dict, recorder: Recorder, *,
        stage: str, unit: str) -> dict:
    """Ask until the answer passes the validator (at most ATTEMPTS times); returns content with real ids."""
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    note, last = "", ""
    for attempt in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        reply = backend.complete(system, body + note)
        try:
            content = aliases.to_real(_strip(parse_json_object(reply)))
            validate_content(content, bundle)
        except ValueError as error:          # bad JSON or DigestValidationError
            last = str(error)
            recorder.add(stage=stage, unit=unit, attempt=attempt, seconds=round(time.monotonic() - started, 1),
                         input_chars=len(body), error=last)
            note = ("\n\n【上一次输出被程序拒绝】原因：" + last + "（@ 后面是出错位置）。上一次的输出如下，"
                    "请只修正出错的位置，重新输出完整的JSON：\n" + reply)
            continue
        recorder.add(stage=stage, unit=unit, attempt=attempt, seconds=round(time.monotonic() - started, 1),
                     input_chars=len(body), error=None)
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

    request = {"mode": mode, "people": aliases.people(), "coverage": coverage,
               "windows": aliases.to_alias([view(u) for u in units]),
               "evidence": [aliases.evidence(e) for e in evidence.values()]}
    return request, {"people": aliases.roster, "evidence": list(evidence.values())}


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


def analyze_session(session: Session, backend, *, parallelism: int = 3,
                    windows: list[Window] | None = None) -> Analysis:
    windows = windows if windows is not None else split_windows(session.runs)
    aliases, recorder = Aliases(session.roster), Recorder()
    pool = {run.id: run.as_evidence() for run in session.runs}
    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        units = list(executor.map(
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
    try:
        content = ask(backend, prompts.system_prompt("final"), request, aliases, bundle, recorder,
                      stage="final", unit="all")
    except AnalysisError as error:
        raise AnalysisError(str(error), units, recorder.calls) from None
    return Analysis(content, bundle, units, coverage, recorder.calls)
