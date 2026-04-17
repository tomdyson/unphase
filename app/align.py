"""Core alignment logic — cross-correlation with polarity detection."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate


@dataclass
class AlignmentResult:
    """Result of aligning two signals.

    Sign convention: `lag_samples` is the lag of signal_b relative to signal_a
    as returned by scipy.signal.correlate(a, b):
      lag > 0  → b leads a  (b arrived earlier → b is the close mic)
      lag < 0  → b lags a   (a arrived earlier → a is the close mic)
    """

    lag_samples: int
    sub_sample_lag: float
    invert_polarity: bool
    confidence: float
    sample_rate: int
    # Which input is the close mic: "a", "b", or "aligned"
    close_mic: str
    # How much to delay the close mic, in samples and ms (always positive)
    delay_samples: int
    delay_ms: float


def decode_to_mono(input_path: str, target_sr: int) -> np.ndarray:
    """Use ffmpeg to decode any audio file to mono float64 at target_sr."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            input_path,
            "-ac",
            "1",  # downmix to mono
            "-ar",
            str(target_sr),
            "-c:a",
            "pcm_f32le",
            tmp_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr}")
        sr, data = wavfile.read(tmp_path)
        assert sr == target_sr
        return data.astype(np.float64)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def align(
    audio_a: np.ndarray,
    audio_b: np.ndarray,
    sample_rate: int,
    max_ms: float = 50.0,
    label_a: str = "a",
    label_b: str = "b",
) -> AlignmentResult:
    """Find the optimal lag and polarity to align audio_b with audio_a.

    The algorithm works out which file is the close mic automatically from
    the sign of the lag: whichever signal arrived earlier is the close mic,
    since spot mics sit nearer the source than the reference pair.
    """
    max_lag = int(max_ms * 1e-3 * sample_rate)

    # Use a centered window up to 30 seconds for the correlation.
    n = min(len(audio_a), len(audio_b))
    analysis_len = min(n, 30 * sample_rate)
    start = max(0, (n - analysis_len) // 2)
    a = audio_a[start : start + analysis_len]
    b = audio_b[start : start + analysis_len]

    # Zero-mean and unit-variance normalize so the correlation peak is a
    # proper correlation coefficient in [-1, 1].
    a = a - np.mean(a)
    b = b - np.mean(b)
    a_norm = a / (np.std(a) + 1e-12)
    b_norm = b / (np.std(b) + 1e-12)

    corr = correlate(a_norm, b_norm, mode="full", method="fft")
    corr /= len(a_norm)

    zero_lag_idx = len(b_norm) - 1
    lo = max(0, zero_lag_idx - max_lag)
    hi = min(len(corr), zero_lag_idx + max_lag + 1)
    window = corr[lo:hi]
    lags = np.arange(lo, hi) - zero_lag_idx

    # Peak of absolute correlation catches polarity-inverted matches.
    peak_idx = int(np.argmax(np.abs(window)))
    peak_val = float(window[peak_idx])
    best_lag = int(lags[peak_idx])
    invert = peak_val < 0
    confidence = abs(peak_val)

    # Parabolic interpolation for sub-sample accuracy.
    if 0 < peak_idx < len(window) - 1:
        y0, y1, y2 = window[peak_idx - 1], window[peak_idx], window[peak_idx + 1]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-12:
            offset = 0.5 * (y0 - y2) / denom
            sub_lag = best_lag + offset
        else:
            sub_lag = float(best_lag)
    else:
        sub_lag = float(best_lag)

    # Decide which input is the close mic from the sign of the lag.
    if best_lag > 0:
        close_mic = label_b
        delay_samples = best_lag
    elif best_lag < 0:
        close_mic = label_a
        delay_samples = -best_lag
    else:
        close_mic = "aligned"
        delay_samples = 0

    delay_ms = delay_samples * 1000 / sample_rate

    return AlignmentResult(
        lag_samples=best_lag,
        sub_sample_lag=sub_lag,
        invert_polarity=invert,
        confidence=confidence,
        sample_rate=sample_rate,
        close_mic=close_mic,
        delay_samples=delay_samples,
        delay_ms=delay_ms,
    )
