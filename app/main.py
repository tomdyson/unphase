"""FastAPI web app for Unphase — mic alignment tool."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .align import align, decode_to_mono

app = FastAPI(title="Unphase", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
TARGET_SR = 48000
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB per file


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/align")
async def api_align(
    file_a: UploadFile = File(...),
    file_b: UploadFile = File(...),
) -> JSONResponse:
    """Accept two audio files and return alignment info."""
    tmp_a: str | None = None
    tmp_b: str | None = None
    try:
        tmp_a = await _save_upload(file_a)
        tmp_b = await _save_upload(file_b)

        try:
            audio_a = decode_to_mono(tmp_a, TARGET_SR)
            audio_b = decode_to_mono(tmp_b, TARGET_SR)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Could not decode audio. ffmpeg says: {exc}",
            )

        min_len = min(len(audio_a), len(audio_b))
        if min_len < TARGET_SR:
            raise HTTPException(
                status_code=400,
                detail="Each file must contain at least 1 second of audio.",
            )

        result = align(
            audio_a,
            audio_b,
            sample_rate=TARGET_SR,
            max_ms=50.0,
            label_a=file_a.filename or "file A",
            label_b=file_b.filename or "file B",
        )

        return JSONResponse(
            {
                "file_a": file_a.filename,
                "file_b": file_b.filename,
                "duration_a_s": round(len(audio_a) / TARGET_SR, 2),
                "duration_b_s": round(len(audio_b) / TARGET_SR, 2),
                "sample_rate": result.sample_rate,
                "lag_samples": result.lag_samples,
                "sub_sample_lag": round(result.sub_sample_lag, 3),
                "invert_polarity": result.invert_polarity,
                "confidence": round(result.confidence, 4),
                "close_mic": result.close_mic,
                "delay_samples_48k": result.delay_samples,
                "delay_samples_44k": round(result.delay_ms * 44.1),
                "delay_ms": round(result.delay_ms, 3),
            }
        )
    finally:
        for path in (tmp_a, tmp_b):
            if path and os.path.exists(path):
                os.unlink(path)


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
