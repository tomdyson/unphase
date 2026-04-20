"""FastAPI web app for Unphase — mic alignment tool."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

from .align import align, analysis_window, decode_to_mono, peak_series, probe_channels

app = FastAPI(title="Unphase", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
TARGET_SR = 48000
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB per file

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/align")
async def api_align(request: Request) -> StreamingResponse:
    """Accept N audio files plus a reference index, stream NDJSON progress.

    Form fields:
      files[]           — one or more audio uploads (≥2 required)
      reference_index   — 0-based index of the reference file

    Event stream (one JSON object per line):

      {"phase": "decoding",  "index": i, "name": "..."}
      {"phase": "reference", "index": r, "name": "...", "peaks": [...],
       "durations": [...], "channels": [...]}
      {"phase": "analyzing", "index": i}
      {"phase": "result",    "index": i, ...per-pair payload}
      {"phase": "done"}

    On failure the stream emits `{"phase": "error", "detail": "..."}`.
    """
    form = await request.form()
    try:
        ref_idx = int(form.get("reference_index", "0"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="reference_index must be an integer")

    uploads = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
    if len(uploads) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 audio files.")
    if not 0 <= ref_idx < len(uploads):
        raise HTTPException(status_code=400, detail="reference_index out of range.")

    tmp_paths: list[str] = []
    names: list[str] = []
    try:
        for up in uploads:
            tmp_paths.append(await _save_upload(up))
            names.append(up.filename or "")
    except Exception:
        for p in tmp_paths:
            if p and os.path.exists(p):
                os.unlink(p)
        raise

    async def stream():
        try:
            async for line in _run_batch(tmp_paths, names, ref_idx):
                yield line
        finally:
            for p in tmp_paths:
                if p and os.path.exists(p):
                    os.unlink(p)

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/export")
async def api_export(request: Request) -> Response:
    """Serialize a finished session's results as CSV for paste into DAW notes."""
    payload = await request.json()
    rows = payload.get("rows") or []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "track",
            "delay_ms",
            "delay_samples_48k",
            "delay_samples_44.1k",
            "polarity",
            "confidence",
            "peak_over_median",
            "sanity",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.get("name", ""),
                r.get("delay_ms", ""),
                r.get("delay_samples_48k", ""),
                r.get("delay_samples_44k", ""),
                "invert" if r.get("invert_polarity") else "normal",
                r.get("confidence", ""),
                r.get("peak_over_median", ""),
                "ok" if r.get("sanity_ok") else "review",
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="unphase-session.csv"'},
    )


async def _run_batch(paths: list[str], names: list[str], ref_idx: int):
    def event(**kw: Any) -> bytes:
        return (json.dumps(kw) + "\n").encode()

    try:
        audios: list = []
        channels: list[int] = []
        for i, p in enumerate(paths):
            yield event(phase="decoding", index=i, name=names[i])
            ch = await asyncio.to_thread(probe_channels, p)
            au = await asyncio.to_thread(decode_to_mono, p, TARGET_SR)
            audios.append(au)
            channels.append(ch)
    except RuntimeError as exc:
        yield event(phase="error", detail=f"Could not decode audio. ffmpeg says: {exc}")
        return

    for i, a in enumerate(audios):
        if len(a) < TARGET_SR:
            yield event(
                phase="error",
                detail=f"{names[i]!r} must contain at least 1 second of audio.",
            )
            return

    ref_audio = audios[ref_idx]
    ref_name = names[ref_idx]
    ref_peaks = peak_series(analysis_window(ref_audio, TARGET_SR), 400)

    yield event(
        phase="reference",
        index=ref_idx,
        name=ref_name,
        peaks=ref_peaks,
        durations=[round(len(a) / TARGET_SR, 2) for a in audios],
        channels=channels,
    )

    for i, (audio, name) in enumerate(zip(audios, names)):
        if i == ref_idx:
            continue
        yield event(phase="analyzing", index=i, name=name)
        try:
            result = await asyncio.to_thread(
                align, ref_audio, audio, TARGET_SR, 50.0, ref_name, name
            )
        except Exception as exc:
            yield event(phase="error", index=i, detail=f"Alignment failed: {exc}")
            return

        target_peaks = peak_series(analysis_window(audio, TARGET_SR), 400)

        yield event(
            phase="result",
            index=i,
            name=name,
            channels=channels[i],
            duration_s=round(len(audio) / TARGET_SR, 2),
            sample_rate=result.sample_rate,
            lag_samples=result.lag_samples,
            sub_sample_lag=round(result.sub_sample_lag, 3),
            invert_polarity=result.invert_polarity,
            confidence=round(result.confidence, 4),
            peak_corr=round(result.peak_corr, 4),
            peak_over_median=round(result.peak_over_median, 2),
            envelope_lag_samples=result.envelope_lag_samples,
            sanity_ok=result.sanity_ok,
            close_mic=result.close_mic,
            close_is_reference=(result.close_mic == ref_name),
            delay_samples_48k=result.delay_samples,
            delay_samples_44k=round(result.delay_ms * 44.1),
            delay_ms=round(result.delay_ms, 3),
            target_peaks=target_peaks,
        )

    yield event(phase="done")


async def _save_upload(upload: UploadFile) -> str:
    """Stream an UploadFile to a temp path, enforcing size limit."""
    suffix = Path(upload.filename or "").suffix or ".bin"
    fd, path = tempfile.mkstemp(suffix=suffix)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                    )
                out.write(chunk)
        return path
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
