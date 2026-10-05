"""Model backend: the Qoder CN CLI run headless with every tool disabled (a plain text completion)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class BackendError(RuntimeError):
    """The CLI failed or returned nothing usable (after retries)."""


@dataclass(frozen=True)
class QoderCli:
    command: str                 # path to qoderclicn
    home: str                    # HOME of the CLI (holds its login); must be readable by the service
    model: str = "Qwen3.8-Flash"
    timeout: float = 600.0
    attempts: int = 3
    node_dir: str = ""           # directory containing `node` when it is not on PATH

    @classmethod
    def from_env(cls, env=os.environ) -> "QoderCli":
        for name in ("OOPZ_ANALYZER_CLI", "OOPZ_ANALYZER_HOME"):
            if not env.get(name):
                raise BackendError(f"{name} is not set")
        node = env.get("OOPZ_NODE_PATH", "")
        return cls(env["OOPZ_ANALYZER_CLI"], env["OOPZ_ANALYZER_HOME"],
                   env.get("OOPZ_ANALYZER_MODEL") or cls.model,
                   float(env.get("OOPZ_ANALYZER_TIMEOUT_SECONDS") or cls.timeout),
                   node_dir=str(Path(node).parent) if node else "")

    def complete(self, system: str, user: str) -> str:
        return self.complete_with_usage(system, user)[0]

    def complete_with_usage(self, system: str, user: str) -> tuple[str, dict]:
        """One model reply as text plus what the CLI reported about it; transient failures are retried,
        the last failure is raised.  ``usage["cli_runs"]`` counts every CLI run, failed ones included."""
        environment = dict(os.environ, HOME=self.home)
        if self.node_dir:
            environment["PATH"] = self.node_dir + os.pathsep + environment.get("PATH", "")
        command = [self.command, "-p", "--tools", "", "--no-session-persistence", "--output-format", "json",
                   "-m", self.model, "--system-prompt", system]
        for attempt in range(1, self.attempts + 1):
            try:
                text, usage = self._once(command, user, environment)
                return text, usage | {"cli_runs": attempt}
            except BackendError:
                if attempt == self.attempts:
                    raise
                time.sleep(3 * attempt)
        raise BackendError("no attempt was made")

    def _once(self, command: list[str], user: str, environment: dict) -> tuple[str, dict]:
        try:
            done = subprocess.run(command, input=user, capture_output=True, text=True, encoding="utf-8",
                                  env=environment, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise BackendError(f"timed out after {self.timeout:.0f}s") from None
        if done.returncode != 0:
            raise BackendError(f"exit {done.returncode}: {done.stderr.strip()[:300]}")
        try:
            envelope = json.loads(done.stdout)
        except ValueError:
            raise BackendError("output is not the expected JSON envelope") from None
        text = envelope.get("result")
        if envelope.get("is_error") or not isinstance(text, str) or not text.strip():
            raise BackendError(f"model returned no result (is_error={envelope.get('is_error')})")
        return text, usage_of(envelope)


def usage_of(envelope: dict) -> dict:
    """The accounting fields of one CLI result.  The free model reports zero tokens, so the share of the
    context window (``context_ratio``) is the only size signal; everything is kept as the CLI gave it."""
    raw = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}

    def number(value) -> float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    return {"cli_ms": int(number(envelope.get("duration_ms"))), "api_ms": int(number(envelope.get("duration_api_ms"))),
            "turns": int(number(envelope.get("num_turns"))),
            "input_tokens": int(number(raw.get("input_tokens"))), "output_tokens": int(number(raw.get("output_tokens"))),
            "cache_read_tokens": int(number(raw.get("cache_read_input_tokens"))),
            "cache_creation_tokens": int(number(raw.get("cache_creation_input_tokens"))),
            "context_ratio": number(raw.get("context_usage_ratio")),
            "cost_usd": number(envelope.get("total_cost_usd")), "credits": number(envelope.get("total_credits"))}


def parse_json_object(text: str) -> dict:
    """The first JSON object in the model's reply (tolerates a code fence or a leading sentence)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("json:no_object_in_reply")
    value, _ = json.JSONDecoder().raw_decode(text, start)       # whatever follows the first object is ignored
    if not isinstance(value, dict):
        raise ValueError("json:not_an_object")
    return value
