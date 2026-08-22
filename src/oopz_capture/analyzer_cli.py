from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

from .analysis_benchmark import build_comparison
from .analysis_matrix import run_analysis_matrix
from .analysis_pipeline import refresh_analysis_report, run_analysis
from .analysis_routes import BaselineFinalClient, StageRoutedClient
from .analyzer_job import load_analyzer_input, prepare_analysis
from .deepseek_client import (
    DeepSeekClient, DeepSeekConfig, MockDeepSeekClient, opencode_mimo_v25_client,
)
from .ollama_client import OllamaClient, OllamaConfig
from .performance import NvidiaSMIMonitor
from .pdf_reports import render_matrix_reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oopz-analyzer",
        description="OOPZ transcript analysis preparation and DeepSeek adapter diagnostics",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate an analyzer handoff and its Session inputs")
    validate.add_argument("handoff", type=Path)

    prepare = commands.add_parser("prepare", help="idempotently build 300-second and 60-minute windows")
    prepare.add_argument("handoff", type=Path)

    run = commands.add_parser("run", help="generate short summaries, long summaries, and the final report")
    run.add_argument("handoff", type=Path)
    run.add_argument("--mock", action="store_true", help="use deterministic offline model responses")
    run.add_argument(
        "--route", choices=(
            "configured-api", "qwen-hybrid", "qwen-thinking-hybrid",
            "qwen-thinking-local", "mimo-go",
        ),
        default="configured-api",
        help=("configured-api=all stages via ANALYZER_PROVIDER/BASE_URL/MODEL; "
              "experimental paths 2-5: qwen-hybrid=Qwen/cloud/cloud; "
              "qwen-thinking-hybrid=Qwen-thinking/Qwen-thinking/DeepSeek; "
              "qwen-thinking-local=all Qwen thinking; "
              "mimo-go=compatibility route fixed to OpenCode Go MiMo-V2.5"),
    )
    run.add_argument("--variant", help="isolated output name; qwen-hybrid defaults to qwen3-8b-hybrid")

    api_check = commands.add_parser("api-check", help="check structured JSON adapter behavior")
    api_check.add_argument("--mock", action="store_true", help="use deterministic offline mock without API credentials")
    api_check.add_argument("--thinking", choices=("enabled", "disabled"), default="enabled")
    api_check.add_argument("--max-tokens", type=int, help="optional output budget for the diagnostic request")

    local_check = commands.add_parser("local-check", help="check local Ollama/Qwen structured JSON")
    local_check.add_argument("--thinking", action="store_true", help="check bounded local thinking mode")

    benchmark = commands.add_parser("benchmark", help="run Qwen hybrid and compare it with an existing DeepSeek result")
    benchmark.add_argument("handoff", type=Path)
    benchmark.add_argument("--variant")
    benchmark.add_argument("--baseline-result", type=Path, help="defaults to <SESSION>/analysis/result.json")
    benchmark.add_argument(
        "--reuse-baseline-final", action="store_true",
        help="benchmark local window stages without a new DeepSeek call; final accuracy is not measured",
    )
    benchmark.add_argument(
        "--local-thinking", action="store_true",
        help="enable bounded local Qwen thinking for 300-second and 60-minute summaries",
    )
    benchmark.add_argument(
        "--local-final-thinking", action="store_true",
        help="also generate the final overview with bounded local Qwen thinking; requires --local-thinking",
    )
    matrix = commands.add_parser(
        "matrix", help="run three shared three-stage routes (paths 2-4) with exact-input deduplication"
    )
    matrix.add_argument("handoff", type=Path)
    matrix.add_argument("--qwen-thinking-timeout-seconds", type=float, default=600.0)
    matrix.add_argument("--qwen-short-max-tokens", type=int, default=8192)
    matrix.add_argument("--qwen-long-max-tokens", type=int, default=12288)
    matrix.add_argument("--qwen-final-max-tokens", type=int, default=16384)
    matrix.add_argument(
        "--require-official-deepseek", action="store_true",
        help="abort unless the cloud client uses https://api.deepseek.com and provider=deepseek",
    )

    pdf = commands.add_parser("pdf", help="compile existing matrix Markdown reports into PDFs")
    pdf.add_argument("session", type=Path)
    return parser


def _deepseek() -> DeepSeekClient:
    return DeepSeekClient(DeepSeekConfig.from_env())


def _require_official_deepseek(client: DeepSeekClient) -> None:
    parsed = urlparse(client.config.base_url)
    if (
        client.config.provider != "deepseek"
        or parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
    ):
        raise ValueError(
            "official DeepSeek was required, but the configured provider/endpoint is "
            f"{client.config.provider} / {client.config.base_url}"
        )


def _hybrid(*, local_thinking: bool = False, local_final_thinking: bool = False) -> StageRoutedClient:
    local = OllamaClient(OllamaConfig.from_env())
    final = local if local_final_thinking else _deepseek()
    return StageRoutedClient(
        local, final, local_thinking=local_thinking, local_final_thinking=local_final_thinking
    )


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        if args.command == "validate":
            value = load_analyzer_input(args.handoff)
            print(json.dumps({
                "status": "valid",
                "Request ID": value.request_id,
                "Session ID": value.session_id,
                "segments": len(value.transcript),
                "short_summary_seconds": value.short_summary_seconds,
                "long_summary_seconds": value.long_summary_seconds,
                "input_fingerprint": value.fingerprint,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "prepare":
            result = prepare_analysis(args.handoff)
            print(json.dumps({
                "status": "windows_ready",
                "Session ID": result["job"]["session_id"],
                "reused": result["reused"],
                "short_window_count": result["windows"]["short_window_count"],
                "long_window_count": result["windows"]["long_window_count"],
                "windows_json": str(Path(result["session_dir"]) / "analysis" / "windows.json"),
                "windows_markdown": str(Path(result["session_dir"]) / "analysis" / "windows.md"),
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            local_thinking = args.route in {"qwen-thinking-hybrid", "qwen-thinking-local"}
            local_final_thinking = args.route == "qwen-thinking-local"
            if args.mock:
                client = MockDeepSeekClient()
            elif args.route == "configured-api":
                client = _deepseek()
            elif args.route == "mimo-go":
                client = opencode_mimo_v25_client()
            else:
                client = _hybrid(
                    local_thinking=local_thinking, local_final_thinking=local_final_thinking
                )
            variant = args.variant or (
                "configured-api" if args.route == "configured-api" else
                ("mimo-v2.5-opencode-go" if args.route == "mimo-go" else
                ("qwen3-8b-thinking-local" if local_final_thinking else
                 ("qwen3-8b-thinking-hybrid" if local_thinking else "qwen3-8b-hybrid")))
            )
            output = run_analysis(args.handoff, client, variant=variant, render_pdf=True)
            print(json.dumps({
                "status": output["result"]["status"],
                "Session ID": output["result"]["session_id"],
                "Report ID": output["result"]["report_id"],
                "route": args.route,
                "variant": variant,
                "reused": output["reused"],
                "short_summary_count": len(output["result"]["short_summaries"]),
                "long_summary_count": len(output["result"]["long_summaries"]),
                "analysis_policy": output["result"].get("analysis_policy", {}),
                "token_usage_by_stage": output["result"]["model"].get("usage_by_stage", {}),
                "cost_estimate": output["result"]["model"].get("cost_estimate", {}),
                "human_report": str(output["report_path"]),
                "human_report_pdf": str(output["pdf_path"]) if output.get("pdf_path") else None,
                "qq_messages": str(output["qq_path"]),
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "api-check":
            client = MockDeepSeekClient() if args.mock else _deepseek()
            response = client.complete_json(
                system_prompt="Return one JSON object only.",
                user_prompt='Return JSON matching {"status":"ok","items":[]}.',
                required_keys={"status": str, "items": list},
                thinking=args.thinking,
                reasoning_effort="high" if args.thinking == "enabled" else None,
                max_tokens=args.max_tokens,
            )
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0
        if args.command == "local-check":
            thinking = "enabled" if args.thinking else "disabled"
            response = OllamaClient(OllamaConfig.from_env()).complete_json(
                system_prompt="你是本地接口诊断器。只输出JSON。",
                user_prompt='返回JSON：{"status":"ok","items":[]}。',
                required_keys={"status": str, "items": list},
                thinking=thinking,
                reasoning_effort="low" if args.thinking else None,
                max_tokens=1024 if args.thinking else 256,
            )
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0
        if args.command == "benchmark":
            if args.local_final_thinking and not args.local_thinking:
                raise ValueError("--local-final-thinking requires --local-thinking")
            if args.local_final_thinking and args.reuse_baseline_final:
                raise ValueError("--local-final-thinking cannot be combined with --reuse-baseline-final")
            value = load_analyzer_input(args.handoff)
            baseline = args.baseline_result or value.session_dir / "analysis" / "result.json"
            if not baseline.is_file():
                raise FileNotFoundError(f"DeepSeek baseline result not found: {baseline}")
            variant = args.variant or (
                ("qwen3-8b-thinking-local" if args.local_final_thinking else
                 ("qwen3-8b-thinking-hybrid" if args.local_thinking else "qwen3-8b-hybrid"))
            )
            client = (
                StageRoutedClient(
                    OllamaClient(OllamaConfig.from_env()),
                    BaselineFinalClient(baseline),
                    local_thinking=args.local_thinking,
                )
                if args.reuse_baseline_final else _hybrid(
                    local_thinking=args.local_thinking,
                    local_final_thinking=args.local_final_thinking,
                )
            )
            with NvidiaSMIMonitor() as monitor:
                output = run_analysis(args.handoff, client, variant=variant, render_pdf=False)
            gpu = monitor.summary()
            gpu["measurement_valid"] = not output["reused"]
            if output["reused"]:
                gpu["measurement_note"] = "candidate result was reused; measured time does not represent model inference"
            output = refresh_analysis_report(
                output,
                value,
                runtime_metrics={"gpu": gpu},
                render_pdf=True,
            )
            comparison = build_comparison(
                args.handoff,
                baseline,
                output["result_path"],
                gpu,
                output["result_path"].parent / "benchmark",
                final_reused_from_baseline=args.reuse_baseline_final,
            )
            print(json.dumps({
                "status": "completed",
                "Session ID": value.session_id,
                "baseline_result": str(baseline),
                "candidate_result": str(output["result_path"]),
                "candidate_reused": output["reused"],
                "final_reused_from_baseline": args.reuse_baseline_final,
                "local_thinking": args.local_thinking,
                "local_final_thinking": args.local_final_thinking,
                "variant": variant,
                "comparison_json": str(comparison["json_path"]),
                "comparison_markdown": str(comparison["markdown_path"]),
                "candidate_gpu": gpu,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "matrix":
            if not 5 <= args.qwen_thinking_timeout_seconds <= 1800:
                raise ValueError("--qwen-thinking-timeout-seconds must be 5 to 1800")
            for name in ("qwen_short_max_tokens", "qwen_long_max_tokens", "qwen_final_max_tokens"):
                value = int(getattr(args, name))
                if not 256 <= value <= 32768:
                    raise ValueError(f"--{name.replace('_', '-')} must be 256 to 32768")
            local_config = replace(
                OllamaConfig.from_env(),
                thinking_timeout_seconds=args.qwen_thinking_timeout_seconds,
                thinking_short_max_tokens=args.qwen_short_max_tokens,
                thinking_long_max_tokens=args.qwen_long_max_tokens,
                thinking_final_max_tokens=args.qwen_final_max_tokens,
            )
            deepseek_client = _deepseek()
            if args.require_official_deepseek:
                _require_official_deepseek(deepseek_client)
            output = run_analysis_matrix(
                args.handoff,
                deepseek_client,
                OllamaClient(local_config),
                render_pdf=True,
            )
            manifest = output["manifest"]
            print(json.dumps({
                "status": "completed",
                "Session ID": manifest["session_id"],
                "routes": [
                    {
                        "route_id": item["route_id"],
                        "Report ID": item["report_id"],
                        "human_report": item["report_path"],
                        "reused": item["reused"],
                    }
                    for item in manifest["routes"]
                ],
                "shared_resources": manifest["shared_resources"],
                "matrix_manifest": str(output["manifest_path"]),
                "human_review": str(output["review_path"]),
                "human_review_pdf": str(output["review_pdf"]) if output.get("review_pdf") else None,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "pdf":
            rendered = render_matrix_reports(args.session)
            print(json.dumps({
                "status": "completed",
                "Session ID": args.session.resolve().name,
                "pdfs": [str(path) for path in rendered],
            }, ensure_ascii=False, indent=2))
            return 0
        raise AssertionError(args.command)
    except Exception as error:
        logging.getLogger(__name__).exception("analyzer command failed")
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
