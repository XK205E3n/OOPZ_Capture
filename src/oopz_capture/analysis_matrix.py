from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from .analysis_pipeline import (
    ANALYSIS_PIPELINE_VERSION,
    _aggregate_usage,
    _estimate_costs,
    refresh_analysis_report,
    run_analysis,
)
from .analysis_routes import LOCAL_WINDOW_PROMPT_CONTRACT_VERSION, StageRoutedClient
from .analyzer_job import load_analyzer_input
from .performance import NvidiaSMIMonitor
from .pdf_reports import render_session_reports
from .workflow import _is_reparse_point, utc_now


SHARED_CACHE_SCHEMA = "oopz.analysis.shared-stage-cache.v1"
MATRIX_SCHEMA = "oopz.analysis.matrix.v1"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageSpec:
    provider: str
    thinking: str
    reasoning_effort: str | None
    max_tokens: int


@dataclass(frozen=True)
class RoutePlan:
    route_id: str
    variant: str
    title: str
    short: StageSpec
    long: StageSpec
    final: StageSpec

    def stage(self, name: str) -> StageSpec:
        return {"short_summaries": self.short, "long_summaries": self.long, "final_overview": self.final}[name]


def _type_names(value: type | tuple[type, ...]) -> list[str]:
    values = value if isinstance(value, tuple) else (value,)
    return sorted(item.__name__ for item in values)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _response_valid(response: Any, required_keys: dict[str, type | tuple[type, ...]]) -> bool:
    if not isinstance(response, dict) or not isinstance(response.get("metadata"), dict):
        return False
    content = response.get("content")
    return isinstance(content, dict) and all(
        key in content and isinstance(content[key], expected) for key, expected in required_keys.items()
    )


class SharedStageCache:
    """Deduplicate exact stage requests while retaining route-specific reports."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if self.root.exists() and (_is_reparse_point(self.root) or not self.root.is_dir()):
            raise RuntimeError(f"unsafe shared analysis cache: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []

    def execute(
        self,
        *,
        route_id: str,
        stage: str,
        spec: StageSpec,
        identity: dict[str, Any],
        kwargs: dict[str, Any],
        callback: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        descriptor = {
            "schema_version": SHARED_CACHE_SCHEMA,
            "pipeline_version": ANALYSIS_PIPELINE_VERSION,
            "local_prompt_contract": LOCAL_WINDOW_PROMPT_CONTRACT_VERSION if spec.provider == "qwen" else None,
            "stage": stage,
            "spec": {
                "provider": spec.provider,
                "thinking": spec.thinking,
                "reasoning_effort": spec.reasoning_effort,
                "max_tokens": spec.max_tokens,
            },
            "identity": identity,
            "system_prompt": str(kwargs.get("system_prompt") or ""),
            "user_prompt": str(kwargs.get("user_prompt") or ""),
            "required_keys": {
                key: _type_names(expected) for key, expected in sorted(kwargs["required_keys"].items())
            },
        }
        serialized = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = sha256(serialized.encode("utf-8")).hexdigest()
        path = self.root / f"{key}.json"
        hit = False
        entry: dict[str, Any] | None = None
        if path.is_file() and not _is_reparse_point(path):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
                if (
                    candidate.get("schema_version") == SHARED_CACHE_SCHEMA
                    and candidate.get("cache_key") == key
                    and candidate.get("descriptor") == descriptor
                    and _response_valid(candidate.get("response"), kwargs["required_keys"])
                ):
                    entry = candidate
                    hit = True
            except (OSError, ValueError, json.JSONDecodeError):
                entry = None
        if entry is None:
            response = callback()
            if not _response_valid(response, kwargs["required_keys"]):
                raise ValueError(f"invalid {stage} response cannot enter shared cache")
            metadata = dict(response["metadata"])
            metadata.update({
                "shared_stage_cache_key": key,
                "shared_stage_cache_hit": False,
                "shared_stage_cache_schema": SHARED_CACHE_SCHEMA,
            })
            response = {**response, "metadata": metadata}
            entry = {
                "schema_version": SHARED_CACHE_SCHEMA,
                "cache_key": key,
                "descriptor": descriptor,
                "created_at": utc_now().isoformat(timespec="milliseconds"),
                "response": response,
            }
            _atomic_json(path, entry)
        response = json.loads(json.dumps(entry["response"], ensure_ascii=False))
        response["metadata"]["shared_stage_cache_hit"] = hit
        response["metadata"]["shared_stage_cache_key"] = key
        self.events.append({
            "route_id": route_id,
            "stage": stage,
            "provider": spec.provider,
            "cache_key": key,
            "cache_hit": hit,
        })
        LOGGER.info(
            "matrix stage completed route=%s stage=%s provider=%s cache_hit=%s",
            route_id, stage, spec.provider, hit,
        )
        return response

    def entry(self, key: str) -> dict[str, Any]:
        path = self.root / f"{key}.json"
        if not path.is_file() or _is_reparse_point(path):
            raise FileNotFoundError(f"shared cache entry missing: {key}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != SHARED_CACHE_SCHEMA or value.get("cache_key") != key:
            raise ValueError(f"invalid shared cache entry: {key}")
        return value


def build_route_plans(local_adapter: StageRoutedClient) -> tuple[RoutePlan, ...]:
    local_policy = local_adapter.stage_policy()
    q_short = StageSpec("qwen", "enabled", "low", int(local_policy["short_summaries"]["initial_max_tokens"]))
    q_long = StageSpec("qwen", "enabled", "low", int(local_policy["long_summaries"]["initial_max_tokens"]))
    q_final = StageSpec("qwen", "enabled", "low", int(local_policy["final_overview"]["initial_max_tokens"]))
    ds_long = StageSpec("deepseek", "enabled", "high", 4096)
    ds_final = StageSpec("deepseek", "enabled", "high", 4096)
    return (
        RoutePlan("route-2", "matrix-r2-qwen-ds-ds", "Qwen / DeepSeek / DeepSeek", q_short, ds_long, ds_final),
        RoutePlan("route-3", "matrix-r3-qwen-qwen-ds", "Qwen / Qwen / DeepSeek", q_short, q_long, ds_final),
        RoutePlan("route-4", "matrix-r4-qwen-qwen-qwen", "Qwen / Qwen / Qwen", q_short, q_long, q_final),
    )


class MatrixRouteClient:
    def __init__(
        self,
        plan: RoutePlan,
        deepseek_client: Any,
        local_adapter: StageRoutedClient,
        cache: SharedStageCache,
    ):
        self.plan = plan
        self.deepseek_client = deepseek_client
        self.local_adapter = local_adapter
        self.cache = cache

    @staticmethod
    def _stage(kwargs: dict[str, Any]) -> str:
        required = kwargs.get("required_keys") or {}
        if "overall_summary" in required:
            return "final_overview"
        return "short_summaries" if int(kwargs.get("max_tokens") or 0) <= 1024 else "long_summaries"

    @staticmethod
    def _identity(client: Any) -> dict[str, Any]:
        config = getattr(client, "config", None)
        return {
            "client": type(client).__name__,
            "model": str(getattr(config, "model", "unknown")),
            "base_url": str(getattr(config, "base_url", "unknown")),
        }

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        stage = self._stage(kwargs)
        spec = self.plan.stage(stage)
        if spec.provider == "deepseek":
            effective = {
                **kwargs,
                "thinking": spec.thinking,
                "reasoning_effort": spec.reasoning_effort,
                "max_tokens": spec.max_tokens,
            }
            client = self.deepseek_client
            callback = lambda: client.complete_json(**effective)
        else:
            # Window calls keep the pipeline's disabled marker until the local adapter has attached
            # the natural-Chinese prompt contract; the adapter then enables bounded Qwen thinking.
            effective = dict(kwargs)
            client = self.local_adapter.local_client
            callback = lambda: self.local_adapter.complete_json(**effective)
        identity = self._identity(client)
        if spec.provider == "qwen":
            identity["adapter_profile"] = self.local_adapter.analysis_profile()
        return self.cache.execute(
            route_id=self.plan.route_id,
            stage=stage,
            spec=spec,
            identity=identity,
            kwargs=effective,
            callback=callback,
        )

    def analysis_profile(self) -> dict[str, Any]:
        stages = {}
        for name in ("short_summaries", "long_summaries", "final_overview"):
            spec = self.plan.stage(name)
            source = self.deepseek_client if spec.provider == "deepseek" else self.local_adapter.local_client
            stages[name] = {
                **self._identity(source),
                "provider_route": spec.provider,
                "thinking": spec.thinking,
                "reasoning_effort": spec.reasoning_effort,
                "max_tokens": spec.max_tokens,
            }
        return {
            "client": type(self).__name__,
            "model": f"analysis-matrix:{self.plan.route_id}",
            "base_url": "stage-routed-with-shared-cache",
            "matrix_route_id": self.plan.route_id,
            "matrix_route_title": self.plan.title,
            "shared_cache_schema": SHARED_CACHE_SCHEMA,
            "local_window_prompt_contract": LOCAL_WINDOW_PROMPT_CONTRACT_VERSION,
            "stages": stages,
        }

    def stage_policy(self) -> dict[str, Any]:
        policy: dict[str, Any] = {}
        for name in ("short_summaries", "long_summaries", "final_overview"):
            spec = self.plan.stage(name)
            policy[name] = {
                "thinking": spec.thinking,
                "reasoning_effort": spec.reasoning_effort,
                "initial_max_tokens": spec.max_tokens,
                "shared_stage_cache": True,
            }
        return policy


def _result_models(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    values = [("short_summaries", item.get("model") or {}) for item in result.get("short_summaries", [])]
    values.extend(("long_summaries", item.get("model") or {}) for item in result.get("long_summaries", []))
    values.append(("final_overview", result.get("model", {}).get("final") or {}))
    return values


def _shared_usage(cache: SharedStageCache, route_outputs: list[dict[str, Any]]) -> dict[str, Any]:
    keys_by_stage: dict[str, set[str]] = {
        "short_summaries": set(), "long_summaries": set(), "final_overview": set()
    }
    logical_calls = 0
    for output in route_outputs:
        for stage, model in _result_models(output["result"]):
            key = model.get("shared_stage_cache_key")
            if isinstance(key, str) and key:
                keys_by_stage[stage].add(key)
                logical_calls += 1
    models_by_stage: dict[str, list[dict[str, Any]]] = {}
    usage_by_stage: dict[str, dict[str, Any]] = {}
    for stage, keys in keys_by_stage.items():
        models = [cache.entry(key)["response"]["metadata"] for key in sorted(keys)]
        models_by_stage[stage] = models
        usage_by_stage[stage] = _aggregate_usage([{"model": model} for model in models])
    all_models = [model for values in models_by_stage.values() for model in values]
    usage_by_stage["total"] = _aggregate_usage([{"model": model} for model in all_models])
    models_by_stage["total"] = all_models
    cost = _estimate_costs("analysis-matrix", usage_by_stage, models_by_stage)
    unique_calls = len(all_models)
    return {
        "logical_route_calls": logical_calls,
        "unique_stage_results": unique_calls,
        "avoided_duplicate_calls": max(0, logical_calls - unique_calls),
        "reduction_ratio": round((logical_calls - unique_calls) / logical_calls, 6) if logical_calls else 0.0,
        "physical_calls_this_run": sum(1 for item in cache.events if not item["cache_hit"]),
        "cache_hits_this_run": sum(1 for item in cache.events if item["cache_hit"]),
        "unique_keys_by_stage": {key: len(value) for key, value in keys_by_stage.items()},
        "usage_by_stage": usage_by_stage,
        "deepseek_cost_estimate": cost,
    }


def _render_review(path: Path, manifest: dict[str, Any]) -> None:
    shared = manifest["shared_resources"]
    lines = [
        "# 三路线分阶段分析报告", "", f"Session ID: {manifest['session_id']}", "",
        "## 共享调用结果", "",
        f"- 三条路线逻辑上共使用 {shared['logical_route_calls']} 个非静音阶段结果。",
        f"- 按真实输入指纹去重后只需 {shared['unique_stage_results']} 个唯一结果，避免 "
        f"{shared['avoided_duplicate_calls']} 次重复调用（{shared['reduction_ratio']:.1%}）。",
        f"- 本次命令实际发起 {shared['physical_calls_this_run']} 次模型调用，命中现有共享缓存 "
        f"{shared['cache_hits_this_run']} 次。", "",
        "| 阶段 | 唯一结果 | 输入 Token | 输出 Token | 总 Token | API调用 | 本地调用 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for stage, label in (("short_summaries", "300秒"), ("long_summaries", "60分钟"), ("final_overview", "最终总览")):
        usage = shared["usage_by_stage"][stage]
        lines.append(
            f"| {label} | {shared['unique_keys_by_stage'][stage]} | {usage.get('prompt_tokens', 0)} | "
            f"{usage.get('completion_tokens', 0)} | {usage.get('total_tokens', 0)} | "
            f"{usage.get('api_calls', 0)} | {usage.get('local_calls', 0)} |"
        )
    total = shared["usage_by_stage"]["total"]
    lines.extend([
        f"| 总计 | {shared['unique_stage_results']} | {total.get('prompt_tokens', 0)} | "
        f"{total.get('completion_tokens', 0)} | {total.get('total_tokens', 0)} | "
        f"{total.get('api_calls', 0)} | {total.get('local_calls', 0)} |", "",
        f"唯一 DeepSeek 调用按官方新峰谷价估算：¥{float(shared['deepseek_cost_estimate'].get('total_estimated_cost_rmb') or 0):.6f}。", "",
        "各路线报告中的 Token 和费用表示该路线独立成立时所使用的阶段结果；不要把三份报告的费用直接相加。上表才是共享执行后的唯一实际资源量。", "",
        "## 路线输出", "",
        "| 路线 | 300秒 | 60分钟 | 最终总览 | 路线总Token | DeepSeek估算费用 | 墙钟时间 | 最终报告 |",
        "|---|---|---|---|---:|---:|---:|---|",
    ])
    for route in manifest["routes"]:
        specs = route["stages"]
        usage = route["usage_by_stage"]["total"]
        cost = route["cost_estimate"].get("total_estimated_cost_rmb")
        lines.append(
            f"| {route['route_id']} | {specs['short_summaries']['label']} | {specs['long_summaries']['label']} | "
            f"{specs['final_overview']['label']} | {usage.get('total_tokens', 0)} | "
            f"¥{float(cost or 0):.6f} | {route['gpu'].get('wall_seconds')} 秒 | `{route['report_path']}` |"
        )
    final_fields = (
        ("key_topics", "主要话题"),
        ("decisions", "明确决定"),
        ("action_items", "行动项"),
        ("open_questions", "未解决问题"),
        ("important_moments", "重要时间点"),
        ("uncertainties", "不确定内容"),
    )
    lines.extend([
        "", "## 最终报告结构统计", "",
        "此表只统计输出规模，不能单独证明准确度；事实一致性仍需结合转写和四份正文人工审阅。", "",
        "| 路线 | 总览字数 | 主要话题 | 明确决定 | 行动项 | 未解决问题 | 重要时间点 | 不确定内容 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for route in manifest["routes"]:
        summary = route.get("final_summary") or {}
        lines.append(
            f"| {route['route_id']} | {len(str(summary.get('overall_summary') or ''))} | "
            + " | ".join(str(len(summary.get(field) or [])) for field, _ in final_fields)
            + " |"
        )
    lines.extend([
        "", "## 输入依赖关系", "",
        "- route-1：原始转录 → DeepSeek 300秒 → DeepSeek思考60分钟 → DeepSeek思考最终总览。",
        "- route-2：原始转录 → Qwen思考300秒 → DeepSeek思考60分钟 → DeepSeek思考最终总览。",
        "- route-3：原始转录 → Qwen思考300秒 → Qwen思考60分钟 → DeepSeek思考最终总览。",
        "- route-4：原始转录 → Qwen思考300秒 → Qwen思考60分钟 → Qwen思考最终总览。", "",
        "只有进入某一步的完整提示词和真实JSON证据一致，同时模型与思考设置也一致，才会复用。", "",
        "## 60分钟摘要并排阅读", "",
    ])
    if manifest["routes"]:
        for window in manifest["routes"][0]["long_summaries"]:
            lines.extend([
                f"### 第 {window['window_index']} 个60分钟窗口", "",
                f"Window ID: {window['window_id']}", "",
            ])
            for route in manifest["routes"]:
                matching = next(
                    (item for item in route["long_summaries"] if item["window_id"] == window["window_id"]), None
                )
                if matching:
                    lines.extend([f"- {route['route_id']}：{matching['summary']}", ""])
    lines.extend(["## 最终报告并排阅读", ""])
    for route in manifest["routes"]:
        summary = route.get("final_summary") or {}
        lines.extend([
            f"### {route['route_id']} — {route['title']}", "", f"Report ID: {route['report_id']}", "",
            "#### 总览", "", str(summary.get("overall_summary") or route["overall_summary"]), "",
        ])
        for field, label in final_fields:
            lines.extend([f"#### {label}", ""])
            values = summary.get(field) or []
            lines.extend([f"- {item}" for item in values] if values else ["- 无", ""])
            if values:
                lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def run_analysis_matrix(
    handoff_path: Path,
    deepseek_client: Any,
    qwen_client: Any,
    *,
    render_pdf: bool = False,
) -> dict[str, Any]:
    value = load_analyzer_input(handoff_path)
    matrix_dir = value.session_dir / "analysis_matrix"
    cache = SharedStageCache(value.session_dir / "analysis_shared_cache")
    local_adapter = StageRoutedClient(qwen_client, qwen_client, local_thinking=True, local_final_thinking=True)
    plans = build_route_plans(local_adapter)
    outputs: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    for plan in plans:
        LOGGER.info("matrix route started route=%s title=%s", plan.route_id, plan.title)
        client = MatrixRouteClient(plan, deepseek_client, local_adapter, cache)
        with NvidiaSMIMonitor() as monitor:
            output = run_analysis(handoff_path, client, variant=plan.variant, render_pdf=False)
        gpu = monitor.summary()
        gpu["measurement_valid"] = not output["reused"]
        if output["reused"]:
            gpu["measurement_note"] = "route result was reused; measured time is not model inference"
        output = refresh_analysis_report(
            output,
            value,
            runtime_metrics={"gpu": gpu},
            render_pdf=render_pdf,
        )
        outputs.append(output)
        stages = {}
        for name in ("short_summaries", "long_summaries", "final_overview"):
            spec = plan.stage(name)
            stages[name] = {
                "provider": spec.provider,
                "thinking": spec.thinking,
                "reasoning_effort": spec.reasoning_effort,
                "max_tokens": spec.max_tokens,
                "label": f"{spec.provider} / {spec.thinking}",
            }
        result = output["result"]
        routes.append({
            "route_id": plan.route_id,
            "variant": plan.variant,
            "title": plan.title,
            "stages": stages,
            "report_id": result["report_id"],
            "overall_summary": result["summary"]["overall_summary"],
            "final_summary": result["summary"],
            "result_path": str(output["result_path"]),
            "report_path": str(output["report_path"]),
            "qq_messages": str(output["qq_path"]),
            "reused": output["reused"],
            "usage_by_stage": result["model"]["usage_by_stage"],
            "cost_estimate": result["model"]["cost_estimate"],
            "long_summaries": [
                {
                    "window_id": item["window_id"],
                    "window_index": item["window_index"],
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "summary": item["summary"],
                }
                for item in result["long_summaries"]
            ],
            "gpu": gpu,
        })
        LOGGER.info(
            "matrix route completed route=%s reused=%s report=%s",
            plan.route_id, output["reused"], output["report_path"],
        )
    shared = _shared_usage(cache, outputs)
    manifest = {
        "schema_version": MATRIX_SCHEMA,
        "session_id": value.session_id,
        "request_id": value.request_id,
        "created_at": utc_now().isoformat(timespec="milliseconds"),
        "shared_cache_dir": str(cache.root),
        "shared_resources": shared,
        "routes": routes,
    }
    manifest_path = matrix_dir / "manifest.json"
    review_path = matrix_dir / "review.md"
    _atomic_json(manifest_path, manifest)
    _render_review(review_path, manifest)
    review_pdf = None
    if render_pdf:
        review_pdf = render_session_reports(value.session_dir, [(review_path, "comparison")])[0]
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "review_path": review_path,
        "review_pdf": review_pdf,
    }
