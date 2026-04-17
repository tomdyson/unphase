# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Local dev (requires `ffmpeg` on PATH, Python 3.11+, and `uv`):

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Deploy:

```bash
fly deploy         # app name is "unphase", deployed at https://unphase.fly.dev
```

There is no test suite, linter config, or build step.

## Architecture

Two-file Python app plus a single HTML page. The split is meaningful:

- **`app/align.py`** — pure signal processing, no web concerns. `decode_to_mono()` shells out to `ffmpeg` to convert any input to 48 kHz mono float via a temp WAV. `align()` does FFT cross-correlation on a centered ≤30s window of each signal, normalized so the peak is a proper correlation coefficient in [-1, 1]. Parabolic interpolation around the peak gives sub-sample accuracy.
- **`app/main.py`** — FastAPI wrapper. Streams uploads to temp files with a 200 MB cap, enforces ≥1s of audio, calls `align()`, cleans up temp files in `finally`.
- **`app/static/index.html`** — single-page frontend, Alpine.js + Tailwind loaded from CDN (no build step, no Node). The Alpine data function is named `unphase()` and is referenced as `x-data="unphase()"` on the body.

### Sign convention (important)

`scipy.signal.correlate(a, b)` returns lag of `b` relative to `a`:
- `lag > 0` → `b` arrived earlier → **b is the close mic**
- `lag < 0` → `a` arrived earlier → **a is the close mic**

The API returns the absolute `delay_samples` to apply to the close mic (always non-negative) plus the close mic's label. `lag_samples` is the raw signed value for debugging. Don't "fix" the sign without re-verifying against this convention — the comment block at the top of `AlignmentResult` is authoritative.

### Deployment shape

Single-container Dockerfile on Fly.io in LHR. `fly.toml` sets `auto_stop_machines = "stop"` with `min_machines_running = 0`, so the app cold-starts on first request and costs nothing when idle. The VM is 1 GB / shared-cpu-1x — fine for the default analysis window but worth remembering that two 200 MB uploads decoded to float64 can eat memory under concurrency (`soft_limit = 15`).

Health check is `GET /healthz`; Fly hits it every 30s.
