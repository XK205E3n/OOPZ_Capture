from __future__ import annotations

from pathlib import Path

from .asr import SenseVoiceBackend
from .transcript import AUTO_TRANSCRIPT_LANGUAGES, render_transcript_markdown, transcribe_session
from .vad import SileroVADBackend, VADConfig, run_vad_session


def run_speech_pipeline(
    session_dir: Path,
    *,
    vad_config: VADConfig,
    device: str = "cpu",
    language: str | None = None,
) -> tuple[int, int, Path]:
    """Run one SenseVoice transcript; auto mode keeps Chinese/Cantonese/English only."""
    vad_segments = run_vad_session(session_dir, SileroVADBackend(), vad_config)
    backend = SenseVoiceBackend(device=device)
    is_auto = language is None
    transcript = transcribe_session(
        session_dir,
        backend,
        language=language,
        allowed_languages=AUTO_TRANSCRIPT_LANGUAGES if is_auto else None,
    )
    render_transcript_markdown(session_dir, transcript)
    return len(vad_segments), len(transcript), session_dir / "transcript.md"
