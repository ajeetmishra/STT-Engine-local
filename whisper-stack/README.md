# whisper-stack — Local Voice Dictation

A fully local, containerized dictation app. You speak in the browser, the audio goes to a
FastAPI + `faster-whisper` backend on your own CPU, and you get back editable text.
No audio leaves your machine.

There are two modes, switched with the **Live dictation** toggle in the UI:

| Mode | Default | How it works | Best for |
|---|---|---|---|
| **Record, then transcribe** | ✅ | Record a clip. When you stop, it is sent to `POST /transcribe` and transcribed in one pass. | Accuracy, longer passages, lowest CPU |
| **Live dictation** | | Mic audio streams to `WS /ws/transcribe`. Words appear about 1 s after you say them. | Seeing text as you speak |

```text
whisper-stack/
├── docker-compose.yml        # Orchestration (podman compose / podman-compose)
├── start.bat                 # start stack + open browser (start.bat --build to rebuild)
├── stop.bat                  # podman-compose down (stop.bat --machine also stops the VM)
├── README.md
├── backend/
│   ├── Containerfile         # python:3.11-slim + ffmpeg + uv sync
│   ├── pyproject.toml        # dependencies (managed by uv)
│   ├── uv.lock               # exact, reproducible dependency lock
│   ├── app.py                # FastAPI: GET /health, POST /transcribe, WS /ws/transcribe
│   └── live.py               # streaming engine (rolling buffer + LocalAgreement)
└── frontend/
    ├── Containerfile         # python:3.11-slim + uv sync
    ├── pyproject.toml
    ├── uv.lock
    ├── app.py                # Streamlit UI, mode toggle, transcript
    └── live_dictation/
        └── index.html        # custom component: mic → 16 kHz PCM → WebSocket, live text
```

```text
 Record mode:  Browser ──► frontend :8501 (Streamlit) ──HTTP POST──► backend:8000/transcribe
 Live mode:    Browser ──────────── WebSocket (PCM audio ⇄ text) ──► localhost:8000/ws/transcribe
                                    └────────── whisper-net (bridge) ──────────┘   │
                                                                     hf-cache volume (/models)
```

In live mode the **browser** opens the WebSocket directly to the backend's published port 8000.
Streamlit can't proxy WebSockets, and WebRTC's UDP media is hard to route into Podman
containers on Windows. A plain WebSocket over the published port avoids both problems.

| Service    | Host port | Purpose                                                  |
|------------|-----------|----------------------------------------------------------|
| `backend`  | 8000      | `GET /health` → `{"status":"ok"}`, `POST /transcribe` → `{"text":"..."}`, `WS /ws/transcribe` |
| `frontend` | 8501      | Streamlit dictation UI                                   |

---

## 1. Prerequisites (Windows 11)

1. **Podman Desktop / Podman CLI** (tested with Podman 5.8.3, WSL machine).
2. **[uv](https://docs.astral.sh/uv/)** (`winget install astral-sh.uv`). uv manages all Python
   dependencies: the host tooling and both container images.
3. **A compose provider.** `podman compose` delegates to `docker-compose` or `podman-compose`.
   Install `podman-compose` as a uv tool (one time):

   ```powershell
   uv tool install podman-compose
   uv tool update-shell        # adds %USERPROFILE%\.local\bin to your user PATH
   # Open a NEW PowerShell window afterwards, then verify:
   podman-compose version
   podman compose version
   ```

   Upgrade later with `uv tool upgrade podman-compose`.

4. **Start the Podman machine:**

   ```powershell
   podman machine start
   ```

   Optional: give the VM more RAM/CPU, which helps with `small.en`:

   ```powershell
   podman machine stop
   podman machine set --cpus 8 --memory 4096
   podman machine start
   ```

---

## 2. Build & run

### Quick start: double-click scripts

| Script | What it does |
|---|---|
| `start.bat` | Starts the Podman machine if needed and runs `podman-compose up -d`. It then waits until the backend and frontend health checks pass, which allows up to 10 min on first run while the model downloads. Finally it opens **http://localhost:8501** in your default browser. |
| `start.bat --build` | Same, but rebuilds the images and recreates the containers. Use after changing code or dependencies. |
| `stop.bat` | `podman-compose down`. Containers and network are removed; the model cache is kept. |
| `stop.bat --machine` | Also stops the Podman machine to free its RAM. |

Both scripts work from any folder or shortcut, and add `%USERPROFILE%\.local\bin` to PATH
themselves. On errors they pause so the window stays open.

### Manual commands

```powershell
cd E:\stt-engine-local\whisper-stack
podman compose up --build -d
```

On the first start, the backend downloads the model (`base.en` is about 145 MB) into the `hf-cache` volume.
Later starts load it from the cache in about 1 second.

Open the UI at **http://localhost:8501**.

> Browsers only allow microphone access on secure origins. `http://localhost` qualifies.
> Opening the app from another device by LAN IP (`http://192.168.x.x:8501`) will **not**
> get mic access unless you put HTTPS in front of it.

---

## 3. Verify

### Backend health

```powershell
Invoke-RestMethod http://localhost:8000/health
# status
# ------
# ok

curl.exe http://localhost:8000/health
# {"status":"ok"}
```

> In Windows PowerShell 5.1, `curl` is an alias for `Invoke-WebRequest`. Use `curl.exe` explicitly.

### Transcription (end-to-end)

Generate a test WAV with Windows TTS and upload it:

```powershell
Add-Type -AssemblyName System.Speech
$wav = "$env:TEMP\whisper-test.wav"
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth.SetOutputToWaveFile($wav, $fmt)
$synth.Speak("The quick brown fox jumps over the lazy dog.")
$synth.Dispose()

# Windows PowerShell 5.1:
curl.exe -s -F "file=@$wav;type=audio/wav" http://localhost:8000/transcribe

# PowerShell 7+ only (-Form is not available in 5.1):
Invoke-RestMethod -Uri http://localhost:8000/transcribe -Method Post -Form @{ file = Get-Item $wav }
```

Expected: `{"text":"The quick brown fox jumps over the lazy dog."}`

Interactive API docs: **http://localhost:8000/docs** (WebSocket routes are not listed there)

### Live dictation

Open http://localhost:8501, switch on **Live dictation**, and click the mic. Text appears as
you speak:
- black text is final;
- grey italic text is tentative and may still change.

Click the mic again to stop. The final text is then added to the editable transcript.

#### WebSocket protocol

Use this if you want to write your own client.

- **Client → server:**
  - binary frames of 16 kHz, mono, little-endian int16 PCM (100 ms per frame works well);
  - then a text frame `{"type":"stop"}` to finish.
- **Server → client:** JSON text frames.

  ```text
  {"type":"ready"}                                           # start sending audio
  {"type":"update","committed":" new final words","partial":" tentative words"}
  {"type":"final","committed":" last words","text":"full session text"}
  {"type":"error","message":"..."}
  ```

  `committed` pieces keep Whisper's leading spaces. Concatenate them as-is, without adding
  separators.

### Container status / internal networking

```powershell
podman compose ps
podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
podman exec whisper-frontend python -c "import requests; print(requests.get('http://backend:8000/health').json())"
```

---

## 4. Logs & troubleshooting

```powershell
podman compose logs -f backend          # follow backend logs
podman compose logs --tail 100 frontend # last 100 lines of frontend
podman logs whisper-backend             # same thing, via plain podman
podman inspect --format "{{.State.Health.Status}}" whisper-backend

# Debug logging: shows every live-dictation pass (buffer offset, committed words, raw hypothesis)
$env:LOG_LEVEL = "DEBUG"; podman compose up -d --force-recreate backend
Remove-Item Env:LOG_LEVEL; podman compose up -d --force-recreate backend   # back to INFO
```

| Symptom | Fix |
|---|---|
| `looking up compose provider failed` / `podman-compose` not recognized | Run `uv tool install podman-compose` and `uv tool update-shell`, then open a **new** terminal. For the current window only: `$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"` |
| `uv sync --frozen` fails during build | `uv.lock` is out of date with `pyproject.toml`. Run `uv lock` in that service folder and rebuild. |
| `Cannot connect to Podman` | `podman machine start` |
| UI sidebar shows **Offline** | Backend is still loading/downloading the model. Check `podman compose logs -f backend`. |
| `ModuleNotFoundError: requests` in backend | `huggingface-hub` ≥ 1.0 got installed. Keep the `<1.0` pin in `backend/pyproject.toml`. |
| Backend killed / OOM with `small.en` | Raise the VM memory (`podman machine set --memory 4096`). |
| Mic button does nothing | Allow mic permission for `localhost:8501` in the browser. Use `localhost`, not a LAN IP. |
| Port already in use | Change the left side of `ports:` in `docker-compose.yml`, e.g. `"18000:8000"`. If you move the backend port, also set the frontend's `LIVE_BACKEND_PORT` to match. |
| Code changes don't show up after `up --build` | `podman-compose` rebuilds the image but keeps the running container. Add `--force-recreate`. |
| Live mode: "Cannot connect to the live backend" | The backend must be running and port 8000 published, because the browser connects to it directly. The page's origin must be in `LIVE_ALLOWED_ORIGINS`. Opening the UI as `http://<hostname>:8501` needs that origin added. |
| Live mode: "Too many live sessions" | Close other live tabs, or raise `LIVE_MAX_SESSIONS`. |
| Live text lags further and further behind | The CPU can't keep up. Use `tiny.en`/`base.en`, raise `LIVE_STEP_SECONDS`, lower `LIVE_MAX_BUFFER_SECONDS`, or give the VM more cores. |

---

## 5. Stop, restart, tear down

```powershell
podman compose restart                 # restart both services
podman compose restart backend         # restart only backend (model reloads from cache)
podman compose stop                    # stop, keep containers
podman compose start                   # start stopped containers
podman compose down                    # remove containers + network (model cache KEPT)
podman volume rm whisper-stack_hf-cache  # after `down`: delete cached models too
podman compose up --build -d --force-recreate  # rebuild + replace containers after code / pyproject / uv.lock changes
```

> `podman-compose` does **not** replace a running container just because its image was
> rebuilt. Always pair `--build` with `--force-recreate`, or run `podman compose down` first.

Restart policy is `unless-stopped`. Podman has no always-running daemon, so containers do
**not** auto-start when Windows reboots. Run `start.bat` after a reboot; it starts the Podman
machine too.

---

## 6. Configuration

### Backend (`backend` → `environment:` in `docker-compose.yml`)

| Variable | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `base.en` | `tiny.en`, `base.en`, `small.en`, `medium.en`, or multilingual `base`, `small`, … |
| `WHISPER_DEVICE` | `cpu` | Keep `cpu` (GPU passthrough on Podman/WSL needs extra setup). |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` is fastest on CPU. `int8_float32` / `float32` are slower but slightly more accurate. |
| `WHISPER_CPU_THREADS` | `0` | `0` = auto. Set to your physical core count to pin it. |
| `WHISPER_BEAM_SIZE` | `1` | Greedy decoding (fastest). `5` gives slightly better accuracy at about 2–3× the cost. |
| `WHISPER_VAD_FILTER` | `true` | Silero VAD skips silence. Faster, and avoids "hallucinated" text on silence. |
| `MAX_UPLOAD_MB` | `50` | Upload size limit. |
| `HF_HOME` | `/models` | Hugging Face cache, backed by the `hf-cache` volume. |
| `LOG_LEVEL` | `INFO` | Read from your shell/`.env`. `DEBUG` logs every live pass. |
| `LIVE_STEP_SECONDS` | `1.0` | How often the live buffer is re-transcribed. Lower is snappier but uses more CPU. |
| `LIVE_MAX_BUFFER_SECONDS` | `12` | Rolling-buffer cap. Audio before the last final word is trimmed beyond this. Higher gives more context (accuracy) but slower passes. |
| `LIVE_MAX_SESSIONS` | `2` | Concurrent live connections. Each one keeps the CPU busy. |
| `LIVE_MAX_SESSION_SECONDS` | `600` | A live session is finalized automatically after this much audio. |
| `LIVE_ALLOWED_ORIGINS` | `http://localhost:8501,http://127.0.0.1:8501` | Browser origins allowed to open the WebSocket. `*` disables the check. |

### Frontend

| Variable | Default | Notes |
|---|---|---|
| `BACKEND_URL` | `http://backend:8000` | Service-name DNS on `whisper-net`. |
| `TRANSCRIBE_TIMEOUT` | `300` | Seconds to wait for the backend. |
| `RECORDER_PAUSE_THRESHOLD` | `3.0` | Seconds of silence before recording auto-stops. |
| `RECORDER_SAMPLE_RATE` | `16000` | Whisper's native rate. Avoids resampling. |
| `LIVE_WS_URL` | *(empty)* | Full WebSocket URL the **browser** uses for live mode. Empty means `ws://<page host>:LIVE_BACKEND_PORT/ws/transcribe`. |
| `LIVE_BACKEND_PORT` | `8000` | Host port of the backend, used when `LIVE_WS_URL` is empty. |

### Switch to `small.en`

`WHISPER_MODEL` and `WHISPER_CPU_THREADS` are read from your shell or from a `.env` file next to
`docker-compose.yml`:

```powershell
# Option A: one-off
$env:WHISPER_MODEL = "small.en"; podman compose up -d --force-recreate backend

# Option B: persistent
Set-Content -Path .env -Value "WHISPER_MODEL=small.en" -Encoding ascii
podman compose up -d --force-recreate backend
```

The new model downloads once into the same cache volume (about 480 MB for `small.en`).

---

## 7. Performance tuning (CPU)

- **Model choice is the biggest lever:** `tiny.en` < `base.en` < `small.en` for both speed and accuracy.
  `base.en` int8 runs well faster than real time on a modern laptop CPU. `small.en` is noticeably
  more accurate but roughly 3× slower.
- **Give the VM all your cores** (`podman machine set --cpus N`). CTranslate2 scales with threads.
- **Keep `beam_size=1` and VAD on** for dictation latency.
- **One Uvicorn worker on purpose.** Each worker loads its own model copy into RAM. For more
  concurrency, prefer adding `num_workers=N` to `WhisperModel(...)` in `backend/app.py` over
  adding Uvicorn workers.
- **Record at 16 kHz mono** (the default here) to avoid resampling work.

### How live dictation works, and what it costs

Whisper is not a streaming model. It transcribes complete audio in windows of up to 30 s. Live
mode approximates streaming the same way `whisper_streaming` and WhisperLive do:

1. **Capture.** The browser downsamples the mic to 16 kHz int16 in an AudioWorklet and sends
   100 ms frames.
2. **Re-transcribe.** Every `LIVE_STEP_SECONDS`, the backend re-transcribes its rolling buffer
   with word timestamps. It uses greedy decoding with no temperature fallback, VAD on, and the
   preceding final text as prompt.
3. **Commit (LocalAgreement-2).** A word becomes final once two consecutive passes agree on it.
   The rest is sent as tentative text.
4. **Trim.** Once the buffer exceeds `LIVE_MAX_BUFFER_SECONDS`, audio before the last final word
   is dropped, which keeps each pass's cost bounded.
5. **Guard against hallucinations.** Each pass strips zero-length words at the buffer's end and
   an immediately repeated phrase there ("… thank you. Thank you.").

Trade-offs compared with record-then-transcribe:
- **CPU:** the same audio is transcribed several times, roughly 5–10× the CPU of one pass.
- **Accuracy:** slightly lower, since there is less context per decision.
- **Latency:** about 1–2 s.

On the test machine (8 vCPU VM, `base.en` int8), live updates arrived every ~1.1 s and the
final text about 2 s after stopping. `small.en` live needs a fast CPU.
Use `LIVE_STEP_SECONDS=1.5`–`2` if text starts to lag.

---

## 8. Modifying the code

| Want to… | Edit |
|---|---|
| Accept other formats (mp3, webm, …) | `backend/app.py`: relax `_is_wav()`. PyAV/ffmpeg decode most formats. |
| Return timestamps / segments | `backend/app.py`: build a list from `segments` (`segment.start`, `segment.end`, `segment.text`). |
| Change transcript behaviour (append vs. replace) | `frontend/app.py`: the block that sets `st.session_state.transcript`. |
| Change recorder look | `frontend/app.py`: `audio_recorder(...)` args (`recording_color`, `icon_size`, …). |
| Make live mode the default | `frontend/app.py`: `st.toggle("Live dictation", value=True, …)`. |
| Change live UI (button, colours, live text box) | `frontend/live_dictation/index.html`. It is plain HTML/JS with no build step. Rebuild the frontend image after editing. |
| Tune live commit / trimming logic | `backend/live.py` (`LiveTranscriber.process`, `_new_words`, `_trim`). |
| Add / upgrade dependencies | Use uv in the service folder (see below), then `podman compose up --build -d --force-recreate`. |

### Managing dependencies with uv

Each service is its own uv project (`pyproject.toml` + `uv.lock`). The Containerfiles copy uv
from `ghcr.io/astral-sh/uv` and run `uv sync --frozen`. The image therefore installs exactly what
is in `uv.lock`, and the build fails if the lock is stale.

```powershell
cd backend                          # or: cd frontend
uv add numpy                        # add a dependency (updates pyproject.toml + uv.lock)
uv remove numpy                     # remove one
uv lock --upgrade-package fastapi   # bump a single package within its constraints
uv lock --upgrade                   # bump everything within constraints
uv tree                             # inspect the resolved dependency tree
cd ..; podman compose up --build -d --force-recreate  # rebuild images with the new lock
```

Always commit `pyproject.toml` and `uv.lock` together.

### Running a service outside containers (development)

uv creates `.venv` and fetches Python 3.11 if needed:

```powershell
cd backend
uv sync
uv run uvicorn app:app --reload --port 8000 --ws wsproto

cd ..\frontend
$env:BACKEND_URL = "http://localhost:8000"
uv sync
uv run streamlit run app.py
```

---

## 9. Security notes

- Both containers run as a non-root user (`uid 10001`) in rootless Podman.
- Ports are published on all host interfaces (`0.0.0.0`). To restrict to this PC only, change
  them to `"127.0.0.1:8000:8000"` and `"127.0.0.1:8501:8501"`.
- There is no authentication. Do not expose these ports to untrusted networks.
- The live WebSocket checks the browser `Origin` against `LIVE_ALLOWED_ORIGINS`, so other
  websites open in your browser can't stream to it. Clients that send no Origin header
  (scripts, curl) are allowed, as they are for `POST /transcribe`.
- uvicorn uses the `wsproto` WebSocket implementation (`--ws wsproto`). The pinned uvicorn
  0.32 relies on an older API of the `websockets` library, and the locked `websockets` 17.x
  may not provide it.
