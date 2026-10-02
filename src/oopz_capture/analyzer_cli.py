from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from .analysis_pipeline import run_analysis
from .analyzer_job import load_analyzer_input, prepare_analysis
from .deepseek_client import (
    DeepSeekClient, DeepSeekConfig, MockDeepSeekClient, opencode_mimo_v25_client,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oopz-analyzer",
        description="OOPZ transcript analysis preparation and compatible analysis API diagnostics",
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
        "--route", choices=("configured-api", "mimo-go"),
        default="configured-api",
        help=("configured-api=all stages via ANALYZER_PROVIDER/BASE_URL/MODEL; "
              "mimo-go=compatibility route fixed to OpenCode Go MiMo-V2.5"),
    )
    run.add_argument("--variant", help="isolated output name")

    api_check = commands.add_parser("api-check", help="check structured JSON adapter behavior")
    api_check.add_argument("--mock", action="store_true", help="use deterministic offline mock without API credentials")
    api_check.add_argument("--thinking", choices=("enabled", "disabled"), default="enabled")
    api_check.add_argument("--max-tokens", type=int, help="optional output budget for the diagnostic request")

    return parser


def _analysis_api() -> DeepSeekClient:
    return DeepSeekClient(DeepSeekConfig.from_env())


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
            if args.mock:
                client = MockDeepSeekClient()
            elif args.route == "configured-api":
                client = _analysis_api()
            else:
                client = opencode_mimo_v25_client()
            variant = args.variant or (
                "configured-api" if args.route == "configured-api" else
                "mimo-v2.5-opencode-go"
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
                "report_messages": str(output["report_messages_path"]),
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "api-check":
            client = MockDeepSeekClient() if args.mock else _analysis_api()
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
        raise AssertionError(args.command)
    except Exception as error:
        logging.getLogger(__name__).exception("analyzer command failed")
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
