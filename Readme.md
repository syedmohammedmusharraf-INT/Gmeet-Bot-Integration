# 🤖 INT Avatar Interview Bot

An AI-powered bot that joins Google Meet as host, conducts real-time voice interviews, and sees the candidate's screen share in real time. Now featuring **Silent Meeting Recording** to capture audio from any existing meeting.

---

## 🧠 How It Works

### Voice Pipeline (Interview Mode)
```
Candidate speaks in Meet
        ↓
Chrome captures audio → VSpk_{sid} (PulseAudio, per-session isolated)
        ↓
GPT Realtime API (STT + LLM combined, semantic VAD)
        ↓
Cartesia Sonic-3 TTS → paplay → VMic_{sid} → Chrome mic → Meet
        ↓
Candidate hears Alex respond in ~0.8–1.5s
```

### Silent Recording Mode (New!)
```
Any participant speaks in Meet
        ↓
Chrome (Silent Bot) captures all mixed output audio
        ↓
sounddevice callback → high-fidelity PCM buffer
        ↓
ffmpeg real-time encoding (Opus in WebM)
        ↓
Saved to /app/recordings/{session_id}.webm
```

### Vision Pipeline (Interview Mode)
```
Playwright page.screenshot() every 1.0s
        ↓
Perceptual hash diff (aHash 8×8) — skip unchanged frames
        ↓
GPT-4o-mini vision → { summary, screen_type, key_entities, confidence }
        ↓
Significant change? → Tier 1: conversation history entry | ALL → Tier 2: background context update
```

---

## ⚡ Tech Stack

| Layer | Technology |
|-------|-----------|
| Browser automation | Playwright (Chromium, headless=False) |
| STT + LLM | OpenAI GPT Realtime API (`gpt-4o-mini-realtime-preview`) |
| TTS | Cartesia Sonic-3 (WAV PCM → paplay) |
| Audio Encoding | ffmpeg + libopus (for silent recording) |
| VAD | Semantic VAD (fires on sentence completion, not silence) |
| Barge-in | Client RMS threshold + OpenAI `interrupt_response=True` |
| Screen vision | GPT-4o-mini vision + Playwright screenshot + perceptual hash diff |
| API | FastAPI + Uvicorn |
| Frontend | React + Vite + shadcn/ui |
| Web server | Nginx (reverse proxy) |
| Audio routing | PulseAudio virtual cables inside Docker (per-session isolated) |

---

## 📁 Project Structure

```
INT-interview-Bot/
├── Backend/
│   ├── api.py              FastAPI — sessions, routing, recording endpoints
│   ├── main.py             Per-session orchestrator (Interview Mode)
│   ├── silent_recorder.py  NEW: Standalone engine for silent meeting recording
│   ├── join_meet.py        Playwright — joins Meet, handles UI automation
│   ├── realtime.py         GPT Realtime WebSocket (Interview Mode)
│   ├── llm_tts.py          Cartesia TTS + barge-in interrupt
│   ├── vision_worker.py    Async vision worker (Interview Mode)
│   ├── setup_login.py      ONE-TIME: Google login setup via noVNC
│   ├── Dockerfile          python:3.11-slim-bookworm + Chrome + PulseAudio + ffmpeg
│   └── start.sh            Container startup: Xvfb → noVNC → PulseAudio → FastAPI
├── Frontend/
│   ├── src/pages/Index.tsx   Main UI — tabbed interface for Interview vs Recording
│   └── dist/                 Built output served by Nginx
├── recordings/             Host directory for saved .webm files
├── docker-compose.yaml
└── Readme.md
```

---

## 🔐 How Google Authentication Works

The bot does **not** store your password in code. Instead, it uses a **Persistent Chrome Profile** strategy:

1.  **Manual Login**: You run a special setup script (`setup_login.py`) inside the container.
2.  **VNC Access**: This script opens a real Chrome window on a virtual desktop. You connect via your browser (port 4013).
3.  **Session Saving**: You log into Gmail manually once. Google's session cookies and authentication tokens are saved to a dedicated folder (`/tmp/chrome-profile`).
4.  **Bot Reuse**: Every time the bot joins a meeting, it copies this "Base Profile". Google sees a logged-in browser, allowing the bot to join as a host or recognized user without being blocked.

---

## 🚀 Setup & Recording Guide

### 1. Prerequisites
- Docker + Docker Compose
- Node.js 18+ (for frontend build)

### 2. Configure Environment
Create `Backend/.env` with your OpenAI and Cartesia keys.

### 3. Build & Start
```bash
# Build frontend
cd Frontend && npm install && npm run build && cd ..

# Start containers
docker compose up --build -d
```

### 4. Perform One-Time Login (Crucial)
```bash
# Run the setup script
docker exec -it int-avatar-bot python /app/setup_login.py

# 1. Open http://localhost:4013/vnc.html
# 2. Click "Connect" (no password)
# 3. Log into your Google Account in the browser window
# 4. Once logged in, the script will detect it and save your profile
```

### 5. Record a Meeting
1.  Open the UI at **http://localhost:4011**.
2.  Click the **"Join & Record"** tab.
3.  **Paste the Meet Link**: E.g., `https://meet.google.com/abc-defg-hij`.
4.  **Set Duration**: Choose how long the bot should stay.
5.  **Click "Join & Record Silently"**:
    -   The bot will launch an invisible browser.
    -   It will join the meeting silently (mic muted).
    -   It will capture all meeting audio (everyone speaking).
    -   The audio is encoded to Opus/WebM in real-time.
6.  **Stop & Download**:
    -   Click **"Stop Session"** when done.
    -   The recording will appear in the **"Recent Recordings"** list.
    -   Click **"Download"** to get your `.webm` file.

---

## 🤖 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/create-meeting` | Create Google Meet (bot = host) |
| POST | `/start` | Start AI Interviewer session |
| POST | `/join-silent` | Start Silent Recorder session |
| GET | `/status/{id}` | Status of a specific session |
| POST | `/stop/{id}` | Stop a specific session (saves recording) |
| GET | `/recordings` | List all available .webm files |
| GET | `/recordings/{file}` | Download a recording |

---

## 🛠️ Useful Commands

```bash
# View real-time audio/join logs
docker logs -f int-avatar-bot

# Check virtual audio sinks
docker exec int-avatar-bot pactl list sinks short

# Access recordings directly on host
ls ./recordings
```
