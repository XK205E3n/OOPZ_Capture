from __future__ import annotations

import base64
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from oopz_capture.readable import identity_label, readable_nickname
from oopz_capture.recorder import CaptureRecorder


def _pcm(amplitude: int, frames: int) -> bytes:
    return struct.pack(f"<{frames}h", *([amplitude] * frames))


class RecorderTests(unittest.TestCase):
    def test_separate_track_wav_and_gap_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder)
            recorder = CaptureRecorder(session)
            pcm = _pcm(1000, 100)
            for uid, offset in [("101", 100.0), ("202", 200.0)]:
                recorder.ingest({
                    "uid": uid,
                    "sampleRate": 1000,
                    "frameCount": 100,
                    "generation": 1,
                    "sessionOffsetMs": offset,
                    "pcm16Base64": base64.b64encode(pcm).decode("ascii"),
                })
            manifest = recorder.close()
            self.assertEqual(len(manifest), 2)
            with wave.open(str(session / "audio" / "101.wav"), "rb") as first:
                self.assertEqual(first.getnframes(), 100)
            with wave.open(str(session / "audio" / "202.wav"), "rb") as second:
                self.assertEqual(second.getnframes(), 200)

    def test_readable_nickname_repairs_or_suppresses_mojibake_and_labels_ids(self) -> None:
        raw = "\u93c4\u71b7\u6473E3"
        value = readable_nickname(raw)
        self.assertNotEqual(value, raw)
        label = identity_label(nickname=raw, oopz_uid="oopz-1", agora_uid=123)
        self.assertIn("OOPZ UID=oopz-1", label)
        self.assertIn("Agora UID=123", label)
        self.assertNotIn(raw, label)


if __name__ == "__main__":
    unittest.main()
