from __future__ import annotations

import math
import wave
from pathlib import Path


def read_mono_pcm16(path: Path):
    """Return (sample_rate, float32 mono samples in [-1, 1])."""
    import numpy as np
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError(f"expected mono PCM16 WAV: {path}")
        sample_rate = stream.getframerate()
        raw = stream.readframes(stream.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return sample_rate, samples


def resample_audio(samples, source_rate: int, target_rate: int = 16000):
    import numpy as np
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    values = np.asarray(samples, dtype=np.float32)
    if source_rate == target_rate:
        return values.copy()
    from scipy.signal import resample_poly
    divisor = math.gcd(source_rate, target_rate)
    output = resample_poly(values, target_rate // divisor, source_rate // divisor)
    return np.asarray(output, dtype=np.float32)
