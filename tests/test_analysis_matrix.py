from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from oopz_capture.analysis_matrix import (
    MatrixRouteClient,
    SharedStageCache,
    build_route_plans,
    run_analysis_matrix,
)
from oopz_capture.analysis_routes import StageRoutedClient
from oopz_capture.output import write_json, write_jsonl


WINDOW_REQUIRED = {
    "summary": str,
    "decisions": list,
    "action_items": list,
    "open_questions": list,
    "uncertainties": list,
}
FINAL_REQUIRED = {
    "title": str,
    "overall_summary": str,
    "chronological_summary": str,
    "key_topics": list,
    "decisions": list,
    "action_items": list,
    "open_questions": list,
    "important_moments": list,
    "uncertainties": list,
}


class FakeClient:
    def __init__(self, model: str, provider: str):
        self.config = SimpleNamespace(
            model=model,
            base_url="https://api.test" if provider == "deepseek" else "http://127.0.0.1:11434",
            thinking_timeout_seconds=180,
            thinking_short_max_tokens=1024,
            thinking_long_max_tokens=2048,
            thinking_final_max_tokens=4096,
        )
        self.provider = provider
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if "overall_summary" in kwargs["required_keys"]:
            content = {
                "title": "测试报告",
                "overall_summary": "测试用户完成了整场任务。",
                "chronological_summary": "测试用户先完成准备，之后持续推进任务并确认结果。",
                "key_topics": [],
                "decisions": [],
                "action_items": [],
                "open_questions": [],
                "important_moments": [],
                "uncertainties": [],
            }
        else:
            content = {
                "summary": "测试用户处理任务并确认结果。",
                "decisions": [],
                "action_items": [],
                "open_questions": [],
                "uncertainties": [],
            }
        return {
            "content": content,
            "metadata": {
                "provider": "deepseek" if self.provider == "deepseek" else "ollama-local",
                "api_called": self.provider == "deepseek",
                "model_requested": self.config.model,
                "model_returned": self.config.model,
                "thinking": kwargs["thinking"],
                "reasoning_effort": kwargs.get("reasoning_effort"),
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        }


def _call(client, required, max_tokens, user_prompt):
    return client.complete_json(
        system_prompt="只输出JSON。",
        user_prompt=user_prompt,
        required_keys=required,
        thinking="enabled" if required is FINAL_REQUIRED else "disabled",
        reasoning_effort="high" if required is FINAL_REQUIRED else None,
        max_tokens=max_tokens,
    )


def _session(tmp_path: Path) -> Path:
    session_id = str(uuid4())
    request_id = str(uuid4())
    session = tmp_path / session_id
    handoff = session / "handoff" / "analyzer_request.json"
    now = datetime(2026, 8, 13, 5, 10, 3, tzinfo=timezone.utc)
    write_json(session / "session.json", {
        "session_id": session_id,
        "started_at": now.isoformat(),
        "capture_clock_started_at": now.isoformat(),
        "duration_seconds": 600,
    })
    write_json(session / "users.json", [{
        "nickname": "测试用户", "oopz_uid": "oopz-user", "agora_uid": 123,
    }])
    transcript = [
        {
            "segment_id": str(uuid4()), "session_id": session_id,
            "start_ms": 1_000, "end_ms": 2_000, "agora_uid": 123,
            "oopz_uid": "oopz-user", "speaker": "测试用户", "text": "第一段测试内容",
        },
        {
            "segment_id": str(uuid4()), "session_id": session_id,
            "start_ms": 301_000, "end_ms": 302_000, "agora_uid": 123,
            "oopz_uid": "oopz-user", "speaker": "测试用户", "text": "第二段测试内容",
        },
    ]
    write_jsonl(session / "transcript.jsonl", transcript)
    (session / "transcript.md").write_text(f"Session ID: {session_id}\n", encoding="utf-8")
    write_json(session / "transcript_summary.json", {"segments": len(transcript)})
    write_json(session / "request.json", {"requested_by": {"chat_type": "group", "chat_id": "123456"}})
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
    return handoff


def test_three_routes_share_only_identical_stage_inputs(tmp_path) -> None:
    deepseek = FakeClient("deepseek-v4-flash", "deepseek")
    qwen = FakeClient("qwen3:8b", "qwen")
    adapter = StageRoutedClient(qwen, qwen, local_thinking=True, local_final_thinking=True)
    cache = SharedStageCache(tmp_path / "shared")
    plans = build_route_plans(adapter)
    clients = [MatrixRouteClient(plan, deepseek, adapter, cache) for plan in plans]
    assert len(clients) == 3

    short_evidence = 'JSON证据：\n{"minutes":[["2026-08-13 22:15",[["测试用户","处理任务"]]]]}'
    _call(clients[0], WINDOW_REQUIRED, 1024, short_evidence)
    for client in clients[1:]:
        _call(client, WINDOW_REQUIRED, 1024, short_evidence)

    _call(clients[0], WINDOW_REQUIRED, 2048, 'JSON证据：\n{"participants":["测试用户"],"windows":[[1,"DS长摘要",[],[],[],[]]]}')
    long_qwen = 'JSON证据：\n{"participants":["测试用户"],"windows":[[1,"Qwen长摘要",[],[],[],[]]]}'
    _call(clients[1], WINDOW_REQUIRED, 2048, long_qwen)
    _call(clients[2], WINDOW_REQUIRED, 2048, long_qwen)

    _call(clients[0], FINAL_REQUIRED, 4096, 'JSON证据：\n{"hours":[[1,"DS最终A",[],[],[],[]]]}')
    _call(clients[1], FINAL_REQUIRED, 4096, 'JSON证据：\n{"hours":[[2,"DS最终B",[],[],[],[]]]}')
    _call(clients[2], FINAL_REQUIRED, 4096, 'JSON证据：\n{"hours":[[3,"Qwen最终",[],[],[],[]]]}')

    assert len(deepseek.calls) == 3
    assert len(qwen.calls) == 3
    assert len(cache.events) == 9
    assert sum(1 for item in cache.events if item["cache_hit"]) == 3
    assert sum(1 for item in cache.events if not item["cache_hit"]) == 6
    assert all(call["thinking"] == "enabled" for call in deepseek.calls)
    assert all(call["reasoning_effort"] == "high" for call in deepseek.calls)
    assert all(call["thinking"] == "enabled" for call in qwen.calls)
    assert "不要固定以‘首先、先、随后、之后、最后’开头" in qwen.calls[0]["system_prompt"]


def test_route_profiles_match_requested_matrix(tmp_path) -> None:
    deepseek = FakeClient("deepseek-v4-flash", "deepseek")
    qwen = FakeClient("qwen3:8b", "qwen")
    adapter = StageRoutedClient(qwen, qwen, local_thinking=True, local_final_thinking=True)
    cache = SharedStageCache(tmp_path / "shared")
    plans = build_route_plans(adapter)

    expected = [
        ("qwen", "deepseek", "deepseek"),
        ("qwen", "qwen", "deepseek"),
        ("qwen", "qwen", "qwen"),
    ]

    assert [(item.short.provider, item.long.provider, item.final.provider) for item in plans] == expected
    for plan in plans:
        profile = MatrixRouteClient(plan, deepseek, adapter, cache).analysis_profile()
        assert profile["matrix_route_id"] == plan.route_id
        assert profile["shared_cache_schema"] == "oopz.analysis.shared-stage-cache.v1"


def test_matrix_integration_writes_three_reports_and_unique_usage(tmp_path) -> None:
    handoff = _session(tmp_path)
    deepseek = FakeClient("deepseek-v4-flash", "deepseek")
    qwen = FakeClient("qwen3:8b", "qwen")

    output = run_analysis_matrix(handoff, deepseek, qwen)
    manifest = output["manifest"]

    assert len(manifest["routes"]) == 3
    assert all(Path(item["report_path"]).is_file() for item in manifest["routes"])
    assert output["manifest_path"].is_file()
    assert output["review_path"].is_file()
    assert manifest["shared_resources"]["logical_route_calls"] == 12
    # Both fake models deliberately return identical text. Exact-input deduplication therefore also
    # shares the downstream DeepSeek long/final requests even though their upstream model names differ.
    assert manifest["shared_resources"]["unique_stage_results"] == 6
    assert manifest["shared_resources"]["avoided_duplicate_calls"] == 6
    assert manifest["shared_resources"]["physical_calls_this_run"] == 6
    assert len(deepseek.calls) == 2
    assert len(qwen.calls) == 4
    assert all("final_summary" in item for item in manifest["routes"])
    review = output["review_path"].read_text(encoding="utf-8")
    assert "## 最终报告结构统计" in review
    assert "## 最终报告并排阅读" in review
    assert "#### 明确决定" in review
    persisted = json.loads(output["manifest_path"].read_text(encoding="utf-8"))
    assert persisted["session_id"] == manifest["session_id"]
