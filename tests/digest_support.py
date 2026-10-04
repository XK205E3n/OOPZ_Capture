"""Shared fixtures for the digest renderer tests. All data is fictional."""
from pathlib import Path
import copy
import json
import tempfile

from oopz_capture.digest.render.pipeline import generate
from oopz_capture.digest.render.tokens import FONT_DIR
from oopz_capture.digest.stats import compute_frequency_stats

ROOT = FONT_DIR  # renderer font directory (tests that point at a missing font use ROOT / "nope.otf")
FIX = Path(__file__).resolve().parent / "fixtures" / "digest"
CASES = ("normal", "long_cjk", "long_id", "sparse", "unknown_icon")

DEFAULT_ROSTER = [("demo-a", "演示甲"), ("demo-b", "演示乙"), ("demo-c", "演示丙"), ("demo-d", "演示丁"), ("demo-e", "演示戊")]
# (minutes observed in channel, merged speech runs). demo-d is exactly half the session: excluded.
SCRIPT = [(60, 24), (45, 8), (50, 16), (30, 100), (40, 4)]
DURATION_MS = 3_600_000


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def base_case(case: str = "normal"):
    """(model, evidence bundle, metadata) as fresh deep copies."""
    return (copy.deepcopy(load(f"{case}.input.json")), copy.deepcopy(load(f"{case}.evidence.json")),
            copy.deepcopy(load(f"{case}.metadata.json")))


def tmpdir(prefix="oopz-digest-"):
    return tempfile.TemporaryDirectory(prefix=prefix)


def build_stats(root: Path | None = None, roster=None) -> dict:
    """Fictional statistics produced by the real deterministic counting rules."""
    roster = list(roster or DEFAULT_ROSTER)
    people = [(uid, name, *SCRIPT[i % len(SCRIPT)]) for i, (uid, name) in enumerate(roster)]
    observations = []
    for minute in range(61):
        t = minute * 60_000
        observations.append({"kind": "snapshot", "request_started_ms": t, "observed_ms": t, "connection_episode": 1,
                             "people": [{"oopz_uid": uid, "nickname": name, "is_bot": False}
                                        for uid, name, minutes, count in people if minute <= minutes]})
    presence = {"schema_version": "oopz.presence.observations.v1", "source": "successful_membership_snapshot_pairs",
                "clock": "monotonic_session_elapsed_ms", "session_id": "synthetic-session",
                "self_oopz_uid": "synthetic-recorder", "max_snapshot_gap_ms": 60_000, "finalized": True,
                "duration_ms": DURATION_MS, "monotonic_duration_ms": DURATION_MS, "observations": observations,
                "simulation": True}
    transcript = []
    for uid, name, minutes, count in people:
        for i in range(count):
            start = int((i + .5) * minutes * 60_000 / count)
            for offset in (0, 1200):  # two spans 1.2 s apart merge into one run
                transcript.append({"oopz_uid": uid, "speaker": name, "start_ms": start + offset,
                                   "end_ms": start + offset + 1000, "text": "合成发言片段，仅用于测试。",
                                   "transcript_source": "synthetic-asr", "language": "zh"})
    transcript.sort(key=lambda v: (v["start_ms"], v["oopz_uid"]))
    stats = compute_frequency_stats(transcript, DURATION_MS, presence, coverage_complete=True)
    stats.update(simulation=True, simulation_notice="统计演示：进出时间/发言计数为模拟", session_id="synthetic-session")
    return stats


def run(case: str = "normal", out: Path | None = None) -> dict:
    """Render one fictional case (the V7 demo entry point)."""
    model, bundle, meta = base_case(case)
    roster = [(p["speaker_id"], p["nickname"]) for p in bundle["people"]]
    stats = build_stats(roster=roster) if case != "sparse" else None
    avatars = {p.stem: p for p in (FIX / "avatars").glob("*.png")}
    out = Path(out) if out else Path(tempfile.mkdtemp(prefix=f"oopz-digest-{case}-"))
    return generate(model, bundle, meta, stats, out, avatars=avatars, case=case)


__all__ = ["ROOT", "FIX", "CASES", "run", "build_stats", "generate", "load", "base_case", "tmpdir", "copy"]
