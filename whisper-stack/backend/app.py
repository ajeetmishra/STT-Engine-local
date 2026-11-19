"""FastAPI speech-to-text service backed by faster-whisper (CPU / int8)."""

import asyncio
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
from faster_whisper import WhisperModel, decode_audio

from live import LiveTranscriber, pcm16_to_float32
from textfmt import STYLE_PROMPT, format_text

MODEL_NAME = os.getenv("WHISPER_MODEL", "base.en")
DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
CPU_THREADS = int(os.getenv("WHISPER_CPU_THREADS", "0"))  # 0 = let CTranslate2 decide
BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "1"))
VAD_FILTER = os.getenv("WHISPER_VAD_FILTER", "true").lower() in ("1", "true", "yes")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
LANGUAGE = "en" if MODEL_NAME.endswith(".en") else None

# Live (WebSocket) dictation
LIVE_STEP_SECONDS = float(os.getenv("LIVE_STEP_SECONDS", "1.0"))
LIVE_MAX_BUFFER_SECONDS = float(os.getenv("LIVE_MAX_BUFFER_SECONDS", "12"))
LIVE_MAX_SESSIONS = int(os.getenv("LIVE_MAX_SESSIONS", "2"))
LIVE_MAX_SESSION_SECONDS = float(os.getenv("LIVE_MAX_SESSION_SECONDS", "600"))
# Browsers always send Origin; reject other web pages from driving the mic stream.
# Non-browser clients (no Origin header) are allowed. "*" disables the check.
LIVE_ALLOWED_ORIGINS = {
    o.strip()
    for o in os.getenv(
        "LIVE_ALLOWED_ORIGINS", "http://localhost:8501,http://127.0.0.1:8501"
    ).split(",")
    if o.strip()
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whisper-backend")

model: WhisperModel | None = None
live_sessions = 0


@asynccontextmanager
async def lifespan(_: FastAPI):
    global model
    logger.info(
        "Loading model=%s device=%s compute_type=%s cpu_threads=%s (cache: %s)",
        MODEL_NAME, DEVICE, COMPUTE_TYPE, CPU_THREADS, os.getenv("HF_HOME", "default"),
    )
    started = time.perf_counter()
    model = WhisperModel(
        MODEL_NAME,
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        cpu_threads=CPU_THREADS,
    )
    logger.info("Model loaded in %.1fs", time.perf_counter() - started)
    yield
    model = None


app = FastAPI(title="Whisper STT Backend", version="1.0.0", lifespan=lifespan)


def _sniff_format(header: bytes) -> str | None:
    """Return a temp-file suffix for supported containers, or None if unrecognised."""
    if len(header) >= 12 and header[0:4] == b"RIFF" and header[8:12] == b"WAVE":
        return ".wav"
    if header[:3] == b"ID3" or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return ".mp3"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return ".mp4"
    return None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/transcribe")
def transcribe(file: UploadFile = File(...)) -> dict:
    # Declared as a sync endpoint so FastAPI runs the CPU-bound inference in its
    # threadpool instead of blocking the event loop.
    if model is None:
        raise HTTPException(status_code=503, detail="Model is not loaded yet.")

    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB limit.")
    suffix = _sniff_format(data[:12])
    if suffix is None:
        raise HTTPException(status_code=415, detail="Unsupported format. Upload WAV, MP3 or MP4.")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        del data

        # Decode separately so a bad file (corrupt, or a video with no audio track)
        # is reported as a client error rather than an inference failure.
        try:
            audio = decode_audio(tmp_path)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Could not decode audio from the file: {exc}"
            ) from exc

        started = time.perf_counter()
        segments, info = model.transcribe(
            audio,
            beam_size=BEAM_SIZE,
            language=LANGUAGE,
            vad_filter=VAD_FILTER,
            initial_prompt=STYLE_PROMPT or None,
        )
        # `segments` is a lazy generator; decoding happens while iterating.
        text = format_text(" ".join(segment.text.strip() for segment in segments))
        logger.info(
            "Transcribed %.1fs of audio in %.2fs (%d chars)",
            info.duration, time.perf_counter() - started, len(text),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Transcription failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return {"text": text}


@app.websocket("/ws/transcribe")
async def transcribe_live(ws: WebSocket) -> None:
    """Live dictation.

    Client -> server: binary frames of 16 kHz mono little-endian int16 PCM, then a text
    frame ``{"type": "stop"}`` to finalize.
    Server -> client (JSON text frames):
      {"type": "ready"}
      {"type": "update", "committed": "<newly final text>", "partial": "<tentative text>"}
      {"type": "final", "committed": "<newly final text>", "text": "<full session text>"}
      {"type": "error", "message": "..."}
    """
    global live_sessions

    origin = ws.headers.get("origin")
    if origin and "*" not in LIVE_ALLOWED_ORIGINS and origin not in LIVE_ALLOWED_ORIGINS:
        logger.warning("Rejected live session from origin %s", origin)
        await ws.close(code=1008)
        return

    await ws.accept()
    if model is None or live_sessions >= LIVE_MAX_SESSIONS:
        reason = "Model is not loaded yet." if model is None else "Too many live sessions."
        await ws.send_json({"type": "error", "message": reason})
        await ws.close(code=1013)
        return

    live_sessions += 1
    session = LiveTranscriber(
        model,
        language=LANGUAGE,
        beam_size=BEAM_SIZE,
        max_buffer_seconds=LIVE_MAX_BUFFER_SECONDS,
    )

    async def receive_audio() -> bool:
        """Feed audio into the session. True = client asked to finalize, False = it left."""
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return False
            if message.get("bytes"):
                session.add_audio(pcm16_to_float32(message["bytes"]))
                if session.total_seconds >= LIVE_MAX_SESSION_SECONDS:
                    return True
            elif message.get("text"):
                try:
                    if json.loads(message["text"]).get("type") == "stop":
                        return True
                except (ValueError, AttributeError):
                    pass

    receiver = asyncio.create_task(receive_audio())
    started = time.perf_counter()
    try:
        await ws.send_json({"type": "ready"})
        # Transcription runs in a worker thread while the receiver keeps buffering audio,
        # so a slow pass simply covers more audio next time instead of falling behind.
        while not receiver.done():
            if session.pending_seconds >= LIVE_STEP_SECONDS:
                committed, partial = await asyncio.to_thread(session.process)
                await ws.send_json({"type": "update", "committed": committed, "partial": partial})
            else:
                await asyncio.wait({receiver}, timeout=0.05)

        if receiver.result():
            committed = await asyncio.to_thread(session.finish)
            await ws.send_json({"type": "final", "committed": committed, "text": session.text})
            await ws.close()
        logger.info(
            "Live session ended: %.1fs audio in %.1fs wall, %d chars",
            session.total_seconds, time.perf_counter() - started, len(session.text),
        )
    except Exception as exc:
        # Includes the client vanishing mid-send; reporting back is best effort.
        logger.warning("Live session aborted: %r", exc)
        try:
            await ws.send_json({"type": "error", "message": f"Live transcription failed: {exc}"})
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        receiver.cancel()
        live_sessions -= 1
