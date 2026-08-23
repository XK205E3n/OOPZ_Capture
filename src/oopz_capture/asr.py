from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ASRResult:
    text: str
    language: str | None = None
    confidence: float | None = None
    raw: dict[str, Any] | None = None


class ASRBackend(ABC):
    name: str

    @abstractmethod
    def transcribe(self, samples, sample_rate: int, *, language: str | None = None) -> ASRResult:
        raise NotImplementedError


_RICH_TAG = re.compile(r"<\|[^|>]+\|>")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SENSEVOICE_MODEL_PATH = PROJECT_ROOT / "models" / "SenseVoiceSmall"


def resolve_sensevoice_model() -> Path:
    path = SENSEVOICE_MODEL_PATH.resolve()
    if not path.joinpath("model.pt").is_file():
        raise RuntimeError(f"SenseVoice model must be installed at {path}")
    return path


def clean_sensevoice_text(value: str) -> str:
    return _RICH_TAG.sub("", str(value or "")).strip()


class SenseVoiceBackend(ASRBackend):
    name = "sensevoice-small"

    def __init__(self, *, device: str = "cpu"):
        try:
            from funasr import AutoModel
        except ModuleNotFoundError as error:
            raise RuntimeError("FunASR is not installed. Run: pip install -e \".[speech]\"") from error
        model_path = str(resolve_sensevoice_model())
        self._model = AutoModel(
            model=model_path,
            trust_remote_code=False,
            device=device,
            disable_update=True,
        )

    def transcribe(self, samples, sample_rate: int, *, language: str | None = None) -> ASRResult:
        if sample_rate != 16000:
            raise ValueError("SenseVoice input must be 16000 Hz")
        values = self._model.generate(
            input=samples, cache={}, language=language or "auto", use_itn=True,
            batch_size_s=60, disable_pbar=True,
        )
        item = values[0] if values else {}
        raw_text = str(item.get("text") or "") if isinstance(item, dict) else str(item)
        detected = None
        match = re.search(r"<\|(zh|en|yue|ja|ko|nospeech)\|>", raw_text)
        if match:
            detected = match.group(1)
        return ASRResult(
            text=clean_sensevoice_text(raw_text),
            language=detected or language,
            raw=item if isinstance(item, dict) else {"value": str(item)},
        )
