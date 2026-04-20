"""Core alignment logic — cross-correlation with polarity detection."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate, hilbert


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
    # 0-1 strength score derived from peak prominence over noise floor.
    confidence: float
    # Raw peak correlation in [-1, 1]; bare height, not prominence.
    peak_corr: float
    # Peak divided by median |corr| across the search window. A proper
    # "how much does the peak stand out" measure. > ~5 is a confident lock.
    peak_over_median: float
    # Envelope-based coarse lag, in samples. If this disagrees strongly with
    # `lag_samples`, the fine correlator may be fooled by a sidelobe.
    envelope_lag_samples: int
    sanity_ok: bool
    sample_rate: int
    close_mic: str
    delay_samples: int
    delay_ms: float


def probe_channels(input_path: str) -> int:
    """Return the original channel count of an audio file via ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=channels",
        "-of",
        "default=nw=1:nk=1",
        input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


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
            "1",
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


def _normalize(x: np.ndarray) -> np.ndarray:
    x = x - np.mean(x)
    s = np.std(x)
    return x / (s + 1e-12)


def peak_series(audio: np.ndarray, n: int = 400) -> list[float]:
    """Downsample audio to `n` normalized peak values for waveform display."""
    if len(audio) == 0:
        return [0.0] * n
    buckets = np.array_split(audio, n)
    peaks = np.array([float(np.max(np.abs(b))) if b.size else 0.0 for b in buckets])
    m = peaks.max()
    if m > 0:
        peaks = peaks / m
    return [round(float(v), 4) for v in peaks]


def analysis_window(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Return the centered ≤30 s window used by `align()`, for visualization."""
    n = len(audio)
    win_len = min(n, 30 * sample_rate)
    start = max(0, (n - win_len) // 2)
    return audio[start : start + win_len]


def align(
    audio_a: np.ndarray,
    audio_b: np.ndarray,
    sample_rate: int,
    max_ms: float = 50.0,
    label_a: str = "a",
    label_b: str = "b",
    on_phase=None,
) -> AlignmentResult:
    """Find the optimal lag and polarity to align audio_b with audio_a.

    The algorithm works out which file is the close mic automatically from
    the sign of the lag: whichever signal arrived earlier is the close mic,
    since spot mics sit nearer the source than the reference pair.

    `on_phase`, if supplied, is a callable that receives a phase name
    ("check" or "verify") just before that phase runs — used by the
    streaming API handler to push progress events to the client.
    """
    def phase(name: str) -> None:
        if on_phase is not None:
            on_phase(name)

    max_lag = int(max_ms * 1e-3 * sample_rate)

    n = min(len(audio_a), len(audio_b))
    analysis_len = min(n, 30 * sample_rate)
    start = max(0, (n - analysis_len) // 2)
    a = audio_a[start : start + analysis_len]
    b = audio_b[start : start + analysis_len]

    a_norm = _normalize(a)
    b_norm = _normalize(b)

    phase("check")
    corr = correlate(a_norm, b_norm, mode="full", method="fft")
    corr /= len(a_norm)

    zero_lag_idx = len(b_norm) - 1
    lo = max(0, zero_lag_idx - max_lag)
    hi = min(len(corr), zero_lag_idx + max_lag + 1)
    window = corr[lo:hi]
    lags = np.arange(lo, hi) - zero_lag_idx

    peak_idx = int(np.argmax(np.abs(window)))
    peak_val = float(window[peak_idx])
    best_lag = int(lags[peak_idx])
    invert = peak_val < 0

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

    # Peak prominence over the noise floor of the correlation function.
    # Median of |corr| across the search window is robust to the main lobe
    # (which occupies only a few samples of a window that's thousands wide)
    # and to any coherent sidelobes.
    abs_window = np.abs(window)
    median_floor = float(np.median(abs_window))
    peak_over_median = abs(peak_val) / (median_floor + 1e-12)

    # Map prominence to a 0-1 strength. peak/median ≈ 1 → 0, ≈ 10 → 0.9.
    confidence = 1.0 - 1.0 / max(peak_over_median, 1.0)

    phase("verify")
    # Envelope sanity check: compute a Hilbert-envelope coarse alignment.
    # It ignores phase detail so it can't lock onto pitch-period sidelobes.
    # If it disagrees with the fine correlator by more than 5 ms, something
    # is off — flag low confidence.
    env_a = np.abs(hilbert(a))
    env_b = np.abs(hilbert(b))
    env_a = _normalize(env_a)
    env_b = _normalize(env_b)
    env_corr = correlate(env_a, env_b, mode="full", method="fft") / len(env_a)
    env_window = env_corr[lo:hi]
    env_peak_idx = int(np.argmax(np.abs(env_window)))
    env_lag = int(lags[env_peak_idx])

    disagreement_ms = abs(env_lag - best_lag) * 1000 / sample_rate
    sanity_ok = disagreement_ms <= 5.0

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
        peak_corr=peak_val,
        peak_over_median=peak_over_median,
        envelope_lag_samples=env_lag,
        sanity_ok=sanity_ok,
        sample_rate=sample_rate,
        close_mic=close_mic,
        delay_samples=delay_samples,
        delay_ms=delay_ms,
    )
