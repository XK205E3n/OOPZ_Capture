from __future__ import annotations

import struct
import tempfile
import unittest
import wave
from pathlib import Path

from oopz_capture.recorder import WavTrackWriter


class RecorderGenerationTests(unittest.TestCase):
    def test_continuous_generation_ignores_callback_timer_jitter(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            writer = WavTrackWriter(Path(folder), "123", 1000)
            pcm = struct.pack("<100h", *([1000] * 100))
            writer.append(pcm, session_offset_ms=100, frame_count=100, generation=1)
            writer.append(pcm, session_offset_ms=240, frame_count=100, generation=1)
            result = writer.close()
            self.assertEqual(result["captured_frames"], 200)
            self.assertEqual(result["silence_frames"], 0)

    def test_new_generation_inserts_real_timeline_gap(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            writer = WavTrackWriter(Path(folder), "123", 1000)
            pcm = struct.pack("<100h", *([1000] * 100))
            writer.append(pcm, session_offset_ms=100, frame_count=100, generation=1)
            writer.append(pcm, session_offset_ms=500, frame_count=100, generation=2)
            result = writer.close()
            self.assertEqual(result["silence_frames"], 300)
            with wave.open(str(Path(folder) / "123.wav"), "rb") as stream:
                self.assertEqual(stream.getnframes(), 500)


if __name__ == "__main__":
    unittest.main()
