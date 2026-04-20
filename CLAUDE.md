# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Local dev (requires `ffmpeg` and `ffprobe` on PATH, Python 3.11+, and `uv`):

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Deploy: push to `main` on GitHub and the `.github/workflows/fly-deploy.yml`
action runs `flyctl deploy --remote-only` using the `FLY_API_TOKEN` repo
secret. Manual deploy is still `fly deploy` if needed (app name `unphase`,
deployed at https://unphase.fly.dev, repo at github.com/tomdyson/unphase).

There is no test suite, linter config, or build step.

## Architecture

Two-file Python app plus a single HTML page. The split is meaningful:

- **`app/align.py`** — pure signal processing, no web concerns.
  `probe_channels()` shells out to `ffprobe` for channel count.
  `decode_to_mono()` shells out to `ffmpeg` to convert any input to 48 kHz
  mono float via a temp WAV. `align()` does FFT cross-correlation on a
  centered ≤30 s window of each signal, normalized so the peak is a proper
  correlation coefficient in [-1, 1]. Parabolic interpolation around the
  peak gives sub-sample accuracy. After the fine peak it runs a
  Hilbert-envelope coarse correlation as a sanity check and sets
  `sanity_ok=False` if they disagree by more than 5 ms. `peak_series()`
  downsamples audio to N normalized peaks for waveform display, and
  `analysis_window()` returns the same centered 30 s slice `align()` uses
  so peak series and correlation match visually.
- **`app/main.py`** — FastAPI wrapper. Accepts an N-file batch as
  `files[]` multipart plus a `reference_index` form field, streams
  newline-delimited JSON phase events as each track is decoded then
  aligned against the reference. Event shapes: `decoding`, `reference`
  (with reference peaks + per-file channels/durations), `analyzing`,
  `result` (one per target pair, includes `target_peaks` for waveform
  display), and a terminal `done` or `error`. `align()` is CPU-bound and
  runs in `asyncio.to_thread()`. Temp files are cleaned up in the stream
  generator's `finally`. A separate `POST /api/export` accepts a JSON
  array of per-track rows and returns a session CSV for paste into DAW
  notes.
- **`app/static/index.html`** + **`app/static/styles.css`** — single-page
  frontend, Alpine.js from CDN (no build step, no Node). Light warm
  neutral palette, Inter Tight + JetBrains Mono, two-column layout (file
  list + results). The Alpine data function is named `unphase()` and is
  referenced as `x-data="unphase()"` on the body. Uses `XMLHttpRequest`
  rather than `fetch` so it can observe upload progress via
  `upload.onprogress` and parse the streamed NDJSON response progressively
  from `responseText`. The results pane renders headline + four KPI cards
  (delay, 44.1 k samples, polarity, confidence), an amber sanity notice
  when the envelope check disagrees, canvas waveforms fed by
  `target_peaks`, a spatial SVG diagram (only when the target — not the
  reference — is the close mic), a batch table for 3+ targets, and
  collapsible technical details. Icons are inlined SVG strings in an
  `ICONS` object used via `x-html`.

### Sign convention (important)

`scipy.signal.correlate(a, b)` returns lag of `b` relative to `a`:
- `lag > 0` → `b` arrived earlier → **b is the close mic**
- `lag < 0` → `a` arrived earlier → **a is the close mic**

The API returns the absolute `delay_samples` to apply to the close mic
(always non-negative) plus the close mic's label. `lag_samples` is the raw
signed value for debugging. Don't "fix" the sign without re-verifying
against this convention — the comment block at the top of
`AlignmentResult` is authoritative.

### Confidence metric

`confidence` in the API response is `1 - 1/max(peak_over_median, 1)`,
mapping peak-prominence to a 0-1 strength score. A peak/noise-floor ratio
of ~8 (typical for a good match) gives ~0.88. The bare peak correlation
in [-1, 1] is also returned as `peak_corr`; don't confuse the two. Tonal
material (violin, sustained vocals) produces peak amplitudes ~0.5 even
when the alignment is rock-solid, which is why prominence is the primary
metric.

### Deployment shape

Single-container Dockerfile on Fly.io in LHR. `fly.toml` sets
`auto_stop_machines = "stop"` with `min_machines_running = 0`, so the app
cold-starts on first request and costs nothing when idle. The VM is
1 GB / shared-cpu-1x — fine for the default analysis window but worth
remembering that two 200 MB uploads decoded to float64 can eat memory
under concurrency (`soft_limit = 15`).

Health check is `GET /healthz`; Fly hits it every 30 s.
