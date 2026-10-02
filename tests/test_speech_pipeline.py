from __future__ import annotations

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from oopz_capture.asr import ASRBackend, ASRResult, clean_sensevoice_text
from oopz_capture.output import write_json
from oopz_capture.transcript import AUTO_TRANSCRIPT_LANGUAGES, render_transcript_markdown, transcribe_session
from oopz_capture.vad import VADConfig, run_vad_session


class FakeVAD:
    name = "fake-vad"

    def detect(self, samples, sample_rate: int, config: VADConfig):
        self.length = len(samples)
        return [{"start": 1600, "end": 8000}]


class FakeASR(ASRBackend):
    name = "fake-asr"

    def transcribe(self, samples, sample_rate: int, *, language: str | None = None):
        return ASRResult(text="测试文本", language="zh", confidence=0.9)


class ForeignLanguageASR(ASRBackend):
    name = "fake-asr"

    def transcribe(self, samples, sample_rate: int, *, language: str | None = None):
        return ASRResult(text="は", language="ja", confidence=0.9)


def _wav(path: Path, rate: int = 48000, seconds: int = 1) -> None:
    samples = [1000] * (rate * seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(rate)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


class SpeechPipelineTests(unittest.TestCase):
    def test_vad_preserves_session_clock_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder)
            _wav(session / "audio" / "123.wav")
            write_json(session / "session.json", {"session_id": "s1", "started_at": "2026-08-13T00:00:00+00:00"})
            write_json(session / "users.json", [{"agora_uid": 123, "oopz_uid": "oopz-a", "nickname": "小王"}])
            segments = run_vad_session(session, FakeVAD(), VADConfig())
            self.assertEqual((segments[0]["start_ms"], segments[0]["end_ms"]), (100, 500))
            self.assertEqual((segments[0]["audio_start_sample"], segments[0]["audio_end_sample"]), (4800, 24000))
            self.assertEqual(segments[0]["oopz_uid"], "oopz-a")

    def test_transcript_jsonl_and_markdown_are_chronological_and_label_ids(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder)
            _wav(session / "audio" / "123.wav")
            write_json(session / "session.json", {"session_id": "s1", "started_at": "2026-08-13T00:00:00+00:00"})
            write_json(session / "users.json", [{"agora_uid": 123, "oopz_uid": "oopz-a", "nickname": "小王"}])
            run_vad_session(session, FakeVAD(), VADConfig())
            values = transcribe_session(session, FakeASR(), language="zh")
            self.assertEqual(values[0]["text"], "测试文本")
            self.assertEqual(values[0]["start_time"], "2026-08-13T00:00:00.100+00:00")
            path = render_transcript_markdown(session, values)
            rendered = path.read_text(encoding="utf-8")
            self.assertIn("小王", rendered)
            self.assertIn("OOPZ UID=oopz-a", rendered)
            self.assertIn("Agora UID=123", rendered)
            record = json.loads((session / "transcript.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["asr_backend"], "fake-asr")

    def test_sensevoice_rich_tags_are_removed(self) -> None:
        self.assertEqual(clean_sensevoice_text("<|zh|><|NEUTRAL|><|Speech|>你好"), "你好")

    def test_auto_transcript_filters_non_chinese_non_english_detection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder)
            _wav(session / "audio" / "123.wav")
            write_json(session / "session.json", {"session_id": "s1", "started_at": "2026-08-13T00:00:00+00:00"})
            write_json(session / "users.json", [{"agora_uid": 123, "oopz_uid": "oopz-a", "nickname": "小王"}])
            run_vad_session(session, FakeVAD(), VADConfig())
            values = transcribe_session(
                session, ForeignLanguageASR(), language=None,
                allowed_languages=AUTO_TRANSCRIPT_LANGUAGES,
            )
            self.assertEqual(values, [])
            summary = json.loads((session / "transcript_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["allowed_languages"], ["en", "yue", "zh"])


if __name__ == "__main__":
    unittest.main()
