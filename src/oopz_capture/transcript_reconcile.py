"""Local Qwen reconciliation for the fixed SenseVoice zh + auto transcript pair."""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .ollama_client import OllamaClient, OllamaConfig, OllamaError
from .output import write_json, write_jsonl
from .transcript import render_transcript_markdown

_MAX_PROMPT_CHARS = 24_000


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def no_text_marker(session_dir: Path) -> dict[str, Any]:
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    duration_ms = max(1, round(float(session.get("duration_seconds") or 0) * 1000))
    started = datetime.fromisoformat(str(
        session.get("capture_clock_started_at") or session.get("started_at")
    ).replace("Z", "+00:00"))
    return {
        "segment_id": "no-speech",
        "session_id": str(session.get("session_id") or session_dir.name),
        "start_ms": 0,
        "end_ms": duration_ms,
        "start_time": started.isoformat(timespec="milliseconds"),
        "end_time": (started + timedelta(milliseconds=duration_ms)).isoformat(timespec="milliseconds"),
        "agora_uid": 0,
        "oopz_uid": "",
        "speaker": "系统",
        "text": "[该时间段未检测到有效语音文本]",
        "language": "none",
        "asr_backend": "sensevoice-small-dual",
        "transcript_source": "no-speech-marker",
        "overlap": False,
    }


def _batches(zh: list[dict[str, Any]], auto_by_id: dict[str, dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = [[]]
    used = 0
    for item in zh:
        other = auto_by_id.get(str(item["segment_id"]), {})
        size = len(str(item.get("text") or "")) + len(str(other.get("text") or "")) + 180
        if batches[-1] and used + size > _MAX_PROMPT_CHARS:
            batches.append([])
            used = 0
        batches[-1].append(item)
        used += size
    return [batch for batch in batches if batch]


def _prompt(batch: list[dict[str, Any]], auto_by_id: dict[str, dict[str, Any]]) -> str:
    rows = []
    for item in batch:
        other = auto_by_id.get(str(item["segment_id"]), {})
        rows.append(
            f"ID={item['segment_id']} | {item['start_ms']}-{item['end_ms']}ms | {item.get('speaker') or '未知'}\n"
            f"ZH={str(item.get('text') or '').strip()}\n"
            f"AUTO={str(other.get('text') or '').strip()}"
        )
    return "\n\n".join(rows)


_SYSTEM_PROMPT = """你是中文语音转写的校对器。输入是同一段多人语音的两个 SenseVoice 转写：ZH 为强制中文，AUTO 为自动语言识别。
频道主要是朋友间日常生活与多人游戏交流；ASR 可能错听人名、游戏名、英文词或口语。请结合相邻上下文、说话人、时间顺序判断哪一个候选更合理。
不要概括聊天内容，不要改写、润色、补全或臆测原音。每个决定只能在 ZH 或 AUTO 中二选一；若 ZH 没有明确劣势，不要列出它。只列出应采用 AUTO 的 ID，并给出极短理由。"""


def reconcile_sensevoice_pair(
    session_dir: Path,
    *,
    client: OllamaClient | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create canonical transcript.jsonl from transcript.zh and transcript.auto.

    The model can select an existing candidate only. This preserves auditability and
    prevents a transcript-correction step from inventing text absent from the audio.
    """
    zh_path = session_dir / "transcript.zh.jsonl"
    auto_path = session_dir / "transcript.auto.jsonl"
    if not zh_path.is_file() or not auto_path.is_file():
        raise ValueError("both transcript.zh.jsonl and transcript.auto.jsonl are required")
    zh = _read_jsonl(zh_path)
    auto_by_id = {str(item["segment_id"]): item for item in _read_jsonl(auto_path)}
    missing = [str(item["segment_id"]) for item in zh if str(item["segment_id"]) not in auto_by_id]

    if not zh and not auto_by_id:
        marker = no_text_marker(session_dir)
        final = [marker]
        write_jsonl(session_dir / "transcript.jsonl", final)
        render_transcript_markdown(session_dir, final)
        summary = {
            "session_id": session_dir.name,
            "asr_backend": "sensevoice-small-dual-no-speech",
            "segments": 1,
            "source_transcripts": {"zh": "transcript.zh.jsonl", "auto": "transcript.auto.jsonl"},
            "selection_counts": {"no-speech-marker": 1},
            "auto_missing_segment_count": 0,
            "qwen_decisions": 0,
            "qwen_metadata": [],
            "no_speech": True,
        }
        write_json(session_dir / "transcript_summary.json", summary)
        write_json(session_dir / "transcript.reconcile.json", {
            "schema_version": "oopz.transcript.reconcile.v1",
            "decisions": [], "metadata": [], "no_speech": True,
        })
        return final, summary

    if not zh and auto_by_id:
        zh = [dict(item) for item in auto_by_id.values()]

    if client is None:
        config = OllamaConfig.from_env()
        reconcile_retries = int(os.environ.get("OOPZ_RECONCILE_MAX_RETRIES", "3"))
        if not 0 <= reconcile_retries <= 5:
            raise ValueError("OOPZ_RECONCILE_MAX_RETRIES must be 0 to 5")
        client = OllamaClient(replace(
            config,
            max_retries=max(config.max_retries, reconcile_retries),
            thinking_timeout_seconds=min(config.thinking_timeout_seconds, 180.0),
        ))
    selected_auto: set[str] = set()
    decisions: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    for index, batch in enumerate(_batches(zh, auto_by_id), start=1):
        user_prompt = (
            "以下是本批候选。JSON 的 corrections 只写选择 AUTO 的记录，格式为 "
            '{"corrections":[{"segment_id":"ID","source":"auto","reason":"简短理由"}]}'
            "；无须修改则返回空数组。\n\n" + _prompt(batch, auto_by_id)
        )
        try:
            response = client.complete_json(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                required_keys={"corrections": list},
                thinking="enabled",
                reasoning_effort="low",
                max_tokens=4096,
            )
        except OllamaError as thinking_error:
            # Reconciliation is a constrained candidate selection task. If local
            # thinking runs away or repeatedly emits malformed JSON, a bounded
            # non-thinking retry is preferable to losing the whole audio chunk.
            response = client.complete_json(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                required_keys={"corrections": list},
                thinking="disabled",
                reasoning_effort=None,
                max_tokens=4096,
            )
            response["metadata"]["fallback"] = "nonthinking_after_thinking_failure"
            response["metadata"]["thinking_failure"] = str(thinking_error)[:500]
        usage.append(response["metadata"])
        valid_ids = {str(item["segment_id"]) for item in batch if str(item["segment_id"]) in auto_by_id}
        for correction in response["content"]["corrections"]:
            if not isinstance(correction, dict):
                continue
            segment_id = str(correction.get("segment_id") or "")
            source = str(correction.get("source") or "auto").lower()
            if segment_id not in valid_ids or source != "auto":
                continue
            selected_auto.add(segment_id)
            decisions.append({
                "segment_id": segment_id,
                "source": "auto",
                "reason": str(correction.get("reason") or "")[:240],
                "batch": index,
            })

    final: list[dict[str, Any]] = []
    for item in zh:
        segment_id = str(item["segment_id"])
        source = "auto" if segment_id in selected_auto and segment_id in auto_by_id else "zh"
        chosen = auto_by_id[segment_id] if source == "auto" else item
        value = dict(item)
        value["text"] = str(chosen.get("text") or "").strip()
        value["language"] = str(chosen.get("language") or source)
        value["transcript_source"] = f"sensevoice-{source}"
        final.append(value)
    final.sort(key=lambda item: (int(item["start_ms"]), int(item["agora_uid"]), int(item["end_ms"])))
    write_jsonl(session_dir / "transcript.jsonl", final)
    render_transcript_markdown(session_dir, final)
    summary = {
        "session_id": session_dir.name,
        "asr_backend": "sensevoice-small-dual-qwen-reconciled",
        "segments": len(final),
        "source_transcripts": {"zh": "transcript.zh.jsonl", "auto": "transcript.auto.jsonl"},
        "selection_counts": dict(Counter(item["transcript_source"] for item in final)),
        "auto_missing_segment_count": len(missing),
        "qwen_decisions": len(decisions),
        "qwen_metadata": usage,
    }
    write_json(session_dir / "transcript_summary.json", summary)
    write_json(session_dir / "transcript.reconcile.json", {"schema_version": "oopz.transcript.reconcile.v1", "decisions": decisions, "metadata": usage})
    return final, summary
