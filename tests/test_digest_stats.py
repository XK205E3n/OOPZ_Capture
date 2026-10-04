"""Statistics boundaries. The numbers come from oopz_capture.digest.stats (unchanged);
these tests pin its behaviour as the template relies on it, and test our display layer."""
import unittest
from fractions import Fraction

from digest_support import ROOT, base_case, build_stats, tmpdir  # noqa: F401  (sys.path setup)

from oopz_capture.digest.render.stats_view import build_stats_view, format_rate
from oopz_capture.digest.render.textlayout import RenderError
from oopz_capture.digest.render.view import load_labels
from oopz_capture.digest.stats import compute_frequency_stats, merged_speech_runs

SESSION = 600_000          # 10 minutes: half is exactly 300_000 ms


def presence(duration, present_ms, step=1000):
    obs = []
    for t in range(0, duration + 1, step):
        obs.append({"kind": "snapshot", "request_started_ms": t, "observed_ms": t, "connection_episode": 1,
                    "people": [{"oopz_uid": u, "nickname": u.upper(), "is_bot": False}
                               for u, ms in present_ms.items() if t <= ms]})
    return {"schema_version": "oopz.presence.observations.v1", "source": "successful_membership_snapshot_pairs",
            "clock": "monotonic_session_elapsed_ms", "session_id": "s", "self_oopz_uid": "rec", "max_snapshot_gap_ms": 60_000,
            "finalized": True, "duration_ms": duration, "monotonic_duration_ms": duration, "observations": obs}


def speech(uid, starts, length=500):
    return [{"oopz_uid": uid, "speaker": uid.upper(), "start_ms": s, "end_ms": s + length, "text": "合成",
             "transcript_source": "synthetic-asr", "language": "zh"} for s in starts]


def participants(stats):
    return {p["oopz_uid"]: p for p in stats["participants"]}


class EligibilityBoundary(unittest.TestCase):
    def stats_for(self, ms):
        pres = presence(SESSION, {"a": SESSION, "b": SESSION, "t": ms})
        tr = speech("a", range(0, 300_000, 20_000)) + speech("b", range(5_000, 300_000, 50_000)) + speech("t", [10_000, 100_000])
        return compute_frequency_stats(tr, SESSION, pres)

    def test_below_half_not_eligible(self):
        self.assertFalse(participants(self.stats_for(299_000))["t"]["eligible"])

    def test_exactly_half_not_eligible(self):
        p = participants(self.stats_for(300_000))["t"]
        self.assertEqual(p["observed_presence_ms"], 300_000)
        self.assertFalse(p["eligible"])

    def test_slightly_above_half_eligible(self):
        p = participants(self.stats_for(301_000))["t"]
        self.assertTrue(p["eligible"])

    def test_eligibility_uses_full_session_denominator_not_active_window(self):
        """Everyone stops talking after 2 minutes, but the denominator stays the whole session."""
        pres = presence(SESSION, {"a": SESSION, "b": SESSION, "t": 290_000})
        tr = speech("a", [1000, 30_000]) + speech("b", [2000, 60_000]) + speech("t", [3000])
        s = compute_frequency_stats(tr, SESSION, pres)
        self.assertEqual(s["session_duration_ms"], SESSION)
        self.assertFalse(participants(s)["t"]["eligible"])   # 290 s is more than half of the 120 s activity window


class PresenceAndSpeech(unittest.TestCase):
    def test_duplicate_and_overlapping_observation_snapshots_do_not_double_count(self):
        pres = presence(SESSION, {"a": SESSION, "b": SESSION})
        pres["observations"] = pres["observations"] + [dict(pres["observations"][100]) for _ in range(3)]
        pres["observations"].sort(key=lambda o: o["observed_ms"])
        s = compute_frequency_stats(speech("a", [1000]) + speech("b", [2000]), SESSION, pres)
        self.assertLessEqual(participants(s)["a"]["observed_presence_ms"], SESSION)
        self.assertEqual(participants(s)["a"]["observed_presence_ms"], SESSION)

    def test_presence_never_extrapolated_past_last_observation_or_session_end(self):
        pres = presence(SESSION, {"a": 400_000, "b": SESSION})
        s = compute_frequency_stats(speech("a", [1000]) + speech("b", [2000]), SESSION, pres)
        self.assertEqual(participants(s)["a"]["observed_presence_ms"], 400_000)
        self.assertLessEqual(max(p["observed_presence_ms"] for p in s["participants"]), SESSION)

    def test_zero_observation_zero_denominator(self):
        pres = presence(SESSION, {"a": SESSION, "b": SESSION, "z": 0})
        s = compute_frequency_stats(speech("a", [1000]) + speech("b", [2000]), SESSION, pres)
        z = participants(s).get("z")
        if z:   # present in one snapshot only -> zero observed time, never eligible, rate not computed
            self.assertEqual(z["observed_presence_ms"], 0)
            self.assertFalse(z["eligible"])
            self.assertIsNone(z["utterances_per_minute"])
        with self.assertRaises(RenderError):
            format_rate(3, 0)

    def test_missing_presence_abstains(self):
        s = compute_frequency_stats(speech("a", [1000]), SESSION, None)
        self.assertEqual(s["status"], "unavailable")
        self.assertEqual(s["most_frequent"]["people"], [])

    def test_merge_rule_counts(self):
        iv = [(0, SESSION)]
        merged = merged_speech_runs(speech("a", [0, 1500 + 500]), iv)          # gap = 1500 ms -> merge
        self.assertEqual(len(merged), 1)
        apart = merged_speech_runs(speech("a", [0, 2001 + 500]), iv)           # gap 2001 ms -> two runs
        self.assertEqual(len(apart), 2)
        self.assertEqual(len(merged_speech_runs(speech("a", [0, 0, 0]), iv)), 1)   # duplicate rows -> one run
        # chain longer than 30 s is split even if every gap is tiny
        chain = speech("a", [i * 1500 for i in range(0, 40)], length=1000)
        self.assertGreater(len(merged_speech_runs(chain, iv)), 1)

    def test_tail_silence_window_creates_no_award_and_keeps_denominator(self):
        pres = presence(SESSION, {"a": SESSION, "b": SESSION, "c": SESSION})
        tr = speech("a", range(0, 120_000, 10_000)) + speech("b", range(1000, 120_000, 30_000)) + speech("c", [5000, 60_000])
        s = compute_frequency_stats(tr, SESSION, pres)
        self.assertEqual(s["session_duration_ms"], SESSION)
        sv = build_stats_view({**s, "simulation": True}, metadata={"synthetic": True}, labels=load_labels())
        text = repr(sv)
        for word in ("沉默", "安静", "quiet", "silence"):
            self.assertNotIn(word, text)

    def test_demo_stats_use_strict_half_rule(self):
        s = build_stats()
        p = participants({"participants": [{**x, "oopz_uid": x["oopz_uid"]} for x in s["participants"]]})
        self.assertEqual(p["demo-d"]["observed_presence_ms"], 1_800_000)
        self.assertFalse(p["demo-d"]["eligible"])
        self.assertEqual(s["most_frequent"]["people"][0]["oopz_uid"], "demo-a")
        self.assertEqual(s["least_frequent"]["people"][0]["oopz_uid"], "demo-e")
        self.assertEqual(p["demo-a"]["merged_utterance_count"], 24)   # 48 raw spans merge pairwise


class DisplayLayer(unittest.TestCase):
    labels = load_labels()

    def test_rate_rounding_half_up_exact(self):
        self.assertEqual(format_rate(24, 3_600_000), "0.40")
        self.assertEqual(format_rate(1, 480_000), "0.13")      # 0.125 -> half-up
        self.assertEqual(format_rate(0, 60_000), "0.00")
        self.assertEqual(format_rate(5, 2_400_000), "0.13")    # 0.125

    def test_display_rounding_never_changes_who_is_listed(self):
        """Two exact rates that print identically: the card still names exactly the upstream winner."""
        a = {"oopz_uid": "a", "nickname": "甲", "merged_utterance_count": 7, "observed_presence_ms": 2_400_000}   # 0.175
        b = {"oopz_uid": "b", "nickname": "乙", "merged_utterance_count": 7, "observed_presence_ms": 2_401_000}   # 0.17492
        self.assertNotEqual(Fraction(7, 2_400_000), Fraction(7, 2_401_000))
        stats = {"status": "available", "simulation": True, "session_duration_ms": 3_600_000,
                 "most_frequent": {"status": "available", "people": [a], "tied": False},
                 "least_frequent": {"status": "available", "people": [b], "tied": False}}
        sv = build_stats_view(stats, metadata={"synthetic": True}, labels=self.labels)
        self.assertEqual([r["names"] for r in sv["rows"]], ["甲", "乙"])
        self.assertEqual(sv["rows"][0]["value"][:4], "0.18")
        self.assertEqual(sv["rows"][1]["value"][:4], "0.17")

    def test_tie_is_shown_as_tie(self):
        row = {"oopz_uid": "a", "nickname": "甲", "merged_utterance_count": 6, "observed_presence_ms": 3_600_000}
        row2 = dict(row, oopz_uid="b", nickname="乙")
        stats = {"status": "available", "simulation": True, "session_duration_ms": 3_600_000,
                 "most_frequent": {"status": "available", "people": [row, row2], "tied": True},
                 "least_frequent": {"status": "available", "people": [dict(row, oopz_uid="c", nickname="丙")], "tied": False}}
        sv = build_stats_view(stats, metadata={"synthetic": True}, labels=self.labels)
        self.assertEqual(sv["rows"][0]["names"], "甲、乙")
        self.assertEqual(sv["rows"][0]["detail"], "并列")

    def test_simulation_flag_must_match_metadata(self):
        s = build_stats()
        with self.assertRaises(RenderError):
            build_stats_view(s, metadata={"synthetic": False}, labels=self.labels)
        real = dict(s, simulation=False)
        with self.assertRaises(RenderError):
            build_stats_view(real, metadata={"synthetic": True}, labels=self.labels)

    def test_real_stats_carry_no_simulation_marker(self):
        s = dict(build_stats(), simulation=False)
        sv = build_stats_view(s, metadata={"synthetic": False}, labels=self.labels)
        self.assertIsNone(sv["notice"])
        self.assertTrue(all(r["tag"] is None for r in sv["rows"]))

    def test_simulated_stats_are_marked_everywhere(self):
        sv = build_stats_view(build_stats(), metadata={"synthetic": True}, labels=self.labels)
        self.assertIn("模拟", sv["notice"])
        self.assertTrue(all(r["tag"] == "模拟数据" for r in sv["rows"]))
        self.assertIn("虚构", sv["scope_note"])

    def test_unavailable_stats_are_honest(self):
        sv = build_stats_view({"status": "unavailable"}, metadata={}, labels=self.labels)
        self.assertEqual(sv["status"], "unavailable")
        self.assertNotIn("rows", sv)


if __name__ == "__main__":
    unittest.main()
