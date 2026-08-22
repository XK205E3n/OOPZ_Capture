from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any


LOCAL_WINDOW_PROMPT_CONTRACT_VERSION = "chronological-natural-chinese-nickname-gated-v9"
_TIMELINE_LABEL = re.compile(r"(?:20\d{2}-\d{2}-\d{2}|(?:[01]\d|2[0-3]):[0-5]\d)")
_TIMELINE_RANGE = re.compile(
    r"(?:20\d{2}-\d{2}-\d{2}\s*)?"
    r"(?:[01]\d|2[0-3]):[0-5]\d"
    r"(?:\s*[-–—至到]\s*(?:[01]\d|2[0-3]):[0-5]\d)?"
)
_MECHANICAL_SEQUENCE_PREFIX = re.compile(
    r"(?:^|[。！？；;]\s*)(?:首先|先|随后|之后|接着|继而|然后|后来|最后|最终)[，,：:\s]"
)
_LOCAL_META_COMMENTARY = re.compile(
    r"ASR|转写(?:质量|错误|问题)|断句|脏话|口误|术语混淆|表达含混|信息理解|输入质量|文本质量|"
    r"识别错误|语音质量|误转写|术语争议"
)


class StageRoutedClient:
    """Route window and final stages while keeping local experiments isolated from production."""

    def __init__(
        self, local_client: Any, final_client: Any, *, local_thinking: bool = False,
        local_final_thinking: bool = False,
    ):
        self.local_client = local_client
        self.final_client = final_client
        self.local_thinking = local_thinking
        self.local_final_thinking = local_final_thinking
        if local_final_thinking and not local_thinking:
            raise ValueError("local_final_thinking requires local_thinking")

    def _thinking_budgets(self) -> tuple[int, int, int]:
        config = getattr(self.local_client, "config", None)
        model = str(getattr(config, "model", "")).lower()
        defaults = (2048, 4096) if model.startswith("deepseek-r1") else (1024, 2048)
        return (
            int(getattr(config, "thinking_short_max_tokens", 0) or defaults[0]),
            int(getattr(config, "thinking_long_max_tokens", 0) or defaults[1]),
            int(getattr(config, "thinking_final_max_tokens", 0) or 4096),
        )

    @staticmethod
    def _local_window_contract(*, short_stage: bool) -> str:
        if short_stage:
            chronology = (
                "这是300秒短总结。summary必须按照minutes及其内部对话的原有先后顺序叙述。"
                "将连续内容压缩为4至8个关键发展，直接以nickname及其行动、发言或状态变化组织自然、"
                "完整的中文句子；每个值得保留的用户动向都要明确写出对应nickname。"
            )
        else:
            chronology = (
                "这是60分钟长总结。必须按照windows的起始顺序叙述各阶段进展，不得按话题重新分组而"
                "打乱事件顺序；参照‘前段—中段—后段’的阶段推进方式，将整个小时压缩为6至12个按先后"
                "排列的关键发展。每个关键发展都要写出具体nickname及其行动、发言或状态变化，不能只写"
                "‘团队讨论了’‘众人围绕’这类主题概括。不要逐个复述每个300秒窗口，但必须保留窗口之间"
                "的因果、转折、决定和人员进出。summary应使用换行或分号分隔的多个连续句群，而不是一整段"
                "把整小时主题揉在一起；阶段标记可以自然使用，但不要机械重复固定连接词。"
            )
        return (
            "\n本地模型额外输出约束：" + chronology
            + "本地路线覆盖通用的简短长度限制：300秒summary最多600个汉字，60分钟summary最多600个汉字；"
            + "60分钟summary目标为约250至500个汉字。即使该小时证据较少（例如只有部分用户在线、对话稀疏），"
            "也不要过度压缩：应展开每个已确认动向的过程细节、参与者的具体发言或行动、以及事件之间的先后与因果，"
            "宁可把单一动向写得更具体，也不要省略；summary仍须按时间顺序覆盖至少三个阶段或转折，"
            "不能用一两句总括整个小时。"
            "各数组可保留最多8项，每项最多140个汉字。应优先保留具体用户动向、决定和未解决事项，"
            "不要为了缩短而删掉事件之间的因果关系。"
            + "保持顺序不等于逐句添加顺序词。不要固定以‘首先、先、随后、之后、最后’开头，也不要"
            "机械重复这些连接词；只有在缺少连接词会造成理解困难时才自然使用。"
            + "证据中的北京时间只用于判断先后，不得在summary中复述日期、HH:MM具体时刻或时间区间。"
            + "说话人已知时，禁止只用‘多人、众人、参与者、双方、玩家’等泛称代替nickname。"
            "只陈述用户动向，不评论原始文本或说话方式。"
            "禁止输出关于ASR、转写质量、断句、脏话、口误、术语混淆、表达含混、可读性或信息理解难度的"
            "诊断性评论；遇到疑似误识别片段应静默跳过。uncertainties只记录会影响事件事实判断、且对报告"
            "确有价值的具体未确认事实，不能用来评价输入质量。"
        )

    @staticmethod
    def _prompt_evidence(user_prompt: str) -> dict[str, Any]:
        marker = "JSON证据：\n"
        if marker not in user_prompt:
            return {}
        try:
            value = json.loads(user_prompt.split(marker, 1)[1])
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _evidence_participants(cls, user_prompt: str, *, short_stage: bool) -> set[str]:
        evidence = cls._prompt_evidence(user_prompt)
        if short_stage:
            return {
                str(turn[0])
                for minute in evidence.get("minutes", []) if isinstance(minute, list) and len(minute) >= 2
                for turn in minute[1] if isinstance(turn, list) and turn and str(turn[0]).strip()
            }
        return {str(value) for value in evidence.get("participants", []) if str(value).strip()}

    @classmethod
    def _local_output_violations(cls, response: dict[str, Any], user_prompt: str, *, short_stage: bool) -> list[str]:
        content = response.get("content") if isinstance(response, dict) else None
        summary = str(content.get("summary") or "") if isinstance(content, dict) else ""
        participants = cls._evidence_participants(user_prompt, short_stage=short_stage)
        violations: list[str] = []
        if len(_MECHANICAL_SEQUENCE_PREFIX.findall(summary)) >= 4:
            violations.append("summary机械重复使用顺序连接词")
        if _TIMELINE_LABEL.search(summary):
            violations.append("summary包含不应复述的日期或HH:MM时间标签")
        if participants and not any(name in summary for name in participants):
            violations.append("summary缺少实际参与者nickname")
        if _LOCAL_META_COMMENTARY.search(summary):
            violations.append("summary包含被禁止的输入质量元评论")
        limit = 600 if short_stage else 600
        if len(summary) > limit:
            violations.append(f"summary超过{limit}个字符（当前{len(summary)}个）")
        evidence = cls._prompt_evidence(user_prompt)
        if not short_stage and len(evidence.get("windows", [])) >= 3 and len(summary) < 200:
            violations.append(f"summary过短（当前{len(summary)}个），需要保留更多按顺序排列的用户动向")
        return violations

    @classmethod
    def _local_final_output_violations(
        cls, response: dict[str, Any], user_prompt: str = ""
    ) -> list[str]:
        content = response.get("content") if isinstance(response, dict) else None
        if not isinstance(content, dict):
            return ["最终总结不是有效JSON内容"]
        if "overall_summary" not in content or "chronological_summary" not in content:
            return []
        summary = str(content.get("overall_summary") or "")
        chronological = str(content.get("chronological_summary") or "")
        violations: list[str] = []
        evidence = cls._prompt_evidence(user_prompt)
        if len(evidence.get("hours", [])) >= 3 and len(summary) < 250:
            violations.append(f"overall_summary过短（当前{len(summary)}个），需要形成约500字的整体性总结")
        if len(summary) > 700:
            violations.append(f"overall_summary超过700个字符（当前{len(summary)}个）")
        if len(evidence.get("hours", [])) >= 3 and len(chronological) < 250:
            violations.append(f"chronological_summary过短（当前{len(chronological)}个），需要保留按时段的用户动向")
        if len(chronological) > 1100:
            violations.append(f"chronological_summary超过1100个字符（当前{len(chronological)}个）")
        if _LOCAL_META_COMMENTARY.search(summary):
            violations.append("overall_summary包含被禁止的输入质量元评论")
        if _LOCAL_META_COMMENTARY.search(chronological):
            violations.append("chronological_summary包含被禁止的输入质量元评论")
        return violations

    @classmethod
    def _sanitize_local_response(
        cls, response: dict[str, Any], *, summary_key: str = "summary",
        allow_timeline_labels: bool = False,
    ) -> dict[str, Any]:
        content = response.get("content") if isinstance(response, dict) else None
        if not isinstance(content, dict):
            return response
        cleaned = dict(content)
        removed = 0
        summary = str(cleaned.get(summary_key) or "")
        removed_timeline = 0
        if not allow_timeline_labels:
            summary, removed_timeline = _TIMELINE_RANGE.subn("", summary)
            summary = re.sub(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)\s*", "", summary)
        clauses = re.split(r"(?<=[。！？；;])", summary)
        kept: list[str] = []
        for clause in clauses:
            if _LOCAL_META_COMMENTARY.search(clause):
                removed += 1
            else:
                kept.append(clause)
        cleaned[summary_key] = "".join(kept).strip()
        for key, value in list(cleaned.items()):
            if key == summary_key or not isinstance(value, list):
                continue
            filtered = [str(item) for item in value if not _LOCAL_META_COMMENTARY.search(str(item))]
            removed += len(value) - len(filtered)
            cleaned[key] = filtered
        if not removed and not removed_timeline:
            return response
        metadata = dict(response.get("metadata", {}))
        if removed:
            metadata["local_meta_comments_removed"] = removed
        if removed_timeline:
            metadata["local_timeline_labels_removed"] = removed_timeline
        return {**response, "content": cleaned, "metadata": metadata}

    @staticmethod
    def _merge_semantic_retry(first: dict[str, Any], second: dict[str, Any], violations: list[str]) -> dict[str, Any]:
        first_meta = first.get("metadata", {})
        second_meta = second.get("metadata", {})
        first_usage = first_meta.get("usage", {})
        second_usage = second_meta.get("usage", {})
        usage: dict[str, Any] = {}
        for key in {
            "prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        }:
            usage[key] = int(first_usage.get(key, 0) or 0) + int(second_usage.get(key, 0) or 0)
        first_details = first_usage.get("completion_tokens_details", {})
        second_details = second_usage.get("completion_tokens_details", {})
        if first_details or second_details:
            usage["completion_tokens_details"] = {
                "reasoning_tokens": int(first_details.get("reasoning_tokens", 0) or 0)
                + int(second_details.get("reasoning_tokens", 0) or 0)
            }
        metadata = {**second_meta, "usage": usage}
        metadata["usage_by_request"] = list(first_meta.get("usage_by_request", [])) + list(
            second_meta.get("usage_by_request", [])
        )
        metadata["attempts"] = int(first_meta.get("attempts", 1) or 1) + int(second_meta.get("attempts", 1) or 1)
        metadata["semantic_retries"] = 1
        metadata["semantic_retry_reason"] = violations
        metadata["thinking_output_characters"] = int(first_meta.get("thinking_output_characters", 0) or 0) + int(
            second_meta.get("thinking_output_characters", 0) or 0
        )
        first_perf = first_meta.get("performance", {})
        second_perf = second_meta.get("performance", {})
        if first_perf or second_perf:
            metadata["performance"] = {
                key: round(float(first_perf.get(key, 0) or 0) + float(second_perf.get(key, 0) or 0), 6)
                for key in {
                    "wall_seconds", "ollama_total_seconds", "model_load_seconds", "prompt_eval_seconds",
                    "generation_seconds",
                }
            }
        return {**second, "metadata": metadata}

    def analysis_profile(self) -> dict[str, Any]:
        local_config = getattr(self.local_client, "config", None)
        final_config = getattr(self.final_client, "config", None)
        short_budget, long_budget, final_budget = self._thinking_budgets()
        effective_final_client = self.local_client if self.local_final_thinking else self.final_client
        effective_final_config = getattr(effective_final_client, "config", None)
        return {
            "client": type(self).__name__,
            "model": f"{getattr(local_config, 'model', 'local')}+{getattr(final_config, 'model', 'final')}",
            "base_url": "local-only" if self.local_final_thinking else "mixed-local-and-cloud",
            "local_thinking": self.local_thinking,
            "local_final_thinking": self.local_final_thinking,
            "local_window_prompt_contract": LOCAL_WINDOW_PROMPT_CONTRACT_VERSION,
            "local_thinking_limits": (
                {
                    "reasoning_effort": "low",
                    "short_max_tokens": short_budget,
                    "long_max_tokens": long_budget,
                    "final_max_tokens": final_budget,
                    "request_timeout_seconds": getattr(local_config, "thinking_timeout_seconds", 30.0),
                }
                if self.local_thinking else None
            ),
            "stages": {
                "short_summaries": {
                    "client": type(self.local_client).__name__,
                    "model": str(getattr(local_config, "model", "local")),
                    "base_url": str(getattr(local_config, "base_url", "local")),
                },
                "long_summaries": {
                    "client": type(self.local_client).__name__,
                    "model": str(getattr(local_config, "model", "local")),
                    "base_url": str(getattr(local_config, "base_url", "local")),
                },
                "final_overview": {
                    "client": type(effective_final_client).__name__,
                    "model": str(getattr(effective_final_config, "model", "final")),
                    "base_url": str(getattr(effective_final_config, "base_url", "cloud")),
                },
            },
        }

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        is_window_stage = kwargs.get("thinking") == "disabled"
        client = self.local_client if is_window_stage or self.local_final_thinking else self.final_client
        if is_window_stage:
            short_stage = int(kwargs.get("max_tokens") or 0) <= 1024
            kwargs = {
                **kwargs,
                "system_prompt": str(kwargs.get("system_prompt") or "")
                + self._local_window_contract(short_stage=short_stage),
            }
        elif self.local_final_thinking:
            kwargs = {
                **kwargs,
                "system_prompt": str(kwargs.get("system_prompt") or "")
                + "\n本地最终总结额外约束：overall_summary必须是约400至600字的整体性总结，"
                "概括全场主要活动、关系与结果，不按时间顺序展开；chronological_summary必须是约500至900字的"
                "按照hours的先后顺序进展，写出具体nickname、行动、决定、状态变化和因果；可按需要使用‘第N小时’、"
                "相对阶段或具体时间表达，但不强制每段加入时间标签。两者不可互相替代或重复。"
                "关键信息数组的项目数量由证据的重要性和完整性自行决定，不设固定数量上限；每项最多140个汉字。"
                "顺序段只在需要时自然连接，"
                "不要每段固定以‘首先、随后、之后、最后’开头。只陈述用户动向和可核实事实，"
                "禁止评论ASR、误转写、术语争议、断句、文本质量或表达方式。",
            }
        if is_window_stage and self.local_thinking:
            short_budget, long_budget, _ = self._thinking_budgets()
            budget = short_budget if int(kwargs.get("max_tokens") or 0) <= 1024 else long_budget
            kwargs = {
                **kwargs,
                "thinking": "enabled",
                "reasoning_effort": "low",
                "max_tokens": budget,
            }
        elif not is_window_stage and self.local_final_thinking:
            _, _, final_budget = self._thinking_budgets()
            kwargs = {
                **kwargs,
                "thinking": "enabled",
                "reasoning_effort": "low",
                "max_tokens": final_budget,
            }
        if is_window_stage:
            response = self._sanitize_local_response(client.complete_json(**kwargs))
        elif self.local_final_thinking:
            response = self._sanitize_local_response(client.complete_json(**kwargs), summary_key="overall_summary")
            response = self._sanitize_local_response(
                response, summary_key="chronological_summary", allow_timeline_labels=True,
            )
            violations = self._local_final_output_violations(
                response, str(kwargs.get("user_prompt") or "")
            )
            if violations:
                retry_kwargs = {
                    **kwargs,
                    "system_prompt": str(kwargs.get("system_prompt") or "")
                    + "\n上一答案未通过最终总结门禁：" + "；".join(violations)
                    + "。请重新生成完整JSON；overall_summary必须是约500字的非顺序整体总结，"
                    "chronological_summary必须按hours写成多个连续句群。若原因是某项过短："
                    "即使某些时段内容稀疏，也不要压缩——应把每个小时里已确认的动向展开写，"
                    "包括谁、做了什么、说了什么、先后顺序与因果，宁可把单一动向写得更具体，也不要省略；"
                    "不要解释失败原因。",
                }
                retried = self._sanitize_local_response(client.complete_json(**retry_kwargs), summary_key="overall_summary")
                retried = self._sanitize_local_response(
                    retried, summary_key="chronological_summary", allow_timeline_labels=True,
                )
                remaining = self._local_final_output_violations(
                    retried, str(retry_kwargs.get("user_prompt") or "")
                )
                if remaining:
                    raise ValueError("local final overview failed output gate after retry: " + "; ".join(remaining))
                response = self._merge_semantic_retry(response, retried, violations)
            return response
        else:
            return client.complete_json(**kwargs)
        violations = self._local_output_violations(response, str(kwargs.get("user_prompt") or ""), short_stage=short_stage)
        if not violations:
            return response
        participants = sorted(self._evidence_participants(
            str(kwargs.get("user_prompt") or ""), short_stage=short_stage
        ))
        nickname_hint = (
            "允许使用的nickname仅限：" + "、".join(participants)
            + "；summary必须逐字使用其中至少一个nickname。"
            if participants else ""
        )
        retry_kwargs = {
            **kwargs,
            "system_prompt": str(kwargs.get("system_prompt") or "")
            + "\n上一答案未通过本地输出门禁：" + "；".join(violations)
            + "。" + nickname_hint
            + "若原因是summary过短：请勿压缩，而是把证据中每个用户动向展开写——包括谁、做了什么、"
            "说了什么、先后顺序和因果，尽量达到200个汉字以上；宁可把单一动向写得更具体，也不要省略。"
            "重新生成完整JSON并逐项满足；不要解释失败原因。",
        }
        retried = self._sanitize_local_response(client.complete_json(**retry_kwargs))
        remaining = self._local_output_violations(
            retried, str(retry_kwargs.get("user_prompt") or ""), short_stage=short_stage
        )
        if remaining:
            raise ValueError("local window summary failed output gate after retry: " + "; ".join(remaining))
        return self._merge_semantic_retry(response, retried, violations)

    def stage_policy(self) -> dict[str, Any]:
        if not self.local_thinking:
            return {}
        timeout = getattr(getattr(self.local_client, "config", None), "thinking_timeout_seconds", 30.0)
        short_budget, long_budget, final_budget = self._thinking_budgets()
        policy = {
            "short_summaries": {
                "thinking": "enabled",
                "reasoning_effort": "low",
                "initial_max_tokens": short_budget,
                "request_timeout_seconds": timeout,
                "limit_note": "Qwen3 supports boolean thinking; low mode is enforced by token and wall-time limits",
            },
            "long_summaries": {
                "thinking": "enabled",
                "reasoning_effort": "low",
                "initial_max_tokens": long_budget,
                "request_timeout_seconds": timeout,
                "limit_note": "Qwen3 supports boolean thinking; low mode is enforced by token and wall-time limits",
            },
        }
        if self.local_final_thinking:
            policy["final_overview"] = {
                "thinking": "enabled",
                "reasoning_effort": "low",
                "reasoning_effort_note": "Qwen3 local thinking is bounded by token and wall-time limits",
                "initial_max_tokens": final_budget,
                "request_timeout_seconds": timeout,
                "limit_note": "Qwen3 final overview uses local boolean thinking with token and wall-time limits",
            }
        return policy


class BaselineFinalClient:
    """Benchmark-only final-stage reuse. It must never be selected as a production route."""

    def __init__(self, result_path: Path):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.overview = result["summary"]
        self.report_id = result["report_id"]
        self.config = SimpleNamespace(model="baseline-final-reuse", base_url="offline")

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("thinking") != "enabled":
            raise ValueError("baseline final reuse may only handle the final overview")
        required = kwargs["required_keys"]
        content = {key: self.overview[key] for key in required}
        return {
            "content": content,
            "metadata": {
                "provider": "baseline-reuse",
                "api_called": False,
                "model_requested": "baseline-final-reuse",
                "model_returned": "baseline-final-reuse",
                "thinking": "enabled",
                "reasoning_effort": None,
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
                "source_report_id": self.report_id,
            },
        }
