# INT Meeting Recorder — Step-by-Step Guide

## What This Does

This bot joins a Google Meet link and silently records all participant audio to a `.webm` file. It runs in a Docker container with a virtual display (Xvfb), virtual audio routing (PulseAudio), and a Chrome browser automated by Playwright.

---

## 1. Project Structure

```
INT-interview-Bot/
├── .env                          # Single config file
├── docker-compose.yaml           # Container orchestration
├── nginx.conf                    # Reverse proxy (optional)
├── app/
│   ├── backend/                  # API + recorder
│   │   ├── main.py               # FastAPI entry point
│   │   ├── routes/sessions.py    # API endpoints
│   │   ├── models/schemas.py     # Request/response models
│   │   ├── join_meet.py          # Playwright Meet automation
│   │   ├── silent_recorder.py    # Audio capture + WebM encoding
│   │   ├── requirements.txt      # Python dependencies
│   │   ├── Dockerfile            # Container build
│   │   └── start.sh              # Container startup script
│   └── frontend/                 # React + Vite UI
│       ├── src/pages/Index.tsx   # Main UI (record-only)
│       └── ...
├── recordings/                   # Output .webm files (mounted volume)
├── chrome-profile/               # Chrome session data (mounted volume)
└── chrome-profile-backup/        # Backup Chrome profile
```

---

## 2. Prerequisites

- **Docker** and **Docker Compose** installed
- A **Google Meet link** you want to record
- A **Google account** that has access to that meeting (the bot will use a Chrome profile with this account logged in)

---

## 3. Configuration

### `.env` — All settings in one file

```
STAY_DURATION_SECONDS=7200
```

| Variable | Default | Description |
|---|---|---|
| `STAY_DURATION_SECONDS` | `7200` | How long the bot stays in the meeting (seconds). Also configurable per-request via the API. |

That's it — the recorder needs no API keys.

### Environment Variables (set in `docker-compose.yaml`)

These are set automatically and should not be changed unless you know what you're doing:

| Variable | Value | Purpose |
|---|---|---|
| `DISPLAY` | `:99` | Virtual display for headless Chrome |
| `PULSE_SERVER` | `unix:/var/run/pulse/native` | PulseAudio socket for audio routing |
| `PULSE_SINK` | `VirtualMic` | Default audio sink |
| `PULSE_SOURCE` | `VirtualMicSource` | Default audio source |

---

## 4. Setting Up the Chrome Profile (One-Time)

The bot needs a Chrome profile logged into a Google account so it can join meetings.

### Step 4.1: Build and start the container

```bash
docker compose up -d --build
```

This starts the container with Xvfb, PulseAudio, Chrome, and the FastAPI server.

### Step 4.2: Access noVNC (visual browser)

Open in your browser:

```
http://localhost:4014/vnc.html
```

You will see a virtual desktop with a Chrome window.

### Step 4.3: Log in to Google

1. In the noVNC browser, navigate to `https://accounts.google.com`
2. Sign in with the Google account that has access to your target meetings
3. Navigate to `https://meet.google.com` to verify access
4. The profile is now saved to `/tmp/chrome-profile` inside the container

### Step 4.4: Let the profile persist

The `chrome-profile/` directory is mounted from your host to `/tmp/chrome-profile` in the container. As long as `chrome-profile/` exists on your host, the login persists across container restarts.

> **Note:** The container's `start.sh` pre-creates `/tmp/chrome-profile` with microphone permissions pre-granted for `meet.google.com`. You still need to log in to a Google account once.

---

## 5. Running the Recorder

### Via the Web UI

Open `http://localhost:4000` (or wherever your frontend is served).

1. Paste a Google Meet link
2. Set the duration
3. Click **"Join & Record Silently"**
4. Click **"Stop Session"** to end early
5. Download recordings from the list

### Via the API directly

```bash
# Start recording
curl -X POST http://localhost:4000/join-silent \
  -H "Content-Type: application/json" \
  -d '{"meet_link": "https://meet.google.com/abc-defg-hij", "duration_minutes": 60}'

# Response:
# {"session_id":"uuid-here","status":"recording","meetLink":"https://meet.google.com/abc-defg-hij"}

# Check status
curl http://localhost:4000/status/{session_id}

# List active sessions
curl http://localhost:4000/sessions

# Stop early
curl -X POST http://localhost:4000/stop/{session_id}

# List recordings
curl http://localhost:4000/recordings

# Download a recording
curl http://localhost:4000/recordings/{filename}.webm --output recording.webm

# Health check
curl http://localhost:4000/health
```

---

## 6. API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/join-silent` | Start recording a meeting |
| `GET` | `/status/{session_id}` | Get session status |
| `POST` | `/stop/{session_id}` | Stop a recording session |
| `GET` | `/sessions` | List all active sessions |
| `GET` | `/recordings` | List all recordings |
| `GET` | `/recordings/{filename}` | Download a recording file |
| `GET` | `/health` | Health check |

### `POST /join-silent`

**Request body:**
```json
{
  "meet_link": "https://meet.google.com/abc-defg-hij",
  "duration_minutes": 60
}
```

**Response:**
```json
{
  "session_id": "a1b2c3d4-...",
  "status": "recording",
  "meetLink": "https://meet.google.com/abc-defg-hij"
}
```

The bot creates:
- An **isolated Chrome profile** at `/tmp/chrome-profile-{session_id}` (copied from the base profile)
- A **per-session PulseAudio sink** `VSpk_{short_id}` with its monitor source for audio capture

It then spawns `silent_recorder.py` as a subprocess, which:
1. Starts Playwright → joins the Meet
2. Starts `sounddevice` audio capture from `VSpk_{sid}.monitor`
3. Every 5 seconds, encodes buffered PCM → Opus-in-WebM via `ffmpeg`
4. Appends the WebM data to `/app/recordings/{session_id[:8]}.webm`

---

## 7. Docker Commands

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f

# Stop the container
docker compose down

# Execute commands inside the container
docker exec -it int-avatar-bot bash

# View recordings inside the container
docker exec int-avatar-bot ls -la /app/recordings/

# Manually run the recorder
docker exec -it int-avatar-bot python /app/silent_recorder.py
```

---

## 8. Architecture — How Recording Works

```
┌─────────────────────────────────────────────────────────────┐
│                     Docker Container                         │
│                                                              │
│  start.sh                                                    │
│   ├── Xvfb :99          (virtual display for Chrome)         │
│   ├── x11vnc + noVNC    (VNC access on port 6080 → 4014)    │
│   ├── PulseAudio        (audio routing daemon)               │
│   ├── VirtualMic / VirtualSpeaker / VirtualMicSource         │
│   └── uvicorn main:app  (FastAPI on port 8000 → 4000)        │
│         │                                                     │
│         └── POST /join-silent                                 │
│               └── Subprocess: silent_recorder.py              │
│                     ├── Playwright Chrome → joins Meet        │
│                     ├── sounddevice ← captures from           │
│                     │   PulseAudio monitor sink               │
│                     └── ffmpeg → encodes to Opus/WebM         │
│                                                              │
│  Audio path:                                                  │
│    Meet participants → Chrome → PulseAudio spk_sink           │
│    → .monitor → sounddevice → PCM buffer → ffmpeg → .webm    │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Audio Pipeline

1. **Chrome plays all Meet audio** to the per-session PulseAudio sink `VSpk_{sid}`
2. **sounddevice** captures from `VSpk_{sid}.monitor` (the monitor interface of that sink)
3. Raw PCM floats are buffered in memory with a thread-safe lock
4. Every **5 seconds**, a flush thread:
   - Takes the accumulated buffer
   - Resamples to 48kHz (Opus native rate)
   - Runs `ffmpeg` to encode as Opus-in-WebM (`-application voip` for voice optimization)
   - Appends to the `.webm` file on disk

### Session Isolation

Each `POST /join-silent` creates:
- A unique session ID (UUID)
- An isolated Chrome profile directory (copied from base profile)
- A per-session PulseAudio sink named `VSpk_{short_id}`
- A separate subprocess

Multiple recordings can run simultaneously without interfering.

---

## 9. File Details

| File | Purpose |
|---|---|
| `main.py` | FastAPI app with CORS, mounts the router |
| `routes/sessions.py` | All REST endpoints: start, stop, status, list, download |
| `models/schemas.py` | Pydantic models for request/response validation |
| `join_meet.py` | Playwright automation: navigates to Meet, clicks mic/join/leave buttons, handles login prompts |
| `silent_recorder.py` | Async main loop: starts audio capture + flush thread + awaits `run_meet()` |
| `start.sh` | Container entry point: Xvfb → noVNC → PulseAudio → virtual cables → Chrome profile → uvicorn |
| `Dockerfile` | Python 3.11 slim + X11 + PulseAudio + Chrome + Playwright |
| `requirements.txt` | `fastapi`, `uvicorn`, `python-dotenv`, `pydantic`, `playwright`, `numpy`, `sounddevice` |

---

## 10. Troubleshooting

### "No MEETING_LINK provided"
The `MEETING_LINK` environment variable is not set. You must provide it via the API `POST /join-silent` request body.

### Chrome does not join the meeting
1. Check noVNC at `http://localhost:4014/vnc.html` to see what Chrome is doing
2. The base Chrome profile at `/tmp/chrome-profile` must be logged into a Google account
3. Run `docker exec -it int-avatar-bot python /app/setup_login.py` to redo the login (in the full version)
4. Check container logs: `docker compose logs -f`

### No audio in the recording
1. Verify PulseAudio is running: `docker exec int-avatar-bot pactl info`
2. List available sinks: `docker exec int-avatar-bot pactl list sinks short`
3. Check that the per-session sink was created (should appear as `VSpk_{short_id}`)
4. Verify Chrome is outputting audio to the correct sink

### The `.webm` file is empty
1. Ensure the recording duration is long enough (> 10 seconds)
2. Check if the Chrome page was able to load the Meet
3. Check for ffmpeg errors in the container logs

### Container won't start
1. Ensure `chrome-profile/` and `recordings/` directories exist: `mkdir -p chrome-profile recordings`
2. Check Docker logs: `docker compose logs`
