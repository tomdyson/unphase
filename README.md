# Unphase

A small web tool that finds the sample-accurate delay and polarity needed
to align two microphone recordings — typically a stereo pair and a spot
mic from the same performance.

## How it works

Cross-correlation via FFT finds the lag at which the two signals are most
similar. The sign of that lag tells you which mic arrived first (that's
the close mic), and the sign of the correlation peak tells you whether
polarity needs flipping. The tool works out which file is the close mic
automatically — you don't need to label them.

## Local development

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and `ffmpeg`
on your PATH.

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Deploy to Fly.io

```bash
fly deploy
```

The `fly.toml` here is set up to auto-stop machines when idle, so the
tool costs nothing when nobody's using it.

## Notes

- Accepts WAV, AIFF, FLAC, MP3, M4A and anything else ffmpeg can decode.
- Max upload size is 200 MB per file.
- Analysis is done on a 30-second window from the middle of each file at
  48 kHz mono; this is enough for a reliable correlation and fast to
  compute.
- Search window is ±50 ms, which covers any realistic mic spacing.
