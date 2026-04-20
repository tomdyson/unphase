# Unphase

A small web tool that finds the sample-accurate delay and polarity needed
to align microphone recordings against a pinned reference — typically a
stereo pair and one or more spot mics from the same performance.
Handles batch sessions: drop N files, pin the reference, and each
non-reference track is aligned against it.

## How it works

Cross-correlation via FFT finds the lag at which the two signals are most
similar. The sign of that lag tells you which mic arrived first (that's
the close mic), and the sign of the correlation peak tells you whether
polarity needs flipping. The tool works out which file is the close mic
automatically — you don't need to label them.

Confidence is reported as peak-over-median of the correlation window, not
the bare peak amplitude, so a clean but tonal signal (violin, sustained
vocal) registers as confident when it is. A second coarse alignment based
on the Hilbert envelope runs as a sanity check; if it disagrees with the
fine correlator by more than 5 ms the result is flagged so you know to
verify by ear.

Sound travels ≈ 34 cm per millisecond, so the result panel draws a
spatial diagram showing source, close mic, and far mic with the measured
close-to-far distance labeled in both metres and milliseconds. Mono and
stereo inputs are drawn with distinct mic icons.

## Local development

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and `ffmpeg`
(with `ffprobe`) on your PATH.

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Deploy to Fly.io

Pushing to `main` on GitHub deploys automatically via
`.github/workflows/fly-deploy.yml`. To deploy manually:

```bash
fly deploy
```

The `fly.toml` here is set up to auto-stop machines when idle, so the
tool costs nothing when nobody's using it.

## API

`POST /api/align` with a multipart body: one or more `files` fields (≥2
audio uploads) plus a `reference_index` form field indicating which file
in the list is the reference. The response is newline-delimited JSON —
one line per phase transition:

```
{"phase": "decoding",  "index": 0, "name": "..."}
{"phase": "reference", "index": r, "peaks": [...], "durations": [...], "channels": [...]}
{"phase": "analyzing", "index": i, "name": "..."}
{"phase": "result",    "index": i, ...per-pair payload including target_peaks}
{"phase": "done"}
```

On failure the stream terminates with `{"phase": "error", "detail": "..."}`.

`POST /api/export` accepts a JSON body `{"rows": [...]}` and returns a
`text/csv` attachment for paste into DAW session notes.

## Notes

- Accepts WAV, AIFF, FLAC, MP3, M4A and anything else ffmpeg can decode.
- Max upload size is 200 MB per file.
- Analysis is done on a 30-second window from the middle of each file at
  48 kHz mono; this is enough for a reliable correlation and fast to
  compute.
- Search window is ±50 ms, which covers any realistic mic spacing.
