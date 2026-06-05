# Silent Meet Recorder — Architecture Diagrams

> All diagrams for the system described in `JOIN_EXISTING_MEET_AND_RECORD.md`

---

## 1. System Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        SILENT MEET RECORDER — ARCHITECTURE                   │
│                                                                              │
│  ┌──────────┐    ┌──────────────┐    ┌────────────────┐    ┌──────────────┐  │
│  │  Browser  │───▶│   Nginx      │───▶│   FastAPI      │───▶│  Subprocess  │  │
│  │ (React)   │    │ (Reverse     │    │   (api.py)     │    │  (recorder)  │  │
│  │           │◀───│  Proxy)      │◀───│                │◀───│              │  │
│  └──────────┘    └──────────────┘    └────────────────┘    └──────────────┘  │
│                                          │                                    │
│                                          ▼                                    │
│                                   ┌────────────────┐                         │
│                                   │  Playwright     │  Chrome + PulseAudio    │
│                                   │  (join_meet.py) │  in Docker container    │
│                                   └────────────────┘                         │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. User Flow: Entry Point

```
User
  │
  ├─ Opens http://host:4011
  ├─ Pastes existing Meet URL: https://meet.google.com/abc-defg-hij
  ├─ Selects duration: 60 min
  └─ Clicks "Join & Record Silently"
       │
       ▼
  POST /join-silent { meet_link, duration_minutes }
       │
       ▼
  FastAPI (api.py)
```

---

## 3. API Layer: join_silent() Internals

```
join_silent()
  │
  ├─ Generate UUID session_id
  ├─ Build env dict:
  │    SESSION_ID           → UUID
  │    MEETING_LINK         → "https://meet.google.com/abc-defg-hij"
  │    CHROME_USER_DATA_DIR → "/tmp/chrome-profile-{session_id}"
  │    PULSE_SPK_SINK       → "VSpk_{sid8}"
  │    PULSE_SOURCE         → "VSpk_{sid8}.monitor"
  │    PULSE_SINK           → "VSpk_{sid8}"
  │    STAY_DURATION_SECONDS → duration + 5min buffer (capped at 10800)
  │
  ├─ Spawn subprocess: silent_recorder.py
  ├─ Attach stdout logger (threaded, prefix with short_id)
  ├─ Store in active_sessions dict
  └─ Return session_id
```

---

## 4. Lifecycle API Endpoints

```
GET  /status/{session_id}     → "running" | "idle" | "not_found"
POST /stop/{session_id}       → terminates subprocess (SIGTERM → SIGKILL)
GET  /sessions                → list all active sessions
GET  /recordings              → list all .webm files on disk
GET  /recordings/{filename}   → download .webm file
```

---

## 5. Process Tree (Docker Container)

```
Docker container (int-avatar-bot)
  │
  ├── PID 1: start.sh
  │     ├── Xvfb :99 (virtual display)
  │     ├── x11vnc + websockify → noVNC (port 6080)
  │     ├── pulseaudio --system (PulseAudio daemon)
  │     └── uvicorn api:app --port 8000 (FastAPI)
  │            │
  │            └── PID N: silent_recorder.py (subprocess per session)
  │                  │
  │                  ├── Thread 1: flush_loop (WebM writer)
  │                  ├── Thread 2: sounddevice callback (audio capture)
  │                  └── Main coroutine: run_meet (Playwright)
  │                        │
  │                        └── Chrome browser instance (headless=False)
  │                              └── Google Meet tab
```

---

## 6. Per-Session Process Isolation

```
Session A:                   Session B:
  PID 1001                     PID 1101
  Chrome profile:              Chrome profile:
    /tmp/chrome-profile-A        /tmp/chrome-profile-B
  PulseAudio sink:              PulseAudio sink:
    VSpk_A                       VSpk_B
  Output file:                  Output file:
    /app/recordings/A.webm       /app/recordings/B.webm
```

---

## 7. Chrome Profile Hierarchy

```
/tmp/chrome-profile              ← Base profile (created by setup_login.py)
  └── Default/
       └── Preferences            ← Mic pre-grant, Google session cookies

/tmp/chrome-profile-{session_id} ← Per-session copy (silent_recorder.py)
  └── Default/
       └── Preferences            ← Same content, isolated copy

/tmp/chrome-profile-backup       ← Fallback if base is corrupted
```

---

## 8. Chrome Profile Setup Flow

```
setup_login.py (one-time, via docker exec):
  1. Launch Chrome via Playwright with base profile
  2. Open noVNC at http://host:4013/vnc.html
  3. User manually logs into Google account
  4. Script detects login completion → closes browser
  5. Chrome profile saved at /tmp/chrome-profile

Per session (automatic):
  1. cp -r /tmp/chrome-profile → /tmp/chrome-profile-{session_id}
  2. Remove lock files (SingletonLock, SingletonCookie, SingletonSocket)
  3. Pass as user_data_dir to Playwright
```

---

## 9. PulseAudio Audio Routing

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      PULSEAUDIO AUDIO ROUTING                          │
│                                                                         │
│  ┌──────────────────────────────────────────────┐                      │
│  │           Chrome (Playwright)                 │                      │
│  │                                                │                      │
│  │  Audio OUT → PulseAudio sink "VSpk_{sid}"     │                      │
│  │  Audio IN  → (not used — bot is silent)       │                      │
│  └──────────────────┬───────────────────────────┘                      │
│                     │                                                   │
│                     ▼                                                   │
│  ┌────────────────────────────────────┐                                │
│  │  VSpk_{sid} (module-null-sink)     │  ← Chrome writes all          │
│  │  "Virtual Speaker" sink            │     meeting participants'      │
│  │                                    │     audio here                 │
│  └──────────────┬─────────────────────┘                                │
│                 │ .monitor                                              │
│                 ▼                                                       │
│  ┌────────────────────────────────────┐                                │
│  │  sounddevice InputStream            │  ← Python reads the           │
│  │  @ 44100Hz, float32, mono          │     monitor source            │
│  │  device_index=0                     │                                │
│  └──────────────┬─────────────────────┘                                │
│                 │                                                       │
│                 ▼                                                       │
│  ┌────────────────────────────────────┐                                │
│  │  numpy ring buffer (deque)          │  ← Thread-safe buffer         │
│  │  audio_callback() appends chunks    │     of float32 arrays         │
│  └────────────────────────────────────┘                                │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 10. Google Meet Audio Flow (Why This Works)

```
Google Meet's audio architecture:

  ┌──────────┐    ┌──────────┐    ┌──────────┐
  │ Person A │───▶│  Google  │───▶│ Person B │
  └──────────┘    │  Meet    │    └──────────┘
  ┌──────────┐    │  Server  │    ┌──────────┐
  │ Person C │───▶│          │───▶│  Chrome  │
  └──────────┘    └──────────┘    │  (Bot)   │
                                  └────┬─────┘
                                       │ Audio OUT
                                       ▼
                                  VSpk.monitor
                                       │
                                       ▼
                                  .webm file

Chrome plays the mixed audio of ALL participants (minus its own mic input).
Since the bot's mic is silent, Chrome's output IS the full meeting audio.
No separate mixing needed.
```

---

## 11. Audio Capture & Encoding Pipeline

```
Audio frame (30ms)
  │
  ▼
sounddevice callback (audio thread)
  │
  ├─ mono = np.mean(indata, axis=1) → float32 @ 44100Hz
  ├─ with buffer_lock: audio_buffer.append(mono)
  └─ callback returns immediately (<1ms latency)
       │
       ▼  (every 1.0s, flush thread)
flush_loop()
  │
  ├─ with buffer_lock:
  │     chunks = list(audio_buffer)
  │     audio_buffer.clear()
  │
  ├─ if chunks:
  │     combined = np.concatenate(chunks)   ← float32 @ 44100Hz
  │
  ├─ resample(combined, 44100 → 48000)      ← Opus native rate
  │     new_len = len * 48000 / 44100
  │     np.interp() → float32 @ 48000Hz
  │
  ├─ encode_to_webm(resampled, 48000)
  │     │
  │     ├─ clip(-1.0, 1.0) → *32767 → int16 PCM bytes
  │     ├─ write to temp .pcm file
  │     ├─ ffmpeg -f s16le -ar 48000 -ac 1 -i input.pcm
  │     │         -c:a libopus -b:a 32k -application voip output.webm
  │     ├─ read output.webm → bytes
  │     └─ delete temp files
  │
  └─ append bytes to /app/recordings/{sid8}.webm
```

---

## 12. Timing Guarantees

```
┌─────────────┬──────────────────────┬────────────────────────────────┐
│ Component   │ Latency / Interval   │ Guarantee                      │
├─────────────┼──────────────────────┼────────────────────────────────┤
│ Audio       │ 30ms per frame       │ Blocksize = 44100 * 0.03 ≈    │
│ callback    │                      │ 1323 samples/frame             │
├─────────────┼──────────────────────┼────────────────────────────────┤
│ Buffer      │ Real-time insert     │ deque append is O(1), lock     │
│ write       │ < 0.1ms              │ held for microseconds          │
├─────────────┼──────────────────────┼────────────────────────────────┤
│ Flush loop  │ Every 1.0s ± 0.1s   │ time.sleep(0.1) polling loop   │
├─────────────┼──────────────────────┼────────────────────────────────┤
│ ffmpeg      │ ~200-500ms per       │ Subprocess per 1s chunk; max  │
│ encode      │ 1s of audio          │ 15s timeout enforced           │
├─────────────┼──────────────────────┼────────────────────────────────┤
│ File append │ O(segment_size)      │ Append to existing WebM;      │
│             │                      │ no seek, no rewrite           │
└─────────────┴──────────────────────┴────────────────────────────────┘

Maximum end-to-end audio loss: ~30ms (one callback frame) if flush races
with buffer write. Statistical loss < 0.1%.
```

---

## 13. Playwright Automation — Meet Join Sequence

```
silent_recorder.py
  │
  ├─ async_playwright() as p
  │    │
  │    ├─ p.chromium.launch_persistent_context()
  │    │    user_data_dir = /tmp/chrome-profile-{session_id}
  │    │    headless = False       ← Required for audio (Chrome restriction)
  │    │    args = [
  │    │      "--no-sandbox",
  │    │      "--disable-dev-shm-usage",
  │    │      "--alsa-output-device=pulse",   ← Audio via PulseAudio
  │    │      "--window-size=1280,720",
  │    │    ]
  │    │
  │    ├─ context.new_page()
  │    │
  │    ├─ page.goto(meeting_link)
  │    │    wait_until = "domcontentloaded"
  │    │    timeout = 30s
  │    │
  │    ├─ Detect login: _google_login_required(page)
  │    │    URL contains accounts.google.com? → yes → wait for manual login
  │    │    email/password inputs on page?    → yes → wait for manual login
  │    │    body text hints ("sign in", "2-step")? → yes → wait
  │    │
  │    ├─ Click sequence:
  │    │    Step 1: _click_switch_here()
  │    │        "[Switch here]" or "[Switch the call here]"
  │    │        4 attempts × 1.5s apart
  │    │
  │    │    Step 2: _click_join_button()
  │    │        "[Ask to join]" or "[Join now]"
  │    │        10 attempts × 2s apart
  │    │
  │    │    Step 3: _wait_for_admission()
  │    │        Polls for mic state presence
  │    │        120s timeout × 2s poll interval
  │    │        Detects admission when aria-label contains
  │    │        "Turn off microphone" or "Turn on microphone"
  │    │
  │    │    Step 4: _dismiss_popups()
  │    │        Close any "New layout" or "Try features" dialogs
  │    │
  │    ├─ Set joined_event.set()
  │    │
  │    └─ Loop: await asyncio.sleep(5) until
  │         CancelledError or stay_duration exceeded
  │
  └─ Finally:
       ├─ _click_leave_button()
       │   aria-label="Leave call"
       └─ context.close()
```

---

## 14. UI Element Detection Strategy

```
All click operations use page.evaluate() to scan DOM directly
(rather than Playwright locators), for reliability:

_evaluate(`() => {
  const buttons = Array.from(document.querySelectorAll('button'));
  for (const btn of buttons) {
    const txt = (btn.textContent || '').trim().toLowerCase();
    if (txt === 'ask to join') { btn.click(); return 'Ask to join'; }
    if (txt === 'join now')    { btn.click(); return 'Join now'; }
  }
  return null;
}`)
```

---

## 15. WebM Container Structure

```
WebM file = EBML header + Segment(Cluster*)
  │
  ├── EBML header (fixed, 5-10 bytes)
  │     ├── DocType = "webm"
  │     └── DocTypeVersion = 4
  │
  ├── Segment Info
  │     ├── TimecodeScale
  │     └── MuxingApp / WritingApp
  │
  ├── Tracks
  │     └── TrackEntry
  │           ├── TrackNumber = 1
  │           ├── TrackType = 2 (audio)
  │           ├── CodecID = "A_OPUS"
  │           ├── Audio:
  │           │     ├── SamplingFrequency = 48000
  │           │     ├── Channels = 1
  │           │     └── BitDepth = 32 (float, opus)
  │           └── DefaultDuration = 20000000 (20ms opus frames)
  │
  └── Cluster[]
        ├── Cluster 1: Timecode=0,   Block(opus packets for 0-1s)
        ├── Cluster 2: Timecode=1000, Block(opus packets for 1-2s)
        ├── Cluster 3: Timecode=2000, Block(opus packets for 2-3s)
        └── ...

NOTE: Each 1s flush appends a new Cluster. WebM supports this.
The file is playable even if truncated mid-write (last cluster may be
partial but prior clusters are valid).
```

---

## 16. ffmpeg Encoding Parameters

```
ffmpeg -y \
  -f s16le          ← Raw PCM input (signed 16-bit little-endian)
  -ar 48000         ← Sample rate (48kHz, Opus native)
  -ac 1             ← Mono
  -i input.pcm      ← Temp PCM file
  -c:a libopus      ← Opus codec via libopus
  -b:a 32k          ← Target bitrate (32 kbps — good for speech)
  -application voip ← Optimize for voice (lower latency than audio)
  output.webm       ← Temp output file (then read back as bytes)
```

---

## 17. Quality vs Size Tradeoffs

```
Bitrate   Quality    1 hour size   Use case
─────────────────────────────────────────────────
16 kbps   Fair       7 MB          Low-bandwidth, speech only
32 kbps   Good      14 MB          DEFAULT — speech, clear
48 kbps   Very Good  21 MB         Speech + music
64 kbps   Excellent  28 MB         Highest quality
128 kbps  Overkill   56 MB         Not needed for meetings
```

---

## 18. Docker Container Layout

```
docker-compose.yaml
  │
  ├── service: backend
  │     build: ./Backend
  │     image: int-avatar-bot (python:3.11-slim base)
  │     ports:
  │       "4012:8000"    ← FastAPI
  │       "4013:6080"    ← noVNC (debug)
  │     volumes:
  │       ./recordings:/app/recordings    ← WebM output persisted
  │     env_file: ./Backend/.env
  │     privileged: true                  ← PulseAudio needs it
  │     shm_size: "2gb"                  ← Chrome needs shared memory
  │     ipc: host                         ← Chrome needs /dev/shm
  │
  └── service: frontend
        image: nginx:alpine
        ports: "4011:80"
        volumes:
          ./Frontend/dist:/usr/share/nginx/html:ro
          ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
```

---

## 19. Docker Image Contents

```
python:3.11-slim base
  │
  ├── System packages:
  │     xvfb              ← Virtual framebuffer
  │     pulseaudio        ← Audio routing
  │     pulseaudio-utils  ← pactl command
  │     chromium          ← Browser (via playwright deps)
  │     ffmpeg            ← WebM encoding
  │     libopus0          ← Opus codec
  │     x11vnc            ← VNC server
  │     novnc             ← Web VNC client
  │     websockify        ← WebSocket proxy for VNC
  │
  ├── Python packages (pip):
  │     playwright        ← Browser automation
  │     numpy             ← Audio buffer math
  │     sounddevice       ← PulseAudio capture
  │     fastapi, uvicorn  ← API server
  │     python-dotenv     ← .env loading
  │
  ├── Application files:
  │     /app/api.py
  │     /app/silent_recorder.py
  │     /app/join_meet.py
  │     /app/meet_creator.py
  │     /app/setup_login.py
  │     /app/start.sh
  │
  └── Entry point: /app/start.sh
```

---

## 20. Nginx Reverse Proxy

```
Client → :4011 → Nginx
  ├── / → serves Frontend/dist (static files)
  └── /api/* → proxy_pass backend:8000
        proxy_read_timeout 120s
        proxy_connect_timeout 10s
```

---

## 21. Complete End-to-End Sequence

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  Browser  │     │  Nginx   │     │ FastAPI  │     │silent_   │     │  Chrome  │
│  (React)  │     │  :4011   │     │ :8000    │     │recorder  │     │(Playwr.) │
└─────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘
      │                │                │                │                │
      │ 1. Join &      │                │                │                │
      │    Record      │                │                │                │
      │ ──────────────▶│                │                │                │
      │                │ 2. POST        │                │                │
      │                │    /join-silent│                │                │
      │                │ ──────────────▶│                │                │
      │                │                │                │                │
      │                │                │ 3. Spawn       │                │
      │                │                │    subprocess  │                │
      │                │                │ ──────────────▶│                │
      │                │                │                │                │
      │                │                │                │ 4. Copy Chrome │
      │                │                │                │    profile     │
      │                │                │                │ ──────────────▶│
      │                │                │                │◀───────────────│
      │                │                │                │                │
      │                │                │                │ 5. Create      │
      │                │                │                │    VSpk sink   │
      │                │                │                │ (pactl)        │
      │                │                │                │ ──────────────▶│
      │                │                │                │◀───────────────│
      │                │                │                │                │
      │                │                │                │ 6. Launch      │
      │                │                │                │    Playwright  │
      │                │                │                │    Chromium    │
      │                │                │                │ ──────────────▶│
      │                │                │                │                │
      │                │                │                │ 7. Navigate to │
      │                │                │                │    Meet URL    │
      │                │                │                │ ──────────────▶│
      │                │                │                │                │
      │                │                │                │ 8. Check login │
      │                │                │                │ ◀──────────────│
      │                │                │                │                │
      │                │                │                │ 9. Click       │
      │                │                │                │    "Ask to    │
      │                │                │                │     join"      │
      │                │                │                │ ──────────────▶│
      │                │                │                │                │
      │                │                │                │10. Poll for    │
      │                │                │                │    admission   │
      │ 11. (Host admits bot via Meet UI)                │◀───────────────│
      │                │                │                │                │
      │                │                │                │12. Set         │
      │                │                │                │    joined_event│
      │                │                │                │    .set()      │
      │                │                │                │                │
      │                │                │                │13. Start audio │
      │                │                │                │    capture     │
      │                │                │                │    (sounddevice)│
      │                │                │                │ ──────────────▶│
      │                │                │                │◀───────────────│
      │                │                │                │  VSpk.monitor  │
      │                │                │                │  audio frames  │
      │                │                │                │  (every 30ms)  │
      │                │                │                │                │
      │                │                │                │14. Flush loop  │
      │                │                │                │    every 1.0s  │
      │                │                │                │    encode→.webm│
      │                │                │                │                │
      │                │                │                │   ... recording │
      │                │                │                │   ...          │
      │                │                │                │   ...          │
      │                │                │                │                │
      │15. POST /stop  │                │                │                │
      │ ──────────────▶│ ───────────────▶ ───────────────▶                │
      │                │                │                │                │
      │                │                │                │16. Cancelled   │
      │                │                │                │    Error       │
      │                │                │                │                │
      │                │                │                │17. Stop audio  │
      │                │                │                │    capture     │
      │                │                │                │                │
      │                │                │                │18. Final flush │
      │                │                │                │    + save .webm│
      │                │                │                │                │
      │                │                │                │19. Destroy     │
      │                │                │                │    VSpk sink   │
      │                │                │                │                │
      │                │                │                │20. Close Chrome│
      │                │                │                │    + context   │
      │                │                │                │                │
      │                │                │                │21. Process exit│
      │                │                │                │                │
      │                │                │◀───────────────│                │
      │                │◀───────────────│                │                │
      │◀───────────────│                │                │                │
      │                │                │                │                │
      │22. GET         │                │                │                │
      │    /recordings │                │                │                │
      │ ──────────────▶│ ───────────────▶                │                │
      │◀───────────────│◀───────────────│                │                │
      │                │                │                │                │
      │23. Download    │                │                │                │
      │    meeting.webm│                │                │                │
      │ ──────────────▶│ ───────────────▶                │                │
      │◀──.webm file──│◀───────────────│                │                │
```

---

## 22. Failure Modes & Recovery

```
┌──────────────────────────┬──────────────────────────┬──────────────────────────┐
│ Failure                  │ Effect                   │ Recovery                 │
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ Chrome crashes           │ Meet disconnects,        │ page.is_closed()         │
│                          │ recording stops          │ → exit, save partial     │
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ PulseAudio dies          │ No audio capture,        │ sounddevice raises       │
│                          │ ffmpeg receives silence  │ exception → log + exit   │
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ ffmpeg timeout (>15s)    │ 1s audio chunk lost      │ Catch CalledProcessError │
│                          │                          │ log, continue next chunk │
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ Network lost (Meet)      │ Chrome shows             │ No action — Chrome       │
│                          │ "Reconnecting..."        │ auto-reconnects          │
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ Host leaves meet         │ Bot becomes host,        │ No action — Chrome       │
│                          │ still records            │ continues receiving audio│
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ /stop called mid-encode  │ Process killed,          │ SIGTERM → finally block  │
│                          │ last chunk lost          │ saves final flush        │
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ Disk full                │ ffmpeg write fails,      │ Catch OSError, log       │
│                          │ OSError                  │ critical, exit            │
├──────────────────────────┼──────────────────────────┼──────────────────────────┤
│ Session exceeds duration │ stay_duration expires,   │ Normal exit — save final │
│                          │ bot leaves               │ flush, exit cleanly       │
└──────────────────────────┴──────────────────────────┴──────────────────────────┘
```

---

## 23. Final File Layout

```
INT-interview-Bot/
  │
  ├── Backend/
  │   ├── api.py                   ← +POST /join-silent, GET /recordings, GET /recordings/{f}
  │   ├── silent_recorder.py       ← NEW: entry point for silent meet recording
  │   ├── recorder.py              ← NEW (optional): standalong MeetingRecorder class
  │   ├── join_meet.py             ← +_wait_for_admission()
  │   ├── meet_creator.py          ← (unchanged — not used for silent join)
  │   ├── setup_login.py           ← (unchanged — one-time login)
  │   ├── start.sh                 ← (unchanged)
  │   ├── Dockerfile               ← +ffmpeg libopus0
  │   └── requirements.txt         ← (unchanged — numpy + sounddevice already present)
  │
  ├── Frontend/
  │   └── src/pages/Index.tsx      ← +"Existing Meet Link" field, "Join & Record" button
  │
  ├── recordings/                  ← Docker volume mount (persisted on host)
  │   ├── a1b2c3d4.webm
  │   └── e5f6g7h8.webm
  │
  ├── docker-compose.yaml          ← +./recordings:/app/recordings volume
  ├── nginx.conf                   ← (unchanged)
  └── Readme.md                    ← (unchanged)
```
