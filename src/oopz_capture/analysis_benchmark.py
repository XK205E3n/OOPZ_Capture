from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from .analyzer_job import load_analyzer_input
from .output import write_json


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", value).lower()


def _ngrams(value: str, size: int = 2) -> set[str]:
    text = _normalize(value)
    if len(text) < size:
        return {text} if text else set()
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def _jaccard(left: str, right: str) -> float:
    a, b = _ngrams(left), _ngrams(right)
    return 1.0 if not a and not b else (len(a & b) / len(a | b) if a | b else 0.0)


def _lcs_f1(left: str, right: str) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    previous = [0] * (len(b) + 1)
    for char in a:
        current = [0]
        for index, other in enumerate(b, start=1):
            current.append(previous[index - 1] + 1 if char == other else max(previous[index], current[-1]))
        previous = current
    length = previous[-1]
    precision = length / len(a)
    recall = length / len(b)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _grounding(summary: str, source: str) -> float:
    candidate = _ngrams(summary, 2)
    evidence = _ngrams(source, 2)
    return len(candidate & evidence) / len(candidate) if candidate else 1.0


def _window_source(transcript: list[dict[str, Any]], start_ms: int, end_ms: int) -> str:
    return " ".join(
        str(item.get("text") or "")
        for item in transcript
        if int(item.get("end_ms", 0)) > start_ms and int(item.get("start_ms", 0)) < end_ms
    )


def _compare_windows(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    reference = {item["window_id"]: item for item in baseline}
    rows: list[dict[str, Any]] = []
    for item in candidate:
        other = reference.get(item["window_id"])
        if other is None:
            continue
        source = _window_source(transcript, int(item["start_ms"]), int(item["end_ms"]))
        rows.append({
            "window_id": item["window_id"],
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "rouge_l_f1": round(_lcs_f1(item["summary"], other["summary"]), 4),
            "character_bigram_jaccard": round(_jaccard(item["summary"], other["summary"]), 4),
            "candidate_grounding_bigram_recall": round(_grounding(item["summary"], source), 4),
            "baseline_grounding_bigram_recall": round(_grounding(other["summary"], source), 4),
            "speaker_sets_equal": sorted(value.get("nickname", "") for value in item.get("speakers", [])) == sorted(
                value.get("nickname", "") for value in other.get("speakers", [])
            ),
            "baseline_summary": other["summary"],
            "candidate_summary": item["summary"],
        })
    return {
        "matched_windows": len(rows),
        "missing_candidate_windows": max(0, len(baseline) - len(rows)),
        "average_rouge_l_f1": round(mean(row["rouge_l_f1"] for row in rows), 4) if rows else None,
        "average_character_bigram_jaccard": round(mean(row["character_bigram_jaccard"] for row in rows), 4) if rows else None,
        "average_candidate_grounding_bigram_recall": round(mean(row["candidate_grounding_bigram_recall"] for row in rows), 4) if rows else None,
        "average_baseline_grounding_bigram_recall": round(mean(row["baseline_grounding_bigram_recall"] for row in rows), 4) if rows else None,
        "speaker_set_match_rate": round(mean(1.0 if row["speaker_sets_equal"] else 0.0 for row in rows), 4) if rows else None,
        "windows": rows,
    }


def _request_span(result: dict[str, Any]) -> float | None:
    requested: list[datetime] = []
    models = [item.get("model") or {} for item in result.get("short_summaries", [])]
    models += [item.get("model") or {} for item in result.get("long_summaries", [])]
    models.append(result.get("model", {}).get("final") or {})
    for model in models:
        raw = model.get("requested_at")
        if isinstance(raw, str):
            try:
                requested.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                pass
    try:
        completed = datetime.fromisoformat(str(result["completed_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return round((completed - min(requested)).total_seconds(), 3) if requested else None


def _render_markdown(path: Path, comparison: dict[str, Any]) -> None:
    short = comparison["accuracy"]["short_summaries"]
    long = comparison["accuracy"]["long_summaries"]
    final = comparison["accuracy"]["final_overview"]
    gpu = comparison["performance"]["candidate_gpu"]
    lines = [
        "# DeepSeek 与本地 Qwen 混合路线对比", "",
        f"Session ID: {comparison['session_id']}",
        f"DeepSeek 基线 Report ID: {comparison['baseline_report_id']}",
        f"Qwen 混合路线 Report ID: {comparison['candidate_report_id']}", "",
        "## 结论使用限制", "",
        "自动分数衡量文本相似度和逐字依据覆盖，不等同于事实准确率。最终取舍必须结合下方并排样本人工阅读。",
        "DeepSeek 基线也可能有遗漏或错误，因此不能把与基线一致直接解释为正确。", "",
        "## 准确度代理指标", "",
        "| 层级 | 匹配窗口 | ROUGE-L F1 | 字符二元组 Jaccard | Qwen依据覆盖 | DeepSeek依据覆盖 | 说话人集合一致率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| 300秒 | {short['matched_windows']} | {short['average_rouge_l_f1']} | {short['average_character_bigram_jaccard']} | {short['average_candidate_grounding_bigram_recall']} | {short['average_baseline_grounding_bigram_recall']} | {short['speaker_set_match_rate']} |",
        f"| 60分钟 | {long['matched_windows']} | {long['average_rouge_l_f1']} | {long['average_character_bigram_jaccard']} | {long['average_candidate_grounding_bigram_recall']} | {long['average_baseline_grounding_bigram_recall']} | {long['speaker_set_match_rate']} |",
        (
            "| 最终总览 | — | — | — | — | — | — |"
            if final.get("reused_from_baseline") else
            f"| 最终总览 | 1 | {final['rouge_l_f1']} | {final['character_bigram_jaccard']} | — | — | — |"
        ), "",
        "## 运行时间和GPU", "",
        f"- DeepSeek 旧基线的可推断请求跨度：{comparison['performance']['baseline_recorded_request_span_seconds']} 秒；它不是完整墙钟计时。",
        f"- Qwen 混合路线完整墙钟时间：{gpu.get('wall_seconds')} 秒。",
        "- DeepSeek 模型推理由云端完成，本机推理GPU占用按架构为0；旧基线没有采样当时的桌面GPU总负载。",
    ]
    if gpu.get("available"):
        lines.extend([
            f"- Qwen 测试期间GPU：峰值总显存 {gpu['peak_memory_used_mb']} MB，相对起点增加 {gpu['peak_memory_delta_mb']} MB；平均/峰值利用率 {gpu['average_utilization_pct']}% / {gpu['peak_utilization_pct']}%。",
            f"- 平均/峰值功耗 {gpu['average_power_w']} W / {gpu['peak_power_w']} W，估算能耗 {gpu['estimated_energy_wh']} Wh。采样包含其他桌面负载。",
        ])
    else:
        lines.append(f"- GPU采样不可用：{gpu.get('reason', '未知原因')}。")
    lines.extend(["", "## Token与费用", ""])
    for label, result in (("DeepSeek全API", comparison["baseline"]), ("Qwen混合", comparison["candidate"])):
        usage = result.get("model", {}).get("usage", {})
        cost = result.get("model", {}).get("cost_estimate", {})
        lines.append(
            f"- {label}：输入 {usage.get('prompt_tokens', 0)}，输出 {usage.get('completion_tokens', 0)}，总 Token {usage.get('total_tokens', 0)}；"
            f"DeepSeek估算费用 ¥{float(cost.get('total_estimated_cost_rmb') or 0):.6f}。"
        )
    lines.extend(["", "## 人工复核：60分钟摘要", ""])
    for row in long["windows"]:
        lines.extend([
            f"### {row['window_id']}", "",
            f"- DeepSeek：{row['baseline_summary']}",
            f"- Qwen：{row['candidate_summary']}", "",
        ])
    lines.extend(["## 人工复核：差异最大的300秒窗口", ""])
    for row in sorted(short["windows"], key=lambda item: item["rouge_l_f1"])[:5]:
        lines.extend([
            f"### {row['window_id']}（ROUGE-L={row['rouge_l_f1']}）", "",
            f"- DeepSeek：{row['baseline_summary']}",
            f"- Qwen：{row['candidate_summary']}", "",
        ])
    lines.extend([
        "## 人工复核：最终总览", "",
        (
            "本次为无API的窗口阶段基准，最终总览直接复用DeepSeek基线，因此不参与准确度比较。"
            if final.get("reused_from_baseline") else
            f"- DeepSeek：{final['baseline_summary']}\n- Qwen混合：{final['candidate_summary']}"
        ), "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_comparison(
    handoff_path: Path,
    baseline_result_path: Path,
    candidate_result_path: Path,
    candidate_gpu: dict[str, Any],
    output_dir: Path,
    *,
    final_reused_from_baseline: bool = False,
) -> dict[str, Any]:
    value = load_analyzer_input(handoff_path)
    baseline = json.loads(baseline_result_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_result_path.read_text(encoding="utf-8"))
    short = _compare_windows(baseline["short_summaries"], candidate["short_summaries"], value.transcript)
    long = _compare_windows(baseline["long_summaries"], candidate["long_summaries"], value.transcript)
    baseline_overview = baseline["summary"]["overall_summary"]
    candidate_overview = candidate["summary"]["overall_summary"]
    def route_summary(result: dict[str, Any]) -> dict[str, Any]:
        model = result.get("model", {})
        return {
            "report_id": result.get("report_id"),
            "analysis_profile": result.get("analysis_profile", {}),
            "model": {
                "usage": model.get("usage", {}),
                "usage_by_stage": model.get("usage_by_stage", {}),
                "cost_estimate": model.get("cost_estimate", {}),
            },
        }

    comparison = {
        "schema_version": "oopz.analysis.benchmark.v1",
        "session_id": value.session_id,
        "baseline_report_id": baseline["report_id"],
        "candidate_report_id": candidate["report_id"],
        "accuracy": {
            "method": "reference similarity plus transcript bigram grounding heuristic; human review required",
            "short_summaries": short,
            "long_summaries": long,
            "final_overview": {
                "rouge_l_f1": round(_lcs_f1(candidate_overview, baseline_overview), 4),
                "character_bigram_jaccard": round(_jaccard(candidate_overview, baseline_overview), 4),
                "baseline_summary": baseline_overview,
                "candidate_summary": candidate_overview,
                "reused_from_baseline": final_reused_from_baseline,
            },
        },
        "performance": {
            "baseline_recorded_request_span_seconds": _request_span(baseline),
            "baseline_local_inference_gpu": "0 by architecture; cloud inference, historical desktop load not sampled",
            "candidate_gpu": candidate_gpu,
        },
        "baseline": route_summary(baseline),
        "candidate": route_summary(candidate),
    }
    json_path = output_dir / "comparison.json"
    markdown_path = output_dir / "comparison.md"
    write_json(json_path, comparison)
    _render_markdown(markdown_path, comparison)
    return {"comparison": comparison, "json_path": json_path, "markdown_path": markdown_path}
