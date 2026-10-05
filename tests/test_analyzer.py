"""Analyzer: transcript runs, windows, and the whole pipeline against a scripted fake model."""
import json

import pytest

from oopz_capture.analyzer import pipeline
from oopz_capture.analyzer.backend import BackendError, parse_json_object
from oopz_capture.analyzer.pipeline import AnalysisError, analyze_session
from oopz_capture.analyzer.transcript import Run, clean, load_session, merge_runs
from oopz_capture.analyzer.windows import split_windows
from oopz_capture.digest.contract import validate_content

SPEAKERS = {"a" * 32: "阿甲", "b" * 32: "阿乙", "c" * 32: "阿丙"}


def segment(uid, start, text, length=900):
    return {"oopz_uid": uid, "speaker": SPEAKERS[uid], "start_ms": start, "end_ms": start + length, "text": text}


def make_session(tmp_path, count=60, unmapped=False):
    """A fictional session: three people, `count` sentences, about 12 seconds apart."""
    lines = ["今天先把语音设备调好再说", "这一关的路线我觉得可以换一条", "晚饭我们订外卖还是自己做",
             "你那边的麦克风声音有点小", "下次我们换个新游戏试试看"]
    segments = []
    for i in range(count):
        uid = list(SPEAKERS)[i % 3]
        segments.append(segment(uid, i * 12_000, f"{lines[i % 5]}，{lines[(i + 1) % 5]}，{lines[(i + 2) % 5]}{i}号"))
    if unmapped:   # an audio track the recorder could not match to any member
        segments += [{"oopz_uid": "", "agora_uid": 12345, "speaker": "nickname-unavailable", "start_ms": 5_000 + i * 60_000,
                      "end_ms": 6_000 + i * 60_000, "text": "这是一个没有身份的声音在说话"} for i in range(5)]
    (tmp_path / "transcript.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in segments),
                                               encoding="utf-8")
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "t", "started_at": "2026-10-03T06:00:00+00:00",
                                                       "capture_clock_started_at": "2026-10-03T06:00:00+00:00"}),
                                                encoding="utf-8")
    (tmp_path / "lifecycle.json").write_text(json.dumps({"stopped_at": "2026-10-03T07:00:00+00:00"}), encoding="utf-8")
    (tmp_path / "users.json").write_text(json.dumps(
        [{"oopz_uid": uid, "nickname": name, "is_bot": False} for uid, name in SPEAKERS.items()]
        + [{"oopz_uid": "d" * 32, "nickname": "机器人", "is_bot": True}], ensure_ascii=False), encoding="utf-8")
    return load_session(tmp_path)


class FakeModel:
    """Answers window/section/final requests with valid digests built from what it was shown."""

    def __init__(self, *, fail_windows=(), bad_first=False):
        self.requests, self.fail_windows, self.bad_first, self.calls = [], set(fail_windows), bad_first, 0

    def complete(self, system, user):
        body, _, feedback = user.partition("\n\n【上一次输出被程序拒绝】")
        request = json.loads(body)
        self.requests.append((request["mode"], feedback))
        self.calls += 1
        if request["mode"] == "window" and request["window"] in self.fail_windows:
            raise BackendError("simulated outage")
        if request["mode"] == "editor":
            return self._edit(request)
        if request["mode"] == "review":       # a reader with nothing to improve returns the copy as it is
            return json.dumps(request["current"] | {"labels": request["labels"]}, ensure_ascii=False)
        if self.bad_first and not feedback:
            return self._digest(request, anchor="这句话根本不在证据里")
        return self._digest(request)

    def _edit(self, request):
        """Rewrites every title and text, keeps evidence, and tags what it was given."""
        draft = request["draft"]
        section = draft["content"]
        entries = [section["odd_topic"], *section["topics"], *section["moments"],
                   *section["next_hooks"], *draft["people"]["profiles"]]
        for entry in entries:
            if entry["evidence_ids"]:
                entry["title"], entry["text"] = entry["title"][:4] + "改写", "改写后，一句吐槽"
        labels = {"odd": "离谱至极", "topics": ["笑出声"] * len(section["topics"]),
                  "moments": ["跑偏现场"] * len(section["moments"]), "timeline": ["小标题"] * len(request["flow"])}
        return json.dumps(draft | {"labels": labels}, ensure_ascii=False)

    def _digest(self, request, anchor=None):
        evidence = request["evidence"]
        if request["mode"] == "window":
            first = evidence[0]
            own = [e for e in evidence if e["speaker_id"] == first["speaker_id"]][:4]
            summary = {"title": "这一段的标题", "text": "这一段大家在聊，设备和游戏的事情", "evidence_ids": [first["id"]],
                       "anchor": anchor or first["text"][:8]}
            person = {"title": "先把设备调好", "text": "这一段里，这位朋友主要在处理设备问题",
                      "evidence_ids": [e["id"] for e in own], "anchor": own[0]["text"][:8],
                      "speaker_id": first["speaker_id"],
                      "nickname": next(p["nickname"] for p in request["people"] if p["speaker_id"] == first["speaker_id"])}
        else:
            first = evidence[0]                       # a window_summary (or section summary)
            summary = {"title": "整场的标题", "text": "整场主要聊了设备调试，和接下来想玩的游戏", "evidence_ids": [first["id"]],
                       "anchor": anchor or first["text"][:6]}
            person = None
            runs = [e for e in evidence if e["kind"] == "asr_excerpt"]
            if runs:
                first_run = runs[0]
                own = [e for e in runs if e["speaker_id"] == first_run["speaker_id"]][:4]
                person = {"title": "先把设备调好", "text": "整场里，这位朋友多次处理设备问题",
                          "evidence_ids": [e["id"] for e in own], "anchor": anchor or own[0]["text"][:8],
                          "speaker_id": first_run["speaker_id"],
                          "nickname": next(p["nickname"] for p in request["people"]
                                           if p["speaker_id"] == first_run["speaker_id"])}
        content = {"summary": summary,
                   "odd_topic": {"status": "none", "title": "没有明显候选",
                                 "text": "本次可用记录中，没有可确认的明显离奇话题或概念。",
                                 "evidence_ids": [], "anchor": "", "participant_ids": []},
                   "topics": [], "moments": [], "next_hooks": []}
        if request["mode"] == "final":
            del content["summary"]            # the poster has no overview block
        return json.dumps({"content": content, "people": {"profiles": [person] if person else []}},
                          ensure_ascii=False)


def test_runs_merge_per_speaker_and_keep_all_text():
    segments = [segment("a" * 32, 0, "你好"), segment("b" * 32, 500, "嗯"), segment("a" * 32, 1500, "今天怎么样"),
                segment("a" * 32, 9000, "后来的话"), segment("b" * 32, 9500, "，。"), segment("a" * 32, 20_000, "<|zh|>  ")]
    runs = merge_runs(segments)
    assert [(r.speaker_id[0], r.text) for r in runs] == [("a", "你好今天怎么样"), ("b", "嗯"), ("a", "后来的话")]
    assert [r.id for r in runs] == ["r0001", "r0002", "r0003"]          # time order; noise-only text is dropped
    assert clean("<|zh|> a   b ") == "a b"


def test_windows_cover_every_run_once_in_order():
    runs = [Run(f"r{i}", "a", i * 10_000, i * 10_000 + 500, "字" * 100) for i in range(300)]
    windows = split_windows(runs, max_chars=4000)
    assert [r.id for w in windows for r in w.runs] == [r.id for r in runs]
    assert all(w.chars <= 4000 + 100 for w in windows) and len(windows) >= 7
    assert [w.index for w in windows] == list(range(1, len(windows) + 1))


def test_tiny_windows_merge_into_a_neighbour():
    runs = [Run(f"r{i}", "a", i * 1000, i * 1000 + 500, "字" * c) for i, c in enumerate([5000, 5000, 10, 5])]
    assert [w.chars for w in split_windows(runs, max_chars=5500)] == [5000, 5015] or \
        sum(w.chars for w in split_windows(runs, max_chars=5500)) == 10015


def test_load_session_excludes_bots_and_computes_the_denominator(tmp_path):
    session = make_session(tmp_path)
    assert {p["nickname"] for p in session.roster} == set(SPEAKERS.values())
    assert session.duration_ms == 3_600_000 and session.started_at.strftime("%H:%M") == "14:00"


def test_pipeline_produces_a_valid_digest_and_shows_everything_to_the_model(tmp_path):
    session = make_session(tmp_path)
    model = FakeModel()
    windows = split_windows(session.runs, max_chars=600)
    analysis = analyze_session(session, model, parallelism=2, windows=windows)
    assert len(windows) > 2 and analysis.coverage["missing"] == []
    validate_content(analysis.content, analysis.bundle)
    window_requests = [r for r in model.requests if r[0] == "window"]
    assert len(window_requests) == len(windows)
    assert all(len(unit["notes"]["people"]["profiles"]) == 1 for unit in analysis.units)


def test_every_run_is_sent_to_a_window_call(tmp_path):
    session = make_session(tmp_path)
    seen = []

    class Spy(FakeModel):
        def complete(self, system, user):
            request = json.loads(user.partition("\n\n【上一次输出被程序拒绝】")[0])
            if request["mode"] == "window":
                seen.extend(e["id"] for e in request["evidence"])
            return super().complete(system, user)

    analyze_session(session, Spy(), windows=split_windows(session.runs, max_chars=400))
    assert seen == [run.id for run in session.runs]


def test_rejected_answer_is_retried_with_the_located_error(tmp_path):
    session = make_session(tmp_path, count=12)
    model = FakeModel(bad_first=True)
    analysis = analyze_session(session, model, windows=split_windows(session.runs, max_chars=100_000))
    assert analysis.coverage["missing"] == []
    assert any("anchor:not_verbatim_source" in feedback and feedback.count("@summary") >= 1 for _, feedback in model.requests)
    assert [c["error"] is not None for c in analysis.calls].count(True) == 2     # window and final were each rejected once


def test_failed_window_is_reported_not_hidden(tmp_path):
    session = make_session(tmp_path)
    windows = split_windows(session.runs, max_chars=400)
    analysis = analyze_session(session, FakeModel(fail_windows={2}), windows=windows)
    assert [m["window"] for m in analysis.coverage["missing"]] == ["w02"]
    assert analysis.coverage["windows_analyzed"] == len(windows) - 1
    validate_content(analysis.content, analysis.bundle)


def test_all_windows_failing_is_an_error(tmp_path):
    session = make_session(tmp_path, count=12)
    with pytest.raises(AnalysisError):
        analyze_session(session, FakeModel(fail_windows={1}), windows=split_windows(session.runs, max_chars=100_000))


def test_many_windows_are_merged_in_groups_before_the_final_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "MERGE_ABOVE", 3)
    monkeypatch.setattr(pipeline, "MERGE_GROUP", 2)
    session = make_session(tmp_path)
    model = FakeModel()
    analysis = analyze_session(session, model, windows=split_windows(session.runs, max_chars=500))
    assert "section" in {mode for mode, _ in model.requests} and [m for m, _ in model.requests][-3:] == ["final", "editor", "review"]
    validate_content(analysis.content, analysis.bundle)


def test_json_reply_parsing():
    assert parse_json_object('好的：\n```json\n{"a": 1}\n```') == {"a": 1}
    for bad in ("没有对象", "[1, 2]"):
        with pytest.raises(ValueError):
            parse_json_object(bad)


def test_unmapped_audio_track_gets_a_stable_id_and_a_neutral_name(tmp_path):
    session = make_session(tmp_path, unmapped=True)
    unknown = [p for p in session.roster if p["speaker_id"].startswith("agora-")]
    assert unknown == [{"speaker_id": "agora-12345", "nickname": "未识别成员"}]
    assert all(run.speaker_id for run in session.runs)


def test_digits_inside_a_nickname_are_not_numeric_claims():
    people = [{"speaker_id": "u1", "nickname": "星铸E3"}]
    evidence = [{"id": "r1", "kind": "asr_excerpt", "speaker_id": "u1", "start_ms": 0, "end_ms": 1000,
                 "text": "我们先把设备调好再开始吧"}]
    def content(text):
        item = {"title": "设备先行", "text": text, "evidence_ids": ["r1"], "anchor": "我们先把设备调好"}
        none = {"status": "none", "title": "没有明显候选", "text": "本次可用记录中，没有可确认的明显离奇话题或概念。",
                "evidence_ids": [], "anchor": "", "participant_ids": []}
        return {"content": {"summary": item, "odd_topic": none, "topics": [], "moments": [], "next_hooks": []},
                "people": {"profiles": []}}
    validate_content(content("星铸E3提议先把设备调好"), {"people": people, "evidence": evidence})
    with pytest.raises(ValueError, match="unsupported_numeric_claim"):
        validate_content(content("星铸E3提议先调三次设备，大约3次"), {"people": people, "evidence": evidence})


def test_a_failed_analysis_still_saves_what_was_asked(tmp_path):
    from oopz_capture.analyzer.outputs import save_failure
    error = AnalysisError("final all: rejected 3 times", [{"id": "w01", "notes": None}],
                          [{"stage": "final", "unit": "all", "attempt": 1, "error": "anchor:not_verbatim_source@summary"}])
    save_failure(tmp_path / "out", error)
    assert json.loads((tmp_path / "out" / "failure.json").read_text(encoding="utf-8"))["error"].startswith("final")
    assert "anchor:not_verbatim_source@summary" in (tmp_path / "out" / "calls.jsonl").read_text(encoding="utf-8")


@pytest.mark.needs_fonts
def test_analysis_saves_and_renders_a_card(tmp_path):
    from oopz_capture.analyzer.outputs import render, save
    (tmp_path / "s").mkdir()
    session = make_session(tmp_path / "s")
    analysis = analyze_session(session, FakeModel(), windows=split_windows(session.runs, max_chars=600))
    save(session, tmp_path / "s", analysis, tmp_path / "out")
    manifest = render(tmp_path / "out")
    assert (tmp_path / "out" / "digest" / "digest.png").stat().st_size > 10_000
    meta = json.loads((tmp_path / "out" / "meta.json").read_text(encoding="utf-8"))
    assert meta["session"] == {"date_label": "2026.10.03", "time_label": "14:00 — 15:00"} and manifest["width"] > 0
    assert json.loads((tmp_path / "out" / "stats.json").read_text(encoding="utf-8"))["status"] == "unavailable"


def test_normalize_settles_harmless_details_only():
    from oopz_capture.analyzer.pipeline import normalize
    content = {"content": {"summary": {"evidence_ids": ["a", "a", "b", "c", "d", "e", "f", "g"]},
                           "odd_topic": {"status": "none", "title": "别的写法"}, "topics": [], "moments": [], "next_hooks": []},
               "people": {"profiles": [{"icon_category": "gaming", "evidence_ids": ["r1"]}]}}
    result = normalize(content)
    assert result["content"]["summary"]["evidence_ids"] == ["a", "b", "c", "d", "e", "f"]
    assert result["content"]["odd_topic"]["title"] == "没有明显候选"
    assert "icon_category" not in result["people"]["profiles"][0]


@pytest.mark.needs_fonts
def test_a_card_that_is_too_tall_is_sent_back_with_the_height(tmp_path):
    import copy
    from oopz_capture.analyzer.outputs import card_fit_check
    from digest_support import base_case
    (tmp_path / "s").mkdir()
    session = make_session(tmp_path / "s")
    content, bundle, _ = base_case()
    long_text = ("这一段在聊设备调试和游戏路线的安排" * 30)[:450]
    tall = copy.deepcopy(content)
    for item in (*tall["content"]["topics"], *tall["content"]["moments"], *tall["people"]["profiles"]):
        item["text"] = long_text
    tall["content"]["summary"]["text"] = long_text
    check = card_fit_check(session, tmp_path / "s")
    check(content, bundle, {"missing": []}, [])                    # the normal card fits
    with pytest.raises(ValueError, match=r"card:too_tall.*px high"):
        check(tall, bundle, {"missing": []}, [])


def test_too_many_people_are_sent_back(tmp_path):
    session = make_session(tmp_path, count=12)

    class TooMany(FakeModel):
        def _digest(self, request, anchor=None):
            content = json.loads(super()._digest(request, anchor))
            if request["mode"] == "final" and not self.requests[-1][1]:
                content["people"]["profiles"] = [dict(content["people"]["profiles"][0], title=t) for t in ("称号甲", "称号乙", "称号丙", "称号丁", "称号戊")]
            return json.dumps(content, ensure_ascii=False)

    model = TooMany()
    analyze_session(session, model, windows=split_windows(session.runs, max_chars=100_000))
    assert any("style:people.profiles" in feedback for _, feedback in model.requests)


def test_flow_becomes_the_timeline_of_the_card(tmp_path):
    from oopz_capture.analyzer.outputs import build_metadata
    session = make_session(tmp_path)
    analysis = analyze_session(session, FakeModel(), windows=split_windows(session.runs, max_chars=600))
    meta = build_metadata(session, analysis.coverage, analysis.flow)
    assert [t["title"] for t in meta["timeline"]][0].startswith("14:00") and len(meta["timeline"]) == len(analysis.flow)
    assert all(t["text"] == "小标题" for t in meta["timeline"])        # the editor's short timeline captions


def test_every_faulty_entry_is_reported_at_once():
    from digest_support import base_case
    content, bundle, _ = base_case()
    content["content"]["summary"]["anchor"] = "这句话不在任何证据里"
    content["content"]["topics"][0]["anchor"] = "另一句也不在证据里"
    with pytest.raises(ValueError) as caught:
        validate_content(content, bundle)
    message = str(caught.value)
    assert "@summary" in message and "@topics[0]" in message and "这句话不在任何证据里" in message


def test_editor_rewrites_text_keeps_evidence_and_supplies_tags(tmp_path):
    from oopz_capture.analyzer.outputs import build_metadata
    session = make_session(tmp_path)
    analysis = analyze_session(session, FakeModel(), windows=split_windows(session.runs, max_chars=100_000))
    assert analysis.edited and analysis.content["people"]["profiles"][0]["title"].endswith("改写")
    assert "summary" not in analysis.content["content"] and analysis.content["people"]["profiles"][0]["evidence_ids"]  # evidence untouched and revalidated
    assert analysis.labels["timeline"] == ["小标题"] * len(analysis.flow)
    assert build_metadata(session, analysis.coverage, analysis.flow, analysis.labels).get("topic_labels", []) == []


def test_the_editor_rereads_its_copy_and_keeps_a_good_review(tmp_path):
    class Reviewer(FakeModel):
        def complete(self, system, user):
            request = json.loads(user.partition("\n\n【上一次输出被程序拒绝】")[0])
            if request["mode"] == "review":
                self.requests.append(("review", ""))
                current = request["current"]
                current["people"]["profiles"][0]["title"] = current["people"]["profiles"][0]["title"][:2] + "读顺了"
                return json.dumps(current | {"labels": request["labels"]}, ensure_ascii=False)
            return super().complete(system, user)

    session = make_session(tmp_path)
    model = Reviewer()
    analysis = analyze_session(session, model, windows=split_windows(session.runs, max_chars=100_000))
    assert analysis.edited and analysis.content["people"]["profiles"][0]["title"].endswith("读顺了")
    assert [m for m, _ in model.requests].count("review") == 2         # a changed copy is read once more


def test_a_failed_review_keeps_the_last_good_copy(tmp_path, capsys):
    class BadReviewer(FakeModel):
        def complete(self, system, user):
            if json.loads(user.partition("\n\n【上一次输出被程序拒绝】")[0])["mode"] == "review":
                return "not json"
            return super().complete(system, user)

    session = make_session(tmp_path)
    analysis = analyze_session(session, BadReviewer(), windows=split_windows(session.runs, max_chars=100_000))
    assert analysis.edited and analysis.content["people"]["profiles"][0]["title"].endswith("改写")
    assert "[审稿]" in capsys.readouterr().out


def test_editor_with_wrong_tag_count_is_sent_back_then_falls_back_to_the_draft(tmp_path, capsys):
    class BadEditor(FakeModel):
        def _edit(self, request):
            return json.dumps(request["draft"] | {"labels": {"odd": "x", "topics": ["多余"], "moments": [], "timeline": []}},
                              ensure_ascii=False)

    session = make_session(tmp_path)
    analysis = analyze_session(session, BadEditor(), windows=split_windows(session.runs, max_chars=100_000))
    assert not analysis.edited and analysis.labels == {}
    assert analysis.content["people"]["profiles"][0]["title"] == "先把设备调好"       # the checked draft is kept
    assert "EDITOR FAILED" in capsys.readouterr().out


def test_topics_must_name_who_did_it():
    from oopz_capture.analyzer.pipeline import check_named
    roster = [{"speaker_id": "a" * 32, "nickname": "问夏"}, {"speaker_id": "b" * 32, "nickname": "未识别成员"}]
    content = {"content": {"topics": [{"title": "萝卜惨案", "text": "问夏数萝卜数到崩溃"}], "moments": []}}
    check_named(content, roster)
    content["content"]["topics"][0]["text"] = "有人数萝卜数到崩溃"
    with pytest.raises(ValueError, match="topics.0. names nobody"):
        check_named(content, roster)


def test_window_results_of_an_earlier_run_can_be_reused(tmp_path):
    session = make_session(tmp_path)
    windows = split_windows(session.runs, max_chars=600)
    first = analyze_session(session, FakeModel(), windows=windows)
    model = FakeModel()
    again = analyze_session(session, model, windows=windows, units=first.units)
    assert "window" not in {mode for mode, _ in model.requests} and again.content["people"]["profiles"]


def test_long_text_without_punctuation_is_refused():
    from oopz_capture.analyzer.pipeline import check_style
    content = {"content": {"summary": {"title": "标题", "text": "一" * 20}, "odd_topic": {"status": "none", "title": "无", "text": "无，"},
                           "topics": [], "moments": [], "next_hooks": []}, "people": {"profiles": []}}
    with pytest.raises(ValueError, match="no punctuation"):
        check_style(content)
    content["content"]["summary"]["text"] = "一" * 10 + "，" + "二" * 10
    check_style(content)


def _user(uid, nickname, status="inferred_person_pid", evidence=("Person PID has not yet appeared",), is_bot=False):
    return {"oopz_uid": uid, "nickname": nickname, "status": status, "evidence": list(evidence), "is_bot": is_bot}


def _segments():
    return [{"oopz_uid": "u1", "speaker": "Alice", "agora_uid": 11, "start_ms": 0, "end_ms": 1000, "text": "hello"},
            {"oopz_uid": "", "speaker": "nickname-unavailable", "agora_uid": 99, "start_ms": 2000, "end_ms": 3000, "text": "hi there"}]


def test_the_one_unmapped_track_goes_to_the_one_member_left_by_elimination():
    from oopz_capture.analyzer.transcript import infer_unmapped_speaker
    users = {"u1": _user("u1", "Alice", evidence=("OOPZ data_stream contained matching uid/cid",)),
             "u2": _user("u2", "Rola"),                                                    # only a person-id guess, never spoke
             "u3": _user("u3", "Cold", evidence=("OOPZ data_stream contained matching uid/cid",)),
             "bot": _user("bot", "Bot", is_bot=True)}
    segments = _segments()
    assert infer_unmapped_speaker(segments, users) == [{"agora_uid": 99, "oopz_uid": "u2", "nickname": "Rola", "method": "elimination"}]
    assert segments[1]["oopz_uid"] == "u2" and segments[1]["speaker"] == "Rola"


def test_elimination_changes_nothing_when_it_is_not_unique():
    from oopz_capture.analyzer.transcript import infer_unmapped_speaker
    two_guesses = {"u1": _user("u1", "Alice"), "u2": _user("u2", "Rola"), "u3": _user("u3", "Third")}
    segments = _segments()
    assert infer_unmapped_speaker(segments, two_guesses) == [] and segments[1]["oopz_uid"] == ""
    two_tracks = _segments() + [{"oopz_uid": "", "speaker": "x", "agora_uid": 77, "start_ms": 5000, "end_ms": 6000, "text": "yo"}]
    assert infer_unmapped_speaker(two_tracks, {"u2": _user("u2", "Rola")}) == []


def test_load_session_applies_the_inference_and_avatars_follow_the_roster(tmp_path):
    from oopz_capture.analyzer.outputs import avatar_paths
    from oopz_capture.analyzer.transcript import load_session
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "s", "started_at": "2026-10-03T06:00:00+00:00"}), encoding="utf-8")
    (tmp_path / "lifecycle.json").write_text(json.dumps({"stopped_at": "2026-10-03T07:00:00+00:00"}), encoding="utf-8")
    users = [_user("u1", "Alice", evidence=("data_stream",)), _user("u2", "Rola")]
    (tmp_path / "users.json").write_text(json.dumps(users), encoding="utf-8")
    (tmp_path / "transcript.jsonl").write_text("\n".join(json.dumps(s) for s in _segments()), encoding="utf-8")
    (tmp_path / "avatars").mkdir()
    (tmp_path / "avatars" / "a.png").write_bytes(b"png")
    (tmp_path / "avatars" / "index.json").write_text(json.dumps({"u1": "a.png", "u2": "missing.png", "stranger": "a.png"}), encoding="utf-8")

    session = load_session(tmp_path)

    assert {p["nickname"] for p in session.roster} == {"Alice", "Rola"}
    assert session.identity_inferred[0]["oopz_uid"] == "u2"
    assert avatar_paths(session, tmp_path) == {"u1": str(tmp_path / "avatars" / "a.png")}      # only people on the roster, only files that exist


def _entry(*ids, **extra):
    return {"title": "t", "text": "x", "evidence_ids": list(ids)} | extra


def _card(odd=None, topics=(), moments=(), profiles=()):
    odd = odd or {"status": "none", "evidence_ids": []}
    return {"content": {"odd_topic": odd, "topics": list(topics), "moments": list(moments), "next_hooks": []},
            "people": {"profiles": list(profiles)}}


def test_distinct_rejects_a_story_told_twice_and_names_both_blocks():
    card = _card(topics=[_entry("r1", "r2", "r3")], moments=[_entry("r1", "r2", "r9", stages=[])],
                 profiles=[_entry("r7", "r8")])
    with pytest.raises(ValueError, match=r"moments\[0\] tells the same story as topics\[0\]"):
        pipeline.check_distinct(card)
    card["content"]["moments"] = [_entry("r5", "r6")]
    pipeline.check_distinct(card)                                 # different lines: fine
    card["people"]["profiles"] = [_entry("r1", "r4")]            # a person retelling half of a topic
    with pytest.raises(ValueError, match=r"profiles\[0\] tells the same story as topics\[0\].*leave this person out"):
        pipeline.check_distinct(card)


def test_distinct_counts_stage_lines_and_ignores_window_summaries():
    moment = _entry("r1", stages=[{"label": "a", "evidence_ids": ["r2"]}, {"label": "b", "evidence_ids": ["r3"]}])
    with pytest.raises(ValueError):
        pipeline.check_distinct(_card(topics=[_entry("r2", "r3")], moments=[moment]))
    pipeline.check_distinct(_card(topics=[_entry("w01")], moments=[_entry("w01")]))


def test_drop_repeats_keeps_odd_then_moments_and_leaves_people():
    card = _card(odd=_entry("r1", status="supported"), topics=[_entry("r1", "r2"), _entry("r4", "r5"), _entry("r6", "r7")],
                 moments=[_entry("r6", "r8")], profiles=[_entry("r1", "r2")])
    pipeline.drop_repeats(card)
    assert [t["evidence_ids"] for t in card["content"]["topics"]] == [["r4", "r5"]]
    assert len(card["content"]["moments"]) == 1 and len(card["people"]["profiles"]) == 1


def test_usage_of_reads_the_cli_envelope_and_tolerates_missing_fields():
    from oopz_capture.analyzer.backend import usage_of

    got = usage_of({"duration_ms": 1200, "duration_api_ms": 1100, "num_turns": 1, "total_cost_usd": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 5, "cache_read_input_tokens": 3,
                              "context_usage_ratio": 0.1}})
    assert got["cli_ms"] == 1200 and got["output_tokens"] == 5 and got["cache_read_tokens"] == 3
    assert got["context_ratio"] == 0.1 and got["credits"] == 0
    assert usage_of({})["context_ratio"] == 0.0


def test_a_very_long_quiet_session_is_cut_by_time():
    runs = [Run(id=f"r{i:04d}", speaker_id="a" * 32, start_ms=i * 1_800_000, end_ms=i * 1_800_000 + 1000,
                text="一句话说得很长" * 30) for i in range(6)]
    assert len(split_windows(runs)) == 2          # 2 hours is the longest a window may span
    assert len(split_windows(runs[:4])) == 1


def test_usage_text_lists_model_requests_and_time():
    from oopz_capture.digest_job import usage_text

    calls = [{"seconds": 97.0, "error": "x", "usage": {"cli_runs": 1}}, {"seconds": 130.0, "error": None, "usage": {"cli_runs": 2}},
             {"seconds": 31.7, "error": None}]
    text = usage_text("Qwen3.8-Flash", calls, 310.4)
    assert text.splitlines() == ["分析用量", "模型：Qwen3.8-Flash", "请求：4 次（其中 1 次因校验未通过而重试）",
                                 "总耗时：5 分 10 秒（模型调用合计 4 分 19 秒）"]
    assert "重试" not in usage_text("m", [{"seconds": 5.0, "error": None}], 8)


def test_budget_grows_with_the_recording_and_reaches_the_prompt():
    from oopz_capture.analyzer import prompts
    from oopz_capture.analyzer.pipeline import budget_for

    hour = 3_600_000
    short, long = budget_for(2 * hour), budget_for(12 * hour)
    assert (short.topics, short.moments, short.hooks) == (3, 1, 1)
    assert long.topics > short.topics and long.chars[1] > short.chars[1] and long.profiles == 7
    assert budget_for(4 * hour).topics == 4 and budget_for(0).topics == 3
    prompt = prompts.system_prompt("final", short)
    assert "最多3项" in prompt and "400到700" in prompt and "写空数组" in prompt and "@" not in prompt
    assert "summary" not in prompts.system_prompt("final", short).split("【输出结构示例")[1].split("odd_topic 没有候选")[0]
    assert '"summary":' in prompts.system_prompt("window")


def test_style_enforces_the_budget_counts():
    from oopz_capture.analyzer.pipeline import Budget, check_style

    tiny = Budget(1, 0, 0, 1, (1, 2))
    card = _card(topics=[_entry("r1")], moments=[_entry("r2", stages=[])])
    with pytest.raises(ValueError, match="moments has 1 entries but at most 0"):
        check_style(card, tiny)
    check_style(card)                                              # the largest budget accepts it


def test_repair_fixes_only_mechanical_gaps():
    from oopz_capture.analyzer.pipeline import repair

    bundle = {"people": [], "evidence": [
        {"id": "r1", "kind": "asr_excerpt", "speaker_id": "u1", "text": "甲说的话"},
        {"id": "r2", "kind": "asr_excerpt", "speaker_id": "u2", "text": "乙说的话"},
        {"id": "r3", "kind": "asr_excerpt", "speaker_id": "u1", "text": "甲又说了"}]}
    card = _card(odd={"title": "t", "text": "x", "evidence_ids": ["r1", "r2", "r1", "r99"], "anchor": "甲说的话"},
                 moments=[_entry("r3", "r99", stages=[{"label": "步", "evidence_ids": ["rX", "r1"], "anchor": "甲说"}])])
    repair(card, bundle)
    odd = card["content"]["odd_topic"]
    assert odd["status"] == "supported" and odd["participant_ids"] == ["u1", "u2"]
    assert odd["evidence_ids"] == ["r1", "r2", "r1"]               # the unknown id went, duplicates are normalize's job
    moment = card["content"]["moments"][0]
    assert moment["evidence_ids"] == ["r3"] and moment["stages"][0]["icon_category"] == "other"
    assert moment["stages"][0]["evidence_ids"] == ["r1"]
    lone = _card(topics=[_entry("r99")])
    repair(lone, bundle)
    assert lone["content"]["topics"][0]["evidence_ids"] == ["r99"]  # nothing known is left: the validator will say so


def test_json_reply_may_carry_text_after_the_object():
    assert parse_json_object('说明\n{"a": {"b": 1}}\n{"c": 2} 多余') == {"a": {"b": 1}}


def test_titles_may_carry_any_punctuation_that_is_not_counted():
    from oopz_capture.analyzer.pipeline import check_titles, title_length

    card = _card(topics=[_entry("r1")], profiles=[_entry("r2")])
    for title in ("萝卜惨案", "工作台居然被狼叼走了？！", "三秒钟社会性死亡……", "龙蛋大业：一句话蒸发"):
        card["content"]["topics"][0]["title"] = title
        check_titles(card)
    assert title_length("工作台居然被狼叼走了？！") == 10
    card["content"]["topics"][0]["title"] = "一二三四五六七八九十一二三"
    with pytest.raises(ValueError, match="13 characters .* at most 12"):
        check_titles(card)
    card["content"]["topics"][0]["title"] = "一二三四五六七八"
    card["people"]["profiles"][0]["title"] = "人物称号也不能超过十二个字啊真的！"
    with pytest.raises(ValueError, match="at most 12"):
        check_titles(card)


def test_the_editor_rereads_its_copy_and_keeps_a_good_review(tmp_path):
    class Reviewer(FakeModel):
        def complete(self, system, user):
            request = json.loads(user.partition("\n\n【上一次输出被程序拒绝】")[0])
            if request["mode"] == "review":
                self.requests.append(("review", ""))
                current = request["current"]
                current["people"]["profiles"][0]["title"] = current["people"]["profiles"][0]["title"][:2] + "读顺了"
                return json.dumps(current | {"labels": request["labels"]}, ensure_ascii=False)
            return super().complete(system, user)

    session = make_session(tmp_path)
    model = Reviewer()
    analysis = analyze_session(session, model, windows=split_windows(session.runs, max_chars=100_000))
    assert analysis.edited and analysis.content["people"]["profiles"][0]["title"].endswith("读顺了")
    assert [m for m, _ in model.requests].count("review") == 2         # a changed copy is read once more


def test_a_failed_review_keeps_the_last_good_copy(tmp_path, capsys):
    class BadReviewer(FakeModel):
        def complete(self, system, user):
            if json.loads(user.partition("\n\n【上一次输出被程序拒绝】")[0])["mode"] == "review":
                return "not json"
            return super().complete(system, user)

    session = make_session(tmp_path)
    analysis = analyze_session(session, BadReviewer(), windows=split_windows(session.runs, max_chars=100_000))
    assert analysis.edited and analysis.content["people"]["profiles"][0]["title"].endswith("改写")
    assert "[审稿]" in capsys.readouterr().out


def test_editor_with_wrong_tag_count_is_sent_back_then_falls_back_to_the_draft(tmp_path, capsys):
    class BadEditor(FakeModel):
        def _edit(self, request):
            return json.dumps(request["draft"] | {"labels": {"odd": "x", "topics": ["多余"], "moments": [], "timeline": []}},
                              ensure_ascii=False)

    session = make_session(tmp_path)
    analysis = analyze_session(session, BadEditor(), windows=split_windows(session.runs, max_chars=100_000))
    assert not analysis.edited and analysis.labels == {}
    assert analysis.content["people"]["profiles"][0]["title"] == "先把设备调好"       # the checked draft is kept
    assert "EDITOR FAILED" in capsys.readouterr().out


def test_topics_must_name_who_did_it():
    from oopz_capture.analyzer.pipeline import check_named
    roster = [{"speaker_id": "a" * 32, "nickname": "问夏"}, {"speaker_id": "b" * 32, "nickname": "未识别成员"}]
    content = {"content": {"topics": [{"title": "萝卜惨案", "text": "问夏数萝卜数到崩溃"}], "moments": []}}
    check_named(content, roster)
    content["content"]["topics"][0]["text"] = "有人数萝卜数到崩溃"
    with pytest.raises(ValueError, match="topics.0. names nobody"):
        check_named(content, roster)


def test_window_results_of_an_earlier_run_can_be_reused(tmp_path):
    session = make_session(tmp_path)
    windows = split_windows(session.runs, max_chars=600)
    first = analyze_session(session, FakeModel(), windows=windows)
    model = FakeModel()
    again = analyze_session(session, model, windows=windows, units=first.units)
    assert "window" not in {mode for mode, _ in model.requests} and again.content["people"]["profiles"]


def test_long_text_without_punctuation_is_refused():
    from oopz_capture.analyzer.pipeline import check_style
    content = {"content": {"summary": {"title": "标题", "text": "一" * 20}, "odd_topic": {"status": "none", "title": "无", "text": "无，"},
                           "topics": [], "moments": [], "next_hooks": []}, "people": {"profiles": []}}
    with pytest.raises(ValueError, match="no punctuation"):
        check_style(content)
    content["content"]["summary"]["text"] = "一" * 10 + "，" + "二" * 10
    check_style(content)


def _user(uid, nickname, status="inferred_person_pid", evidence=("Person PID has not yet appeared",), is_bot=False):
    return {"oopz_uid": uid, "nickname": nickname, "status": status, "evidence": list(evidence), "is_bot": is_bot}


def _segments():
    return [{"oopz_uid": "u1", "speaker": "Alice", "agora_uid": 11, "start_ms": 0, "end_ms": 1000, "text": "hello"},
            {"oopz_uid": "", "speaker": "nickname-unavailable", "agora_uid": 99, "start_ms": 2000, "end_ms": 3000, "text": "hi there"}]


def test_the_one_unmapped_track_goes_to_the_one_member_left_by_elimination():
    from oopz_capture.analyzer.transcript import infer_unmapped_speaker
    users = {"u1": _user("u1", "Alice", evidence=("OOPZ data_stream contained matching uid/cid",)),
             "u2": _user("u2", "Rola"),                                                    # only a person-id guess, never spoke
             "u3": _user("u3", "Cold", evidence=("OOPZ data_stream contained matching uid/cid",)),
             "bot": _user("bot", "Bot", is_bot=True)}
    segments = _segments()
    assert infer_unmapped_speaker(segments, users) == [{"agora_uid": 99, "oopz_uid": "u2", "nickname": "Rola", "method": "elimination"}]
    assert segments[1]["oopz_uid"] == "u2" and segments[1]["speaker"] == "Rola"


def test_elimination_changes_nothing_when_it_is_not_unique():
    from oopz_capture.analyzer.transcript import infer_unmapped_speaker
    two_guesses = {"u1": _user("u1", "Alice"), "u2": _user("u2", "Rola"), "u3": _user("u3", "Third")}
    segments = _segments()
    assert infer_unmapped_speaker(segments, two_guesses) == [] and segments[1]["oopz_uid"] == ""
    two_tracks = _segments() + [{"oopz_uid": "", "speaker": "x", "agora_uid": 77, "start_ms": 5000, "end_ms": 6000, "text": "yo"}]
    assert infer_unmapped_speaker(two_tracks, {"u2": _user("u2", "Rola")}) == []


def test_load_session_applies_the_inference_and_avatars_follow_the_roster(tmp_path):
    from oopz_capture.analyzer.outputs import avatar_paths
    from oopz_capture.analyzer.transcript import load_session
    (tmp_path / "session.json").write_text(json.dumps({"session_id": "s", "started_at": "2026-10-03T06:00:00+00:00"}), encoding="utf-8")
    (tmp_path / "lifecycle.json").write_text(json.dumps({"stopped_at": "2026-10-03T07:00:00+00:00"}), encoding="utf-8")
    users = [_user("u1", "Alice", evidence=("data_stream",)), _user("u2", "Rola")]
    (tmp_path / "users.json").write_text(json.dumps(users), encoding="utf-8")
    (tmp_path / "transcript.jsonl").write_text("\n".join(json.dumps(s) for s in _segments()), encoding="utf-8")
    (tmp_path / "avatars").mkdir()
    (tmp_path / "avatars" / "a.png").write_bytes(b"png")
    (tmp_path / "avatars" / "index.json").write_text(json.dumps({"u1": "a.png", "u2": "missing.png", "stranger": "a.png"}), encoding="utf-8")

    session = load_session(tmp_path)

    assert {p["nickname"] for p in session.roster} == {"Alice", "Rola"}
    assert session.identity_inferred[0]["oopz_uid"] == "u2"
    assert avatar_paths(session, tmp_path) == {"u1": str(tmp_path / "avatars" / "a.png")}      # only people on the roster, only files that exist


def _entry(*ids, **extra):
    return {"title": "t", "text": "x", "evidence_ids": list(ids)} | extra


def _card(odd=None, topics=(), moments=(), profiles=()):
    odd = odd or {"status": "none", "evidence_ids": []}
    return {"content": {"odd_topic": odd, "topics": list(topics), "moments": list(moments), "next_hooks": []},
            "people": {"profiles": list(profiles)}}


def test_distinct_rejects_a_story_told_twice_and_names_both_blocks():
    card = _card(topics=[_entry("r1", "r2", "r3")], moments=[_entry("r1", "r2", "r9", stages=[])],
                 profiles=[_entry("r7", "r8")])
    with pytest.raises(ValueError, match=r"moments\[0\] tells the same story as topics\[0\]"):
        pipeline.check_distinct(card)
    card["content"]["moments"] = [_entry("r5", "r6")]
    pipeline.check_distinct(card)                                 # different lines: fine
    card["people"]["profiles"] = [_entry("r1", "r4")]            # a person retelling half of a topic
    with pytest.raises(ValueError, match=r"profiles\[0\] tells the same story as topics\[0\].*leave this person out"):
        pipeline.check_distinct(card)


def test_distinct_counts_stage_lines_and_ignores_window_summaries():
    moment = _entry("r1", stages=[{"label": "a", "evidence_ids": ["r2"]}, {"label": "b", "evidence_ids": ["r3"]}])
    with pytest.raises(ValueError):
        pipeline.check_distinct(_card(topics=[_entry("r2", "r3")], moments=[moment]))
    pipeline.check_distinct(_card(topics=[_entry("w01")], moments=[_entry("w01")]))


def test_drop_repeats_keeps_odd_then_moments_and_leaves_people():
    card = _card(odd=_entry("r1", status="supported"), topics=[_entry("r1", "r2"), _entry("r4", "r5"), _entry("r6", "r7")],
                 moments=[_entry("r6", "r8")], profiles=[_entry("r1", "r2")])
    pipeline.drop_repeats(card)
    assert [t["evidence_ids"] for t in card["content"]["topics"]] == [["r4", "r5"]]
    assert len(card["content"]["moments"]) == 1 and len(card["people"]["profiles"]) == 1


def test_usage_of_reads_the_cli_envelope_and_tolerates_missing_fields():
    from oopz_capture.analyzer.backend import usage_of

    got = usage_of({"duration_ms": 1200, "duration_api_ms": 1100, "num_turns": 1, "total_cost_usd": 0,
                    "usage": {"input_tokens": 0, "output_tokens": 5, "cache_read_input_tokens": 3,
                              "context_usage_ratio": 0.1}})
    assert got["cli_ms"] == 1200 and got["output_tokens"] == 5 and got["cache_read_tokens"] == 3
    assert got["context_ratio"] == 0.1 and got["credits"] == 0
    assert usage_of({})["context_ratio"] == 0.0


def test_a_very_long_quiet_session_is_cut_by_time():
    runs = [Run(id=f"r{i:04d}", speaker_id="a" * 32, start_ms=i * 1_800_000, end_ms=i * 1_800_000 + 1000,
                text="一句话说得很长" * 30) for i in range(6)]
    assert len(split_windows(runs)) == 2          # 2 hours is the longest a window may span
    assert len(split_windows(runs[:4])) == 1


def test_usage_text_lists_model_requests_and_time():
    from oopz_capture.digest_job import usage_text

    calls = [{"seconds": 97.0, "error": "x", "usage": {"cli_runs": 1}}, {"seconds": 130.0, "error": None, "usage": {"cli_runs": 2}},
             {"seconds": 31.7, "error": None}]
    text = usage_text("Qwen3.8-Flash", calls, 310.4)
    assert text.splitlines() == ["分析用量", "模型：Qwen3.8-Flash", "请求：4 次（其中 1 次因校验未通过而重试）",
                                 "总耗时：5 分 10 秒（模型调用合计 4 分 19 秒）"]
    assert "重试" not in usage_text("m", [{"seconds": 5.0, "error": None}], 8)


def test_budget_grows_with_the_recording_and_reaches_the_prompt():
    from oopz_capture.analyzer import prompts
    from oopz_capture.analyzer.pipeline import budget_for

    hour = 3_600_000
    short, long = budget_for(2 * hour), budget_for(12 * hour)
    assert (short.topics, short.moments, short.hooks) == (3, 1, 1)
    assert long.topics > short.topics and long.chars[1] > short.chars[1] and long.profiles == 7
    assert budget_for(4 * hour).topics == 4 and budget_for(0).topics == 3
    prompt = prompts.system_prompt("final", short)
    assert "最多3项" in prompt and "400到700" in prompt and "写空数组" in prompt and "@" not in prompt
    assert "summary" not in prompts.system_prompt("final", short).split("【输出结构示例")[1].split("odd_topic 没有候选")[0]
    assert '"summary":' in prompts.system_prompt("window")


def test_style_enforces_the_budget_counts():
    from oopz_capture.analyzer.pipeline import Budget, check_style

    tiny = Budget(1, 0, 0, 1, (1, 2))
    card = _card(topics=[_entry("r1")], moments=[_entry("r2", stages=[])])
    with pytest.raises(ValueError, match="moments has 1 entries but at most 0"):
        check_style(card, tiny)
    check_style(card)                                              # the largest budget accepts it


def test_repair_fixes_only_mechanical_gaps():
    from oopz_capture.analyzer.pipeline import repair

    bundle = {"people": [], "evidence": [
        {"id": "r1", "kind": "asr_excerpt", "speaker_id": "u1", "text": "甲说的话"},
        {"id": "r2", "kind": "asr_excerpt", "speaker_id": "u2", "text": "乙说的话"},
        {"id": "r3", "kind": "asr_excerpt", "speaker_id": "u1", "text": "甲又说了"}]}
    card = _card(odd={"title": "t", "text": "x", "evidence_ids": ["r1", "r2", "r1", "r99"], "anchor": "甲说的话"},
                 moments=[_entry("r3", "r99", stages=[{"label": "步", "evidence_ids": ["rX", "r1"], "anchor": "甲说"}])])
    repair(card, bundle)
    odd = card["content"]["odd_topic"]
    assert odd["status"] == "supported" and odd["participant_ids"] == ["u1", "u2"]
    assert odd["evidence_ids"] == ["r1", "r2", "r1"]               # the unknown id went, duplicates are normalize's job
    moment = card["content"]["moments"][0]
    assert moment["evidence_ids"] == ["r3"] and moment["stages"][0]["icon_category"] == "other"
    assert moment["stages"][0]["evidence_ids"] == ["r1"]
    lone = _card(topics=[_entry("r99")])
    repair(lone, bundle)
    assert lone["content"]["topics"][0]["evidence_ids"] == ["r99"]  # nothing known is left: the validator will say so


def test_json_reply_may_carry_text_after_the_object():
    assert parse_json_object('说明\n{"a": {"b": 1}}\n{"c": 2} 多余') == {"a": {"b": 1}}


def test_a_field_written_after_an_early_closed_root_is_still_read():
    reply = '{"content":{"a":1},"people":{"profiles":[]}},"labels":{"odd":"x","topics":["y"]}}'
    assert parse_json_object(reply) == {"content": {"a": 1}, "people": {"profiles": []},
                                        "labels": {"odd": "x", "topics": ["y"]}}
    assert parse_json_object('{"a":1},oops') == {"a": 1}


def test_a_heading_must_use_words_from_its_story():
    from oopz_capture.analyzer.pipeline import check_title_words

    pool = {"r1": {"text": "现在开始步入洋葱时代"}, "r2": {"text": "龙追了我一路"}}
    draft = _card(topics=[_entry("r1", title="种菜宣布", text="有人宣布种菜是新时代")])
    good = _card(topics=[_entry("r1", title="洋葱时代")])
    check_title_words(draft, good, pool)
    bad = _card(topics=[_entry("r1", title="蔬菜封年")])
    with pytest.raises(ValueError, match="uses no word from what was said"):
        check_title_words(draft, bad, pool)
    check_title_words(draft, _card(topics=[_entry("r2", title="x")]), pool)     # a one-character title is not judged
