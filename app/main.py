"""FastAPI web app for Unphase — mic alignment tool."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

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
) -> StreamingResponse:
    """Accept two audio files and stream phase events as NDJSON.

    Each line of the response body is a JSON object describing one phase
    transition. The final line's `phase` is either `result` (success, with
    full payload) or `error` (with `detail`). Events emitted in order:

      {"phase": "decode", "file": "a"}
      {"phase": "decode", "file": "b"}
      {"phase": "check"}
      {"phase": "verify"}
      {"phase": "result", ...payload}
    """
    tmp_a = await _save_upload(file_a)
    try:
        tmp_b = await _save_upload(file_b)
    except Exception:
        if os.path.exists(tmp_a):
            os.unlink(tmp_a)
        raise

    async def stream():
        try:
            async for line in _run_pipeline(tmp_a, tmp_b, file_a.filename, file_b.filename):
                yield line
        finally:
            for p in (tmp_a, tmp_b):
                if p and os.path.exists(p):
                    os.unlink(p)

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _run_pipeline(
    tmp_a: str, tmp_b: str, name_a: str | None, name_b: str | None
):
    def event(**kw) -> bytes:
        return (json.dumps(kw) + "\n").encode()

    try:
        yield event(phase="decode", file="a")
        audio_a = await asyncio.to_thread(decode_to_mono, tmp_a, TARGET_SR)

        yield event(phase="decode", file="b")
        audio_b = await asyncio.to_thread(decode_to_mono, tmp_b, TARGET_SR)
    except RuntimeError as exc:
        yield event(phase="error", detail=f"Could not decode audio. ffmpeg says: {exc}")
        return

    if min(len(audio_a), len(audio_b)) < TARGET_SR:
        yield event(phase="error", detail="Each file must contain at least 1 second of audio.")
        return

    # align() is sync and CPU-bound. Run it in a thread and forward its
    # phase callbacks back here via a threadsafe queue.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def on_phase(name: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, name)

    task = asyncio.create_task(
        asyncio.to_thread(
            align,
            audio_a,
            audio_b,
            TARGET_SR,
            50.0,
            name_a or "file A",
            name_b or "file B",
            on_phase,
        )
    )

    while not task.done() or not queue.empty():
        try:
            name = await asyncio.wait_for(queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        yield event(phase=name)

    try:
        result = await task
    except Exception as exc:
        yield event(phase="error", detail=f"Alignment failed: {exc}")
        return

    yield event(
        phase="result",
        file_a=name_a,
        file_b=name_b,
        duration_a_s=round(len(audio_a) / TARGET_SR, 2),
        duration_b_s=round(len(audio_b) / TARGET_SR, 2),
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
        delay_samples_48k=result.delay_samples,
        delay_samples_44k=round(result.delay_ms * 44.1),
        delay_ms=round(result.delay_ms, 3),
    )


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
