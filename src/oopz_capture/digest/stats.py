"""Deterministic, observation-bounded speaker-frequency estimates.

Presence is never reconstructed from speech, users.json, or legacy Agora events.
Two consecutive successful membership snapshots support only their common users
between the first completion and the second request start. Request latency,
failed refreshes, health/reconnect gaps, overlong polling gaps, and the final
unobserved tail contribute no presence. Polling still cannot prove continuous
physical attendance between samples; all displayed counts/rates are estimates.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..output import write_json


PRESENCE_SCHEMA = "oopz.presence.observations.v1"
FREQUENCY_SCHEMA = "oopz.digest.frequency.v1"
PRESENCE_FILENAME = "presence_observations.json"
PRESENCE_SOURCE = "successful_membership_snapshot_pairs"
MERGE_GAP_MS = 1_500
MERGE_MAX_SPAN_MS = 30_000
# The planner can clamp diagnostic max-runtime overshoot by up to two seconds.
# This is a compatibility tolerance, not permission to infer attendance using
# a wall clock which disagrees materially with the observation clock.
CLOCK_DURATION_TOLERANCE_MS = 2_000
LOGGER = logging.getLogger(__name__)


class PresenceClockDisagreement(ValueError):
    """Session and membership duration clocks cannot be safely compared."""


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _uid(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def union_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union overlapping/touching half-open intervals, without double counting."""
    result: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


class PresenceObservationRecorder:
    """Persist prospective membership evidence without altering old captures.

    All offsets use the capture's monotonic clock, in integer milliseconds.
    A gap invalidates the previous snapshot, including on a transient health
    failure which later recovers without rejoining. finish() never extends it.
    """

    def __init__(self, session_dir: Path, *, session_id: str,
                 self_oopz_uid: str, max_snapshot_gap_ms: int) -> None:
        self.path = session_dir / PRESENCE_FILENAME
        self.failed = False
        self.data: dict[str, Any] = {
            "schema_version": PRESENCE_SCHEMA,
            "source": PRESENCE_SOURCE,
            "session_id": session_id,
            "clock": "monotonic_session_elapsed_ms",
            "self_oopz_uid": self_oopz_uid,
            "max_snapshot_gap_ms": _integer(max_snapshot_gap_ms, "max_snapshot_gap_ms", minimum=1),
            "finalized": False,
            "observations": [],
        }
        self._write()

    def _write(self) -> None:
        if self.failed:
            return
        temporary = self.path.with_suffix(".json.tmp")
        try:
            write_json(temporary, self.data)
            temporary.replace(self.path)
        except OSError as error:
            # Statistics are additive. Preserve any prior unfinalized evidence
            # and never interrupt primary audio capture on a sidecar failure.
            self.failed = True
            self.data["finalized"] = False
            LOGGER.warning("Presence persistence failed (%s); frequency statistics disabled", type(error).__name__)

    def snapshot(self, *, request_started_ms: int, observed_ms: int,
                 connection_episode: int, participants: Iterable[Any]) -> None:
        if self.failed:
            return
        people = {}
        for participant in participants:
            uid = _uid(getattr(participant, "oopz_uid", None))
            if not uid:
                # A partial membership result cannot establish who was absent.
                self.gap(at_ms=observed_ms, reason="invalid_member_identity")
                return
            people[uid] = {
                "oopz_uid": uid,
                "nickname": str(getattr(participant, "nickname", "") or ""),
                "is_bot": bool(getattr(participant, "is_bot", False)),
            }
        self.data["observations"].append({
            "kind": "snapshot", "request_started_ms": request_started_ms,
            "observed_ms": observed_ms, "connection_episode": connection_episode,
            "people": [people[uid] for uid in sorted(people)],
        })
        self._write()

    def gap(self, *, at_ms: int, reason: str) -> None:
        if self.failed:
            return
        self.data["observations"].append({"kind": "gap", "at_ms": at_ms, "reason": reason})
        self._write()

    def finish(self, *, duration_ms: int, monotonic_duration_ms: int) -> None:
        if self.failed:
            return
        self.data["duration_ms"] = _integer(duration_ms, "duration_ms")
        self.data["monotonic_duration_ms"] = _integer(monotonic_duration_ms, "monotonic_duration_ms")
        self.data["finalized"] = True
        self._write()


def observed_presence_intervals(
    data: Mapping[str, Any], duration_ms: int,
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, str]]:
    """Validate v1 evidence and derive conservative per-person presence unions.

    Returned intervals are clipped to [0, duration_ms). No extrapolation is made
    before the first observation, after the last, or across explicit gaps. The
    supplied duration is the pipeline's full session duration, not speech span.
    It and the recorded wall duration must each agree with the capture-stop
    monotonic duration within 2000ms, including the exact boundary. That small
    tolerance accommodates the planner's existing diagnostic max-runtime clamp.
    """
    _integer(duration_ms, "duration_ms")
    if (data.get("schema_version") != PRESENCE_SCHEMA
            or data.get("source") != PRESENCE_SOURCE
            or data.get("clock") != "monotonic_session_elapsed_ms"
            or data.get("finalized") is not True):
        raise ValueError("unsupported_or_unfinished_presence_observations")
    monotonic_duration = _integer(data.get("monotonic_duration_ms"), "monotonic_duration_ms")
    wall_duration = _integer(data.get("duration_ms"), "duration_ms")
    if (abs(monotonic_duration - duration_ms) > CLOCK_DURATION_TOLERANCE_MS
            or abs(monotonic_duration - wall_duration) > CLOCK_DURATION_TOLERANCE_MS):
        raise PresenceClockDisagreement("clock_disagreement")
    maximum_gap = _integer(data.get("max_snapshot_gap_ms"), "max_snapshot_gap_ms", minimum=1)
    observations = data.get("observations")
    if not isinstance(observations, list):
        raise ValueError("observations must be a list")
    self_uid = _uid(data.get("self_oopz_uid"))
    intervals: dict[str, list[tuple[int, int]]] = {}
    names: dict[str, str] = {}
    excluded_uids = {self_uid} if self_uid else set()
    previous: dict[str, Any] | None = None
    last_ms = 0
    last_episode = 0
    for item in observations:
        if not isinstance(item, dict):
            raise ValueError("invalid observation")
        if item.get("kind") == "gap":
            at_ms = _integer(item.get("at_ms"), "gap.at_ms")
            if at_ms < last_ms or at_ms > monotonic_duration:
                raise ValueError("nonmonotonic presence observations")
            last_ms = at_ms
            previous = None
            continue
        if item.get("kind") != "snapshot":
            raise ValueError("unknown presence observation")
        started = _integer(item.get("request_started_ms"), "request_started_ms")
        observed = _integer(item.get("observed_ms"), "observed_ms")
        episode = _integer(item.get("connection_episode"), "connection_episode", minimum=1)
        if (started > observed or observed < last_ms or observed > monotonic_duration
                or episode < last_episode):
            raise ValueError("nonmonotonic presence snapshot")
        last_episode = episode
        request_crossed_barrier = started < last_ms
        last_ms = observed
        people = item.get("people")
        if not isinstance(people, list):
            raise ValueError("snapshot.people must be a list")
        members: set[str] = set()
        for person in people:
            if not isinstance(person, dict) or not (uid := _uid(person.get("oopz_uid"))):
                raise ValueError("snapshot member lacks stable oopz_uid")
            if not isinstance(person.get("is_bot"), bool) or uid in members:
                raise ValueError("invalid or duplicate snapshot member")
            members.add(uid)
            if person["is_bot"]:
                excluded_uids.add(uid)
            nickname = person.get("nickname")
            if isinstance(nickname, str) and nickname.strip():
                names[uid] = nickname.strip()
            intervals.setdefault(uid, [])
        if (previous is not None and episode == previous["episode"]
                and previous["observed"] <= started
                and observed - previous["observed"] <= maximum_gap):
            start, end = previous["observed"], min(started, duration_ms)
            if start < end:
                for uid in previous["members"] & members:
                    intervals[uid].append((start, end))
        # A request which began before a failure/reconnect barrier may carry
        # a stale pre-gap snapshot, even if it completed after recovery.
        previous = None if request_crossed_barrier else {
            "episode": episode, "observed": observed, "members": members,
        }
    return (
        {uid: union_intervals(items) for uid, items in intervals.items() if uid not in excluded_uids},
        {uid: name for uid, name in names.items() if uid not in excluded_uids},
    )


def _real_speech(item: Mapping[str, Any]) -> bool:
    text = str(item.get("text") or "").strip()
    return bool(text) and (
        item.get("transcript_source") != "no-speech-marker"
        and str(item.get("language") or "").casefold() != "none"
        and text not in {"[该时间段未检测到有效语音文本]", "[silence]", "[no speech]"}
    )


def merged_speech_runs(
    segments: Iterable[Mapping[str, Any]], presence_intervals: list[tuple[int, int]],
    *, merge_gap_ms: int = MERGE_GAP_MS, merge_max_span_ms: int = MERGE_MAX_SPAN_MS,
) -> list[tuple[int, int]]:
    """Estimate utterances from one person's speech, independent of ASR rows.

    Overlap/touching rows are unioned first (including duplicate ASR/chunk rows),
    so fragmented and unfragmented speech share the same original start. A
    contiguous span counts only when that start is within observed presence
    [start, end); its end is clipped to that interval. Consecutive disjoint
    speech spans merge when gap <= 1500ms AND combined span <= 30000ms. A single
    already-contiguous span over 30s remains one estimate; it is never split to
    manufacture extra utterances. Runs never bridge unknown presence gaps.
    """
    _integer(merge_gap_ms, "merge_gap_ms")
    _integer(merge_max_span_ms, "merge_max_span_ms", minimum=1)
    values = []
    for item in segments:
        if not _real_speech(item):
            continue
        start = _integer(item.get("start_ms"), "segment.start_ms")
        end = _integer(item.get("end_ms"), "segment.end_ms")
        if end < start:
            raise ValueError("segment end precedes start")
        if start < end:
            values.append((start, end))
    # Canonicalize BEFORE the boundary test; otherwise a later ASR fragment
    # can manufacture a new start inside presence for an earlier utterance.
    values = union_intervals(values)
    result: list[tuple[int, int]] = []
    for presence_start, presence_end in union_intervals(presence_intervals):
        continuous = union_intervals(
            (start, min(end, presence_end))
            for start, end in values if presence_start <= start < presence_end
        )
        runs: list[tuple[int, int]] = []
        for start, end in continuous:
            if (runs and start - runs[-1][1] <= merge_gap_ms
                    and end - runs[-1][0] <= merge_max_span_ms):
                runs[-1] = (runs[-1][0], end)
            else:
                runs.append((start, end))
        result.extend(runs)
    return result


def compute_frequency_stats(
    transcript: Iterable[Mapping[str, Any]], duration_ms: int,
    presence_data: Mapping[str, Any] | None = None, *, coverage_complete: bool = True,
) -> dict[str, Any]:
    """Return deterministic most/least person modules or an explicit abstention."""
    _integer(duration_ms, "duration_ms")
    result: dict[str, Any] = {
        "schema_version": FREQUENCY_SCHEMA, "status": "unavailable", "reason": None,
        "session_duration_ms": duration_ms,
        "method": {
            "presence_source": PRESENCE_SOURCE,
            "eligibility": "2 * observed_presence_ms > session_duration_ms",
            "metric": "estimated_merged_utterances_per_observed_in_channel_minute",
            "merge_gap_ms": MERGE_GAP_MS, "merge_max_span_ms": MERGE_MAX_SPAN_MS,
            "interval_boundary": "[start_ms,end_ms)",
            "count_boundary": "original_contiguous_speech_start_in_observed_interval",
            "presence_is_sampling_estimate": True,
            "clock_duration_tolerance_ms": CLOCK_DURATION_TOLERANCE_MS,
        },
        "participants": [], "most_frequent": {}, "least_frequent": {},
    }

    def unavailable(reason: str) -> dict[str, Any]:
        result.update(status="unavailable", reason=reason)
        for key in ("most_frequent", "least_frequent"):
            result[key] = {"status": "unavailable", "reason": reason, "people": []}
        return result

    if duration_ms <= 0:
        return unavailable("session_duration_unavailable")
    if presence_data is None:
        return unavailable("reliable_presence_unavailable")
    try:
        intervals, names = observed_presence_intervals(presence_data, duration_ms)
    except PresenceClockDisagreement:
        return unavailable("clock_disagreement")
    except (ValueError, TypeError, AttributeError):
        return unavailable("invalid_presence_observations")
    segments = list(transcript)
    by_uid: dict[str, list[Mapping[str, Any]]] = {}
    real_speech_count = 0
    unresolved_identity = False
    try:
        for item in segments:
            if not isinstance(item, Mapping):
                return unavailable("invalid_transcript")
            if not _real_speech(item):
                continue
            start = _integer(item.get("start_ms"), "segment.start_ms")
            end = _integer(item.get("end_ms"), "segment.end_ms")
            if end < start:
                return unavailable("invalid_transcript")
            if start >= duration_ms or end == start:
                continue
            real_speech_count += 1
            uid = _uid(item.get("oopz_uid"))
            if not uid:
                unresolved_identity = True
                continue
            by_uid.setdefault(uid, []).append(item)
            if uid not in names and isinstance(item.get("speaker"), str):
                names[uid] = str(item["speaker"]).strip()
        for uid in sorted(intervals):
            presence_ms = sum(end - start for start, end in intervals[uid])
            runs = merged_speech_runs(by_uid.get(uid, []), intervals[uid])
            count = len(runs)
            result["participants"].append({
                "oopz_uid": uid, "nickname": names.get(uid) or "nickname-unavailable",
                "observed_presence_ms": presence_ms,
                "observed_presence_minutes": round(presence_ms / 60_000, 6),
                "presence_fraction": round(presence_ms / duration_ms, 6),
                "eligible": 2 * presence_ms > duration_ms,
                "merged_utterance_count": count, "count_is_estimate": True,
                "utterances_per_minute": round(count * 60_000 / presence_ms, 6) if presence_ms else None,
            })
    except (ValueError, TypeError):
        return unavailable("invalid_transcript")
    if coverage_complete is not True:
        return unavailable("incomplete_transcript_coverage")
    if unresolved_identity:
        return unavailable("unresolved_speaker_identity")
    if not real_speech_count:
        return unavailable("no_valid_speech")
    eligible = [person for person in result["participants"] if person["eligible"]]
    if not eligible:
        return unavailable("no_eligible_participants")
    if len(eligible) < 2:
        return unavailable("insufficient_eligible_comparison")
    if not any(person["merged_utterance_count"] for person in eligible):
        return unavailable("no_observed_speech_for_eligible_participants")
    rates = {p["oopz_uid"]: Fraction(p["merged_utterance_count"], p["observed_presence_ms"]) for p in eligible}
    for key, winning_rate in (("most_frequent", max(rates.values())), ("least_frequent", min(rates.values()))):
        result[key] = {
            "status": "available", "reason": None,
            "people": [dict(p) for p in eligible if rates[p["oopz_uid"]] == winning_rate],
        }
        result[key]["tied"] = len(result[key]["people"]) > 1
    result.update(status="available", reason=None)
    return result


def load_frequency_stats(
    session_dir: Path, transcript: Iterable[Mapping[str, Any]], duration_ms: int,
    *, coverage_complete: bool = True,
) -> dict[str, Any]:
    """Read the optional additive sidecar; absent/corrupt evidence fails closed."""
    path = session_dir / PRESENCE_FILENAME
    data = None
    invalid = False
    if path.is_file():
        try:
            if path.is_symlink() or path.resolve().parent != session_dir.resolve():
                raise ValueError("unsafe presence path")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("session_id") != session_dir.name:
                raise ValueError("presence session mismatch")
        except (OSError, UnicodeError, ValueError):
            invalid = True
    return compute_frequency_stats(
        transcript, duration_ms, {} if invalid else data, coverage_complete=coverage_complete,
    )
