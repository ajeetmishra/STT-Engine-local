"""Streamlit dictation UI: record in the browser, transcribe via the backend.

The sidebar also accepts an MP3/MP4 file upload, transcribed in one pass.

Two mic modes:
- Record-then-transcribe (default): record a clip, POST it to /transcribe.
- Live dictation: the browser streams mic audio straight to the backend WebSocket
  (/ws/transcribe) and words appear as you speak.
"""

import hashlib
import os
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components
from audio_recorder_streamlit import audio_recorder

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("TRANSCRIBE_TIMEOUT", "300"))
# Uploaded files (long talks/videos) can take far longer than a mic clip on CPU.
UPLOAD_TIMEOUT = float(os.getenv("UPLOAD_TIMEOUT", "1800"))
MAX_UPLOAD_MB = int(os.getenv("STREAMLIT_SERVER_MAX_UPLOAD_SIZE", "200"))
PAUSE_THRESHOLD = float(os.getenv("RECORDER_PAUSE_THRESHOLD", "3.0"))
SAMPLE_RATE = int(os.getenv("RECORDER_SAMPLE_RATE", "16000"))
# The live WebSocket is opened by the *browser*, so it needs a host-reachable URL, not
# http://backend:8000. Empty = ws://<host the page was loaded from>:LIVE_BACKEND_PORT.
LIVE_WS_URL = os.getenv("LIVE_WS_URL", "")
LIVE_BACKEND_PORT = int(os.getenv("LIVE_BACKEND_PORT", "8000"))

live_dictation = components.declare_component(
    "live_dictation", path=str(Path(__file__).parent / "live_dictation")
)

st.set_page_config(page_title="Local Dictation", page_icon="🎙️", layout="centered")

if "transcript" not in st.session_state:
    st.session_state.transcript = ""
if "last_audio_hash" not in st.session_state:
    st.session_state.last_audio_hash = None
if "last_upload_hash" not in st.session_state:
    st.session_state.last_upload_hash = None
if "live_session_ids" not in st.session_state:
    st.session_state.live_session_ids = set()


def backend_healthy() -> tuple[bool, str]:
    try:
        resp = requests.get(f"{BACKEND_URL}/health", timeout=3)
        resp.raise_for_status()
        return resp.json().get("status") == "ok", ""
    except requests.exceptions.ConnectionError:
        return False, "Connection refused — backend is down or still loading the model."
    except requests.exceptions.Timeout:
        return False, "Health check timed out."
    except (requests.exceptions.RequestException, ValueError) as exc:
        return False, str(exc)


def transcribe_file(data: bytes, filename: str, mime: str, timeout: float) -> str:
    files = {"file": (filename, data, mime)}
    resp = requests.post(f"{BACKEND_URL}/transcribe", files=files, timeout=timeout)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"Backend returned HTTP {resp.status_code}: {detail}")
    return resp.json().get("text", "")


def transcribe(audio_bytes: bytes) -> str:
    return transcribe_file(audio_bytes, "recording.wav", "audio/wav", REQUEST_TIMEOUT)


def run_transcription(fn, timeout: float, spinner: str = "Transcribing…") -> str | None:
    """Call ``fn()`` and show a friendly error on failure. Returns None if it failed."""
    try:
        with st.spinner(spinner):
            return fn()
    except requests.exceptions.ConnectionError:
        st.error(
            f"Cannot reach the backend at {BACKEND_URL}. "
            "Check that the `backend` container is running: `podman compose ps`."
        )
    except requests.exceptions.Timeout:
        st.error(f"Transcription timed out after {timeout:g}s.")
    except (requests.exceptions.RequestException, RuntimeError, ValueError) as exc:
        st.error(f"Transcription failed: {exc}")
    return None


def append_to_transcript(text: str) -> None:
    # Must run before the text_area below is instantiated in this script run.
    existing = st.session_state.transcript.rstrip()
    st.session_state.transcript = f"{existing} {text}".strip() if existing else text


def record_then_transcribe() -> None:
    st.caption(
        "Click the mic to start recording, click again to stop "
        f"(auto-stops after {PAUSE_THRESHOLD:g}s of silence). The clip is transcribed when you stop."
    )
    audio_bytes = audio_recorder(
        text="Click to record",
        recording_color="#e5484d",
        neutral_color="#6c757d",
        icon_name="microphone",
        icon_size="3x",
        pause_threshold=PAUSE_THRESHOLD,
        sample_rate=SAMPLE_RATE,
    )
    if not audio_bytes:
        return

    st.audio(audio_bytes, format="audio/wav")

    # Streamlit reruns the script on every interaction; only transcribe new clips.
    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
    if audio_hash == st.session_state.last_audio_hash:
        return
    text = run_transcription(lambda: transcribe(audio_bytes), REQUEST_TIMEOUT)
    if text is not None:
        st.session_state.last_audio_hash = audio_hash
        if text:
            append_to_transcript(text)
        else:
            st.warning("No speech detected in the recording.")


def live_mode() -> None:
    st.caption(
        "Click the mic and speak. Words appear as they are recognised (grey = tentative). "
        "Click again to stop; the text is then added to the transcript below."
    )
    result = live_dictation(
        ws_url=LIVE_WS_URL, backend_port=LIVE_BACKEND_PORT, key="live_dictation", default=None
    )
    # The component keeps returning its last value on every rerun; append each session once.
    if result and result.get("id") not in st.session_state.live_session_ids:
        st.session_state.live_session_ids.add(result.get("id"))
        if result.get("text"):
            append_to_transcript(result["text"])


with st.sidebar:
    st.header("Backend")
    st.code(BACKEND_URL, language=None)
    ok, reason = backend_healthy()
    if ok:
        st.success("Online")
    else:
        st.error("Offline")
        if reason:
            st.caption(reason)
    if st.button("Re-check"):
        st.rerun()
    st.divider()
    st.header("Transcribe a file")
    uploaded = st.file_uploader(
        "MP3 audio or MP4 video",
        type=["mp3", "mp4"],
        key="upload_file",
        help=f"Up to {MAX_UPLOAD_MB} MB. The text is added to the transcript on the right.",
    )
    if uploaded is not None:
        is_video = uploaded.name.lower().endswith(".mp4")
        if is_video:
            st.video(uploaded)
        else:
            st.audio(uploaded)
        if st.button("Transcribe file", type="primary", disabled=not ok):
            data = uploaded.getvalue()
            upload_hash = hashlib.sha256(data).hexdigest()
            if upload_hash == st.session_state.last_upload_hash:
                st.info("This file was already transcribed.")
            else:
                mime = uploaded.type or ("video/mp4" if is_video else "audio/mpeg")
                text = run_transcription(
                    lambda: transcribe_file(data, uploaded.name, mime, UPLOAD_TIMEOUT),
                    UPLOAD_TIMEOUT,
                    spinner=f"Transcribing {uploaded.name}… (long files can take a while)",
                )
                if text is not None:
                    st.session_state.last_upload_hash = upload_hash
                    if text:
                        append_to_transcript(text)
                        st.success("Added to transcript.")
                    else:
                        st.warning("No speech detected in the file.")
    st.divider()
    if st.button("Clear transcript"):
        st.session_state.transcript = ""
        st.session_state.last_audio_hash = None
        st.session_state.last_upload_hash = None
        st.rerun()

st.title("🎙️ Local Dictation")

live = st.toggle(
    "Live dictation",
    value=False,
    help=(
        "Off: record a clip, then transcribe it in one pass (most accurate). "
        "On: stream audio while you speak and see words appear live (uses more CPU)."
    ),
)
st.caption(f"Mode: **{'Live dictation' if live else 'Record, then transcribe'}** · audio never leaves this machine.")

if live:
    live_mode()
else:
    record_then_transcribe()

st.text_area("Transcript (editable)", key="transcript", height=250)

st.download_button(
    "Download transcript",
    data=st.session_state.transcript,
    file_name="transcript.txt",
    mime="text/plain",
    disabled=not st.session_state.transcript,
)
