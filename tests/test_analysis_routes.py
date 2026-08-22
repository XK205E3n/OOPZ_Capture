from __future__ import annotations

from types import SimpleNamespace

from oopz_capture.analysis_routes import StageRoutedClient


class Client:
    def __init__(self, model: str):
        self.config = SimpleNamespace(model=model, base_url="test")
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": {"summary": "测试用户开始任务，完成处理并确认了结果。"}, "metadata": {}}


def test_stage_route_keeps_final_on_deepseek() -> None:
    local = Client("qwen3:8b")
    final = Client("deepseek-v4-flash")
    route = StageRoutedClient(local, final)

    route.complete_json(thinking="disabled", system_prompt="short", max_tokens=1024)
    route.complete_json(thinking="enabled", system_prompt="final", max_tokens=4096)

    assert len(local.calls) == 1
    assert len(final.calls) == 1
    assert "按照minutes及其内部对话" in local.calls[0]["system_prompt"]
    assert "禁止输出关于ASR" in local.calls[0]["system_prompt"]
    assert "不得在summary中复述日期" in local.calls[0]["system_prompt"]
    assert "不要固定以‘首先、先、随后、之后、最后’开头" in local.calls[0]["system_prompt"]
    assert final.calls[0]["system_prompt"] == "final"
    assert route.analysis_profile()["local_window_prompt_contract"] == "chronological-natural-chinese-nickname-gated-v9"
    assert route.analysis_profile()["stages"]["final_overview"]["model"] == "deepseek-v4-flash"


def test_stage_route_can_enable_bounded_local_thinking_without_changing_final() -> None:
    local = Client("qwen3:8b")
    local.config.thinking_timeout_seconds = 30
    final = Client("deepseek-v4-flash")
    route = StageRoutedClient(local, final, local_thinking=True)

    route.complete_json(thinking="disabled", reasoning_effort=None, max_tokens=1024, system_prompt="short")
    route.complete_json(thinking="enabled", reasoning_effort="high", max_tokens=4096)

    assert local.calls[0]["thinking"] == "enabled"
    assert local.calls[0]["reasoning_effort"] == "low"
    assert local.calls[0]["max_tokens"] == 1024
    assert "压缩为4至8个" in local.calls[0]["system_prompt"]
    assert final.calls[0]["reasoning_effort"] == "high"
    assert route.stage_policy()["short_summaries"]["request_timeout_seconds"] == 30


def test_stage_route_can_generate_all_stages_with_local_qwen_thinking() -> None:
    local = Client("qwen3:8b")
    local.config.thinking_timeout_seconds = 180
    local.config.thinking_final_max_tokens = 8192
    final = Client("deepseek-v4-flash")
    route = StageRoutedClient(
        local, final, local_thinking=True, local_final_thinking=True
    )

    route.complete_json(thinking="disabled", max_tokens=1024, system_prompt="short")
    route.complete_json(thinking="enabled", reasoning_effort="high", max_tokens=4096, system_prompt="final")

    assert len(local.calls) == 2
    assert len(final.calls) == 0
    assert local.calls[1]["thinking"] == "enabled"
    assert local.calls[1]["reasoning_effort"] == "low"
    assert local.calls[1]["max_tokens"] == 8192
    assert "final" in local.calls[1]["system_prompt"]
    assert "本地最终总结额外约束" in local.calls[1]["system_prompt"]
    assert "约400至600字的整体性总结" in local.calls[1]["system_prompt"]
    assert "按照hours的先后" in local.calls[1]["system_prompt"]
    assert route.analysis_profile()["stages"]["final_overview"]["model"] == "qwen3:8b"
    assert route.analysis_profile()["base_url"] == "local-only"
    assert route.stage_policy()["final_overview"]["initial_max_tokens"] == 8192
    assert "Qwen3 local thinking" in route.stage_policy()["final_overview"]["reasoning_effort_note"]


def test_deepseek_r1_distill_gets_more_thinking_room_but_same_wall_timeout() -> None:
    local = Client("deepseek-r1:8b")
    local.config.thinking_timeout_seconds = 30
    final = Client("deepseek-v4-flash")
    route = StageRoutedClient(local, final, local_thinking=True)

    route.complete_json(thinking="disabled", reasoning_effort=None, max_tokens=1024, system_prompt="short")
    route.complete_json(thinking="disabled", reasoning_effort=None, max_tokens=2048, system_prompt="long")

    assert [item["max_tokens"] for item in local.calls] == [2048, 4096]
    assert "按照minutes及其内部对话" in local.calls[0]["system_prompt"]
    assert "按照windows的起始顺序" in local.calls[1]["system_prompt"]
    assert "不得按话题重新分组" in local.calls[1]["system_prompt"]
    assert "summary应使用换行或分号分隔的多个连续句群" in local.calls[1]["system_prompt"]
    assert route.stage_policy()["short_summaries"]["initial_max_tokens"] == 2048
    assert route.stage_policy()["long_summaries"]["initial_max_tokens"] == 4096
    assert route.stage_policy()["long_summaries"]["request_timeout_seconds"] == 30


def test_experimental_thinking_budgets_override_defaults() -> None:
    local = Client("qwen3:8b")
    local.config.thinking_timeout_seconds = 120
    local.config.thinking_short_max_tokens = 4096
    local.config.thinking_long_max_tokens = 8192
    route = StageRoutedClient(local, Client("deepseek-v4-flash"), local_thinking=True)

    route.complete_json(thinking="disabled", max_tokens=1024, system_prompt="short")
    route.complete_json(thinking="disabled", max_tokens=2048, system_prompt="long")

    assert [item["max_tokens"] for item in local.calls] == [4096, 8192]
    assert route.stage_policy()["short_summaries"]["request_timeout_seconds"] == 120


def test_local_output_gate_retries_missing_nickname_without_forcing_connectors() -> None:
    class CorrectingClient(Client):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {
                    "content": {"summary": "参与者完成了任务。"},
                    "metadata": {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
                }
            return {
                "content": {"summary": "测试用户开始任务，推进处理并确认完成。"},
                "metadata": {"usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}},
            }

    local = CorrectingClient("qwen3:8b")
    route = StageRoutedClient(local, Client("deepseek-v4-flash"))
    evidence = '{"minutes":[["2026-08-13 13:10",[["测试用户","开始任务"]]]]}'
    result = route.complete_json(
        thinking="disabled", max_tokens=1024, system_prompt="short", user_prompt="JSON证据：\n" + evidence
    )

    assert len(local.calls) == 2
    assert result["content"]["summary"] == "测试用户开始任务，推进处理并确认完成。"
    assert result["metadata"]["usage"]["total_tokens"] == 26
    assert result["metadata"]["semantic_retries"] == 1
    assert "允许使用的nickname仅限：测试用户" in local.calls[1]["system_prompt"]


def test_local_output_gate_deterministically_removes_meta_commentary() -> None:
    class MetaClient(Client):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "content": {
                    "summary": "测试用户开始任务，推进处理并确认完成。存在大量ASR错误。",
                    "uncertainties": ["ASR转写质量较差", "具体物品尚未确认"],
                },
                "metadata": {"usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
            }

    local = MetaClient("qwen3:8b")
    route = StageRoutedClient(local, Client("deepseek-v4-flash"))
    evidence = '{"minutes":[["2026-08-13 13:10",[["测试用户","开始任务"]]]]}'
    result = route.complete_json(
        thinking="disabled", max_tokens=1024, system_prompt="short", user_prompt="JSON证据：\n" + evidence
    )

    assert len(local.calls) == 1
    assert result["content"]["summary"] == "测试用户开始任务，推进处理并确认完成。"
    assert result["content"]["uncertainties"] == ["具体物品尚未确认"]
    assert result["metadata"]["local_meta_comments_removed"] == 2


def test_local_output_gate_retries_an_overlong_long_summary() -> None:
    class LongClient(Client):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            summary = (
                    "测试用户开始任务并" + ("处理任务" * (320 if len(self.calls) == 1 else 50))
                + "，结果已经确认。"
            )
            return {"content": {"summary": summary}, "metadata": {"usage": {"total_tokens": 1}}}

    local = LongClient("qwen3:8b")
    route = StageRoutedClient(local, Client("deepseek-v4-flash"))
    evidence = '{"participants":["测试用户"],"windows":[]}'
    result = route.complete_json(
        thinking="disabled", max_tokens=2048, system_prompt="long", user_prompt="JSON证据：\n" + evidence
    )

    assert len(local.calls) == 2
    assert len(result["content"]["summary"]) <= 600
    assert "超过600个字符" in local.calls[1]["system_prompt"]


def test_local_final_gate_retries_short_overview_and_removes_meta_commentary() -> None:
    class FinalClient(Client):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                summary = "测试用户推进任务。存在误转写。"
            else:
                summary = "测试用户开始任务并完成资源整理，星铸E5随后检查车辆状态并提出修理方案。" * 7
            return {
                "content": {"overall_summary": summary, "chronological_summary": summary},
                "metadata": {"usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
            }

    local = FinalClient("qwen3:8b")
    local.config.thinking_timeout_seconds = 30
    route = StageRoutedClient(local, Client("deepseek-v4-flash"), local_thinking=True, local_final_thinking=True)
    result = route.complete_json(
        thinking="enabled", reasoning_effort="high", max_tokens=4096,
        system_prompt="final", user_prompt='JSON证据：\n{"participants":["测试用户","星铸E5"],"hours":[[1,"A"],[2,"B"],[3,"C"]]}',
    )

    assert len(local.calls) == 2
    assert len(result["content"]["overall_summary"]) >= 200
    assert "误转写" not in result["content"]["overall_summary"]
    assert result["metadata"]["semantic_retries"] == 1


def test_local_chronological_summary_can_keep_optional_time_labels() -> None:
    response = {
        "content": {"chronological_summary": "第2小时，测试用户在22:15继续推进任务。"},
        "metadata": {},
    }

    result = StageRoutedClient._sanitize_local_response(
        response, summary_key="chronological_summary", allow_timeline_labels=True,
    )

    assert result["content"]["chronological_summary"] == "第2小时，测试用户在22:15继续推进任务。"


def test_local_output_gate_retries_explicit_timeline_labels() -> None:
    class TimelineClient(Client):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            summary = (
                "22:15 测试用户开始并完成处理。"
                if len(self.calls) == 1
                else "测试用户开始并处理任务，结果已经确认。"
            )
            return {"content": {"summary": summary}, "metadata": {"usage": {"total_tokens": 1}}}

    local = TimelineClient("deepseek-r1:8b")
    route = StageRoutedClient(local, Client("deepseek-v4-flash"))
    evidence = '{"minutes":[["2026-08-13 22:15",[["测试用户","开始任务"]]]]}'
    result = route.complete_json(
        thinking="disabled", max_tokens=1024, system_prompt="short", user_prompt="JSON证据：\n" + evidence
    )

    assert len(local.calls) == 1
    assert "22:15" not in result["content"]["summary"]
    assert result["metadata"]["local_timeline_labels_removed"] == 1


def test_local_output_gate_retries_mechanical_connector_repetition() -> None:
    class NaturalizingClient(Client):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            summary = (
                "首先，测试用户开始任务。随后，测试用户处理材料。之后，测试用户检查结果。最后，测试用户完成任务。"
                if len(self.calls) == 1
                else "测试用户开始任务，处理并检查材料，确认结果后完成任务。"
            )
            return {"content": {"summary": summary}, "metadata": {"usage": {"total_tokens": 1}}}

    local = NaturalizingClient("qwen3:8b")
    route = StageRoutedClient(local, Client("deepseek-v4-flash"))
    evidence = '{"minutes":[["2026-08-13 22:15",[["测试用户","开始任务"]]]]}'
    result = route.complete_json(
        thinking="disabled", max_tokens=1024, system_prompt="short", user_prompt="JSON证据：\n" + evidence
    )

    assert len(local.calls) == 2
    assert result["content"]["summary"].startswith("测试用户")
    assert "机械重复使用顺序连接词" in local.calls[1]["system_prompt"]
