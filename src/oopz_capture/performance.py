from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GPUSample:
    at: float
    utilization_pct: float
    memory_used_mb: float
    memory_total_mb: float
    power_w: float


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _sample_nvidia() -> GPUSample | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_creation_flags(),
        )
        first = completed.stdout.strip().splitlines()[0]
        values = [float(item.strip()) for item in first.split(",")]
        return GPUSample(time.perf_counter(), values[0], values[1], values[2], values[3])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


class NvidiaSMIMonitor:
    """Low-dependency whole-run GPU sampler for benchmark reporting."""

    def __init__(self, interval_seconds: float = 1.0):
        self.interval_seconds = max(0.25, interval_seconds)
        self.samples: list[GPUSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._stopped_at: float | None = None

    def __enter__(self) -> "NvidiaSMIMonitor":
        self._started_at = time.perf_counter()
        first = _sample_nvidia()
        if first is not None:
            self.samples.append(first)
            self._thread = threading.Thread(target=self._run, name="oopz-gpu-monitor", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            value = _sample_nvidia()
            if value is not None:
                self.samples.append(value)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        final = _sample_nvidia()
        if final is not None:
            self.samples.append(final)
        self._stopped_at = time.perf_counter()

    def summary(self) -> dict[str, Any]:
        stopped = self._stopped_at or time.perf_counter()
        started = self._started_at or stopped
        elapsed = max(0.0, stopped - started)
        if not self.samples:
            return {
                "available": False,
                "sample_count": 0,
                "wall_seconds": round(elapsed, 3),
                "reason": "nvidia-smi unavailable or returned no samples",
            }
        baseline = self.samples[0]
        average_util = sum(item.utilization_pct for item in self.samples) / len(self.samples)
        average_power = sum(item.power_w for item in self.samples) / len(self.samples)
        peak_memory = max(item.memory_used_mb for item in self.samples)
        return {
            "available": True,
            "sample_count": len(self.samples),
            "wall_seconds": round(elapsed, 3),
            "gpu_memory_total_mb": round(baseline.memory_total_mb, 1),
            "baseline_memory_used_mb": round(baseline.memory_used_mb, 1),
            "peak_memory_used_mb": round(peak_memory, 1),
            "peak_memory_delta_mb": round(max(0.0, peak_memory - baseline.memory_used_mb), 1),
            "average_utilization_pct": round(average_util, 2),
            "peak_utilization_pct": round(max(item.utilization_pct for item in self.samples), 2),
            "average_power_w": round(average_power, 2),
            "peak_power_w": round(max(item.power_w for item in self.samples), 2),
            "estimated_energy_wh": round(average_power * elapsed / 3600.0, 4),
            "measurement_scope": "whole NVIDIA GPU; includes other desktop workloads",
        }

