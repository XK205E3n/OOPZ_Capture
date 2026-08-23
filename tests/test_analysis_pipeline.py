from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from oopz_capture.analysis_pipeline import (
    SHORT_REQUIRED,
    _beijing_time_range,
    _normalized_content,
    _render_compact_field,
    run_analysis,
)
from oopz_capture.output import write_json, write_jsonl


class RecordingClient:
    def __init__(self, *, fail_on: int | None = None, request_times: list[str] | None = None):
        self.config = SimpleNamespace(model="deepseek-v4-flash", base_url="https://api.example.test")
        self.fail_on = fail_on
        self.request_times = request_times or ["2026-08-13T00:00:00+00:00"] * 10
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_on == len(self.calls):
            raise RuntimeError("planned failure")
        if "summaries" in kwargs["required_keys"]:
            evidence = json.loads(kwargs["user_prompt"].split("JSON证据：\n", 1)[1])
            content = {"summaries": [{
                "window_id": window["window_id"],
                "summary": "测试-summary",
                "decisions": ["测试-decisions"],
                "action_items": ["测试-action_items"],
                "open_questions": ["测试-open_questions"],
                "uncertainties": ["测试-uncertainties"],
            } for window in evidence["windows"]]}
        else:
            content = {}
            for key, expected in kwargs["required_keys"].items():
                content[key] = f"测试-{key}" if expected is str else [f"测试-{key}"]
        return {
            "content": content,
            "metadata": {
                "provider": "test",
                "model_requested": "deepseek-v4-flash",
                "model_returned": "deepseek-v4-flash",
                "requested_at": self.request_times[len(self.calls) - 1],
                "thinking": kwargs["thinking"],
                "reasoning_effort": kwargs["reasoning_effort"],
                "usage": {
                    "prompt_tokens": 10,
                    "prompt_cache_hit_tokens": 3,
                    "prompt_cache_miss_tokens": 7,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "completion_tokens_details": {"reasoning_tokens": 2 if kwargs["thinking"] == "enabled" else 0},
                },
            },
        }


class ConcurrentOpenCodeClient(RecordingClient):
    def __init__(self) -> None:
        super().__init__()
        self.config = SimpleNamespace(
            model="mimo-v2.5", base_url="https://opencode.ai/zen/go/v1", provider="opencode-go",
        )
        self._lock = threading.Lock()
        self.active_calls = 0
        self.peak_active_calls = 0

    def complete_json(self, **kwargs):
        with self._lock:
            self.active_calls += 1
            self.peak_active_calls = max(self.peak_active_calls, self.active_calls)
        try:
            time.sleep(0.03)
            return super().complete_json(**kwargs)
        finally:
            with self._lock:
                self.active_calls -= 1


def test_beijing_time_range_shows_both_dates_across_midnight() -> None:
    value = SimpleNamespace(session={"capture_clock_started_at": "2026-08-13T15:59:00+00:00"})
    assert _beijing_time_range(value, 0, 300_000) == (
        "2026-08-13 23:59:00–2026-08-14 00:04:00"
    )


def test_final_key_information_fields_are_not_truncated() -> None:
    lines: list[str] = []
    values = [f"信息{i}" for i in range(12)]

    _render_compact_field(lines, "主要话题", values)

    assert lines == ["### 主要话题", *(f"- 信息{i}" for i in range(12))]


def test_pipeline_reports_foreground_progress(tmp_path: Path) -> None:
    events: list[dict] = []
    output = run_analysis(make_session(tmp_path), RecordingClient(), progress_reporter=events.append)

    assert output["result"]["status"] == "completed"
    assert events[0]["stage"] == "started"
    assert events[0]["short_total"] == 2
    assert [event["stage"] for event in events].count("short") == 2
    assert [event["stage"] for event in events].count("long") == 1
    assert events[-1]["stage"] == "completed"


def test_pdf_failure_does_not_discard_completed_markdown_analysis(tmp_path: Path, monkeypatch) -> None:
    def fail_pdf(*args, **kwargs):
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr("oopz_capture.analysis_pipeline.render_session_reports", fail_pdf)
    output = run_analysis(make_session(tmp_path), RecordingClient(), render_pdf=True)

    assert output["result"]["status"] == "completed"
    assert output["report_path"].is_file()
    assert output["pdf_path"] is None
    assert output["pdf_error"]["type"] == "RuntimeError"
    assert any("PDF rendering failed" in item["message"] for item in output["result"]["errors"])


def test_cloud_window_prompts_require_chronological_nickname_movements(tmp_path: Path) -> None:
    client = RecordingClient()
    run_analysis(make_session(tmp_path), client)
    short_prompt = client.calls[0]["system_prompt"]
    long_prompt = client.calls[2]["system_prompt"]
    assert "严格按照minutes及每分钟内部对话的原有先后顺序" in short_prompt
    assert "不要把不同时间发生的内容按话题重新归类" in short_prompt
    assert "不要固定重复‘首先、随后、之后、最后’" in short_prompt
    assert "严格按照windows的起始顺序叙述各阶段进展" in long_prompt
    assert "不得按游戏、生活、技术等主题重新分组" in long_prompt
    assert "多个连续句群" in long_prompt
    assert "summary目标不超过500个汉字" in short_prompt
    assert "summary目标不超过1000个汉字" in long_prompt


def test_opencode_short_windows_use_one_api_request_each_with_parallel_execution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OOPZ_ANALYSIS_MAX_PARALLELISM", "4")
    client = ConcurrentOpenCodeClient()
    output = run_analysis(make_session(tmp_path, short_window_count=5), client)

    assert output["result"]["analysis_profile"]["window_parallelism"] == 4
    assert output["result"]["analysis_profile"]["short_request_mode"] == "one_request_per_window"
    short_calls = [
        call for call in client.calls
        if call["required_keys"] == SHORT_REQUIRED and call["max_tokens"] == 1024
    ]
    assert len(short_calls) == 5
    assert all("summaries" not in call["required_keys"] for call in client.calls)
    assert client.peak_active_calls == 4
    assert len(output["result"]["short_summaries"]) == 5
    assert output["result"]["model"]["usage_by_stage"]["short_summaries"]["api_calls"] == 5


def test_pipeline_normalizes_object_items_in_text_arrays() -> None:
    content = _normalized_content({
        "summary": "测试摘要",
        "decisions": [{"decision": "继续录音", "confidence": 0.8}],
        "action_items": [{"action": "稍后分析"}],
        "open_questions": [{"question": "是否转发报告"}],
        "uncertainties": [{"text": "术语含义未确认", "segment_id": "ignored"}],
    }, {
        "summary": str,
        "decisions": list,
        "action_items": list,
        "open_questions": list,
        "uncertainties": list,
    })

    assert content["decisions"] == ["继续录音"]
    assert content["action_items"] == ["稍后分析"]
    assert content["open_questions"] == ["是否转发报告"]
    assert content["uncertainties"] == ["术语含义未确认"]


def make_session(
    tmp_path: Path,
    *,
    silent: bool = False,
    delivery_target: bool = True,
    short_window_count: int = 2,
) -> Path:
    session_id = str(uuid4())
    request_id = str(uuid4())
    session = tmp_path / session_id
    handoff = session / "handoff" / "analyzer_request.json"
    now = datetime(2026, 8, 13, 5, 10, 3, tzinfo=timezone.utc)
    write_json(session / "session.json", {
        "session_id": session_id,
        "started_at": now.isoformat(),
        "capture_clock_started_at": now.isoformat(),
        "duration_seconds": 300 * short_window_count,
    })
    users = [{"nickname": "测试用户", "oopz_uid": "oopz-user", "agora_uid": 123}]
    write_json(session / "users.json", users)
    labels = ["第一", "第二", "第三", "第四", "第五"]
    transcript = [] if silent else [
        {
            "segment_id": str(uuid4()), "session_id": session_id,
            "start_ms": index * 300_000 + 1_000, "end_ms": index * 300_000 + 2_000, "agora_uid": 123,
            "oopz_uid": "oopz-user", "speaker": "测试用户",
            "text": f"{labels[index] if index < len(labels) else index + 1}段测试内容",
        }
        for index in range(short_window_count)
    ]
    write_jsonl(session / "transcript.jsonl", transcript)
    (session / "transcript.md").write_text(f"Session ID: {session_id}\n", encoding="utf-8")
    write_json(session / "transcript_summary.json", {"segments": len(transcript)})
    write_json(handoff, {
        "schema_version": "oopz.analyzer.request.v1",
        "request_id": request_id,
        "session_id": session_id,
        "created_at": now.isoformat(),
        "analysis_deadline_at": (now + timedelta(minutes=15)).isoformat(),
        "encoding": "UTF-8",
        "delivery_mode": "final_only",
        "summary_windows": {"short_summary_seconds": 300, "long_summary_seconds": 3600},
        "inputs": {
            "transcript_jsonl": "transcript.jsonl", "transcript_markdown": "transcript.md",
            "transcript_summary": "transcript_summary.json", "users": "users.json",
            "session": "session.json", "segment_count": len(transcript),
        },
        "required_outputs": {},
        "retention": {"delete_after": (now + timedelta(hours=168)).isoformat(), "maximum_hours": 168},
    })
    if delivery_target:
        write_json(session / "request.json", {
            "requested_by": {"chat_type": "group", "chat_id": "123456"},
        })
    return handoff


def test_pipeline_uses_non_thinking_short_and_thinking_long_and_final(tmp_path: Path) -> None:
    handoff = make_session(tmp_path)
    client = RecordingClient()
    output = run_analysis(handoff, client)

    assert [(item["thinking"], item["reasoning_effort"]) for item in client.calls] == [
        ("disabled", None), ("disabled", None), ("disabled", None), ("enabled", "high"),
    ]
    assert client.calls[-1]["max_tokens"] == 4096
    assert [item["max_tokens"] for item in client.calls[:-1]] == [1024, 1024, 2048]
    assert all("oopz-user" not in item["user_prompt"] for item in client.calls)
    assert all("agora_uid" not in item["user_prompt"] for item in client.calls)
    assert "segment_id" not in client.calls[0]["user_prompt"]
    assert "start_ms" not in client.calls[0]["user_prompt"]
    assert "end_ms" not in client.calls[0]["user_prompt"]
    assert '第一段测试内容' in client.calls[0]["user_prompt"]
    assert "2026-08-13 13:10" in client.calls[0]["user_prompt"]
    assert "北京时间 UTC+8" not in client.calls[2]["user_prompt"]
    assert "朋友之间的现实日常生活交流和多人游戏游玩交流" in client.calls[0]["system_prompt"]
    assert '"participants":["测试用户"]' in client.calls[2]["user_prompt"]
    assert "source_segment_ids" not in client.calls[2]["user_prompt"]
    assert "analysis_fingerprint" not in client.calls[2]["user_prompt"]
    assert "model_returned" not in client.calls[-1]["user_prompt"]
    result = output["result"]
    assert result["status"] == "completed"
    assert len(result["short_summaries"]) == 2
    assert len(result["long_summaries"]) == 1
    assert result["model"]["usage"]["mode_calls"] == {"disabled": 3, "enabled": 1, "deterministic": 0}
    assert result["analysis_policy"] == {
        "short_summaries": {
            "thinking": "disabled", "reasoning_effort": None, "initial_max_tokens": 1024,
        },
        "long_summaries": {
            "thinking": "disabled", "reasoning_effort": None, "initial_max_tokens": 2048,
        },
        "final_overview": {
            "thinking": "enabled", "reasoning_effort": "high",
                "reasoning_effort_note": "lowest level supported by the OpenAI-compatible analysis adapter",
            "initial_max_tokens": 4096,
        },
    }
    usage = result["model"]["usage_by_stage"]
    assert usage["short_summaries"] == {
        "prompt_tokens": 20, "prompt_cache_hit_tokens": 6, "prompt_cache_miss_tokens": 14,
        "completion_tokens": 8, "reasoning_tokens": 0,
        "total_tokens": 28, "api_calls": 2,
        "mode_calls": {"disabled": 2, "enabled": 0, "deterministic": 0},
    }
    assert usage["long_summaries"]["total_tokens"] == 14
    assert usage["long_summaries"]["reasoning_tokens"] == 0
    assert usage["final_overview"]["total_tokens"] == 14
    assert usage["final_overview"]["reasoning_tokens"] == 2
    assert usage["total"] == result["model"]["usage"]
    costs = result["model"]["cost_estimate"]
    assert costs["status"] == "estimated"
    assert costs["pricing_effective_at_beijing"] == "2026-08-17T00:00:00+08:00"
    assert costs["contains_pre_effective_requests"] is True
    assert costs["stages"]["short_summaries"]["estimated_cost_rmb"] == 0.0000573
    assert costs["total_estimated_cost_rmb"] == 0.0001146
    assert costs["stages"]["total"]["pricing_periods"]["off_peak"]["billing_records"] == 4
    assert costs["stages"]["total"]["pricing_periods"]["peak"]["billing_records"] == 0
    assert all(item["topics"] == [] for item in result["short_summaries"])
    assert result["long_summaries"][0]["key_topics"] == []
    assert result["long_summaries"][0]["progress"] == []
    report = output["report_path"].read_text(encoding="utf-8")
    assert "Report ID:" in report and "Session ID:" in report
    assert "用户：测试用户" in report and "Window ID:" not in report
    assert "OOPZ UID=" not in report and "Agora UID=" not in report
    assert "## 关键信息" in report and "## 关键信息（精简）" not in report and "\ufffd" not in report
    assert "### 不确定内容" in report
    assert "测试-uncertainties" in report
    assert "# 2026-08-13 13:10:03至2026-08-13 13:20:03OOPZ频道聊天整理与总结" in report
    assert "### 2026-08-13 13:10:03–13:15:03" in report
    assert "## Token 使用与费用估算" in report
    assert "计划于北京时间 2026-08-17 00:00 生效的新峰谷价格" in report
    assert "不代表当前实际账单" in report
    assert "时段依据是每次 API 请求发生的北京时间，不是录音时间" in report
    assert "非高峰" in report and "峰谷计价明细" not in report
    assert "| 300秒总结 | 2 | 20 | 6 | 14 | 0 | 8 | 0 | 28 |" in report
    assert "推理 Token 已包含在输出 Token 中，不重复计费" in report
    assert "官方价格文档：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/" in report
    assert report.rstrip().endswith("https://api-docs.deepseek.com/zh-cn/quick_start/pricing/")
    assert result["report_format_version"] == "3.7.0"
    public_report = output["report_path"].with_name("summary.public.md").read_text(encoding="utf-8")
    assert "### 整体性总结" in public_report
    assert "### 按时间顺序的进展" in public_report
    assert "## 关键信息" in public_report
    assert all(title in public_report for title in (
        "### 主要话题", "### 明确决定", "### 行动项", "### 未解决问题", "### 重要时间点",
    ))
    assert "### 不确定内容" not in public_report
    assert "测试-uncertainties" not in public_report
    assert "## 每300秒短期总结" not in public_report
    assert "## Token 使用与费用估算" not in public_report
    text_report = output["report_path"].with_name("summary.text.md").read_text(encoding="utf-8")
    assert "## 整体性总结" in text_report and "## 按时间顺序的进展" in text_report
    messages = [json.loads(line) for line in output["report_messages_path"].read_text(encoding="utf-8").splitlines()]
    assert all(item["target"] == {"type": "group", "id": "123456"} for item in messages)
    assert all(item["delivery_status"] == "pending" for item in messages)
    assert all(len(item["text"]) <= 3300 for item in messages)


def test_pipeline_is_idempotent_for_same_model_profile(tmp_path: Path) -> None:
    handoff = make_session(tmp_path)
    first = RecordingClient()
    run_analysis(handoff, first)
    second = RecordingClient()
    output = run_analysis(handoff, second)
    assert output["reused"] is True
    assert output["rerendered"] is False
    assert second.calls == []


def test_existing_analysis_is_rerendered_without_model_calls(tmp_path: Path) -> None:
    handoff = make_session(tmp_path)
    first = RecordingClient()
    output = run_analysis(handoff, first)
    result_path = output["result_path"]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("report_format_version")
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    output["report_path"].write_text("旧版报告\n", encoding="utf-8")

    second = RecordingClient()
    refreshed = run_analysis(handoff, second)

    assert refreshed["reused"] is True
    assert refreshed["rerendered"] is True
    assert second.calls == []
    report = refreshed["report_path"].read_text(encoding="utf-8")
    assert "用户：测试用户" in report
    assert "OOPZ UID=" not in report and "Agora UID=" not in report
    assert "### 2026-08-13 13:10:03–13:15:03" in report
    assert "## Token 使用与费用估算" in report
    assert refreshed["result"]["model"]["cost_estimate"]["status"] == "estimated"
    assert refreshed["result"]["report_format_version"] == "3.7.0"
    public_report = output["report_path"].with_name("summary.public.md").read_text(encoding="utf-8")
    assert "### 不确定内容" not in public_report
    assert "测试-uncertainties" not in public_report


def test_cost_estimate_splits_api_requests_across_peak_and_off_peak(tmp_path: Path) -> None:
    handoff = make_session(tmp_path)
    client = RecordingClient(request_times=[
        "2026-08-18T00:30:00+00:00",  # 08:30 Beijing, off-peak
        "2026-08-18T01:30:00+00:00",  # 09:30 Beijing, peak
        "2026-08-18T04:30:00+00:00",  # 12:30 Beijing, off-peak
        "2026-08-18T06:30:00+00:00",  # 14:30 Beijing, peak
    ])
    output = run_analysis(handoff, client)
    costs = output["result"]["model"]["cost_estimate"]

    assert costs["contains_pre_effective_requests"] is False
    assert costs["stages"]["short_summaries"]["pricing_periods"]["off_peak"]["billing_records"] == 1
    assert costs["stages"]["short_summaries"]["pricing_periods"]["peak"]["billing_records"] == 1
    assert costs["stages"]["long_summaries"]["pricing_periods"]["off_peak"]["billing_records"] == 1
    assert costs["stages"]["final_overview"]["pricing_periods"]["peak"]["billing_records"] == 1
    assert costs["total_estimated_cost_rmb"] == 0.0001719
    report = output["report_path"].read_text(encoding="utf-8")
    assert "300秒总结：非高峰 1次" in report
    assert "300秒总结：" in report and "高峰 1次" in report
    assert "峰谷计价明细" not in report
    assert "60分钟摘要：非高峰 1次" in report


def test_pipeline_resumes_after_window_failure(tmp_path: Path) -> None:
    handoff = make_session(tmp_path)
    client = RecordingClient(fail_on=2)
    with pytest.raises(RuntimeError, match="planned failure"):
        run_analysis(handoff, client)
    client.calls.clear()
    client.fail_on = None
    output = run_analysis(handoff, client)
    assert output["result"]["status"] == "completed"
    assert [item["thinking"] for item in client.calls] == ["disabled", "disabled", "enabled"]


def test_silent_session_uses_no_api_and_requires_delivery_target(tmp_path: Path) -> None:
    handoff = make_session(tmp_path, silent=True, delivery_target=False)
    client = RecordingClient()
    output = run_analysis(handoff, client)
    assert client.calls == []
    assert output["result"]["model"]["usage"]["api_calls"] == 0
    messages = [json.loads(line) for line in output["report_messages_path"].read_text(encoding="utf-8").splitlines()]
    assert messages[0]["target"] == {"type": "unconfigured", "id": ""}
    assert messages[0]["delivery_status"] == "target_required"


def test_mock_profile_cannot_poison_real_profile_cache(tmp_path: Path) -> None:
    handoff = make_session(tmp_path)
    first = RecordingClient()
    first.config = SimpleNamespace(model="mock-model", base_url="offline")
    mock_output = run_analysis(handoff, first)
    second = RecordingClient()
    real_output = run_analysis(handoff, second)
    assert second.calls
    assert mock_output["result"]["analysis_fingerprint"] != real_output["result"]["analysis_fingerprint"]
    assert mock_output["result"]["report_id"] != real_output["result"]["report_id"]
