from __future__ import annotations

import re
import os
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


def resolve_sensevoice_model(model: str | None = None) -> Path:
    explicit = model or os.environ.get("OOPZ_SENSEVOICE_MODEL")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.joinpath("model.pt").is_file():
            raise RuntimeError(f"SenseVoice model is missing at {path}")
        return path
    module_path = Path(__file__).resolve()
    candidates = [Path.cwd() / "models" / "SenseVoiceSmall"]
    candidates.extend(parent / "models" / "SenseVoiceSmall" for parent in module_path.parents)
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.joinpath("model.pt").is_file():
            return resolved
    checked = ", ".join(str(path) for path in seen)
    raise RuntimeError(
        "SenseVoice model was not found. Set OOPZ_SENSEVOICE_MODEL to the directory "
        f"containing model.pt. Checked: {checked}"
    )


def clean_sensevoice_text(value: str) -> str:
    return _RICH_TAG.sub("", str(value or "")).strip()


class SenseVoiceBackend(ASRBackend):
    name = "sensevoice-small"

    def __init__(self, *, device: str = "cpu", model: str | None = None):
        try:
            from funasr import AutoModel
        except ModuleNotFoundError as error:
            raise RuntimeError("FunASR is not installed. Run: pip install -e \".[speech]\"") from error
        model_path = str(resolve_sensevoice_model(model))
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
