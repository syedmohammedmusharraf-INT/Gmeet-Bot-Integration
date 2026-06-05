# Understanding the Internal Architecture

## Part 1: Chrome Profile — How It Is Saved and Reused

### 1.1 The Problem

Google Meet requires the browser to be logged into a Google account. Every time the bot joins a meeting, Chrome needs a profile with:
- A signed-in Google session (cookies, tokens)
- Microphone permissions pre-granted for `meet.google.com`
- A ready-to-use browser state (no first-run dialogs, no welcome pages)

Setting this up from scratch on every run would require manual login every time, which defeats automation.

### 1.2 Solution: Persistent Chrome Profile with Volume Mount

The solution has three layers:

#### Layer 1 — Container Startup (`start.sh`, step 5/6)

When the container boots, `start.sh` writes a skeleton Chrome profile with **microphone permissions baked in**:

```bash
mkdir -p /tmp/chrome-profile/Default
cat > /tmp/chrome-profile/Default/Preferences << 'EOF'
{
  "profile": {
    "content_settings": {
      "exceptions": {
        "media_stream_mic": {
          "https://meet.google.com,*": { "setting": 1 },
          "https://meet.google.com:443,*": { "setting": 1 }
        }
      }
    },
    "default_content_setting_values": {
      "media_stream_mic": 1
    }
  },
  "browser": { "has_seen_welcome_page": true }
}
EOF
```

This writes a Chrome `Preferences` file that tells Chrome:
- Microphone access for `meet.google.com` is **allowed** (setting `1` = Allow)
- The welcome/first-run page has been seen — suppresses the startup dialog

The path `/tmp/chrome-profile` is the **base profile** — the canonical, long-lived profile that persists across container restarts via a Docker volume mount.

#### Layer 2 — Docker Volume Mount (`docker-compose.yaml`)

```yaml
volumes:
  - ./chrome-profile:/tmp/chrome-profile
```

The host directory `./chrome-profile/` is mounted at `/tmp/chrome-profile` inside the container. This means:
- Whatever Chrome writes to `/tmp/chrome-profile` (cookies, login tokens, preferences) is **saved to the host filesystem**
- When the container restarts, it reads the same directory — the Google login survives
- You can stop, remove, and recreate the container without losing the profile

The same applies to `./chrome-profile-backup:/tmp/chrome-profile-backup` — a safety copy.

#### Layer 3 — One-Time Manual Login

When you first start the container, `/tmp/chrome-profile` has only the preferences skeleton. No Google account is logged in.

You access the container's Chrome via **noVNC** (`http://localhost:4014/vnc.html`) and manually sign into Google once. Chrome writes the login cookies, OAuth tokens, and session data into `/tmp/chrome-profile/Default`. Because of the volume mount, these are instantly written to the host's `./chrome-profile/` directory.

From that point on, every future container start reads the same profile — the user stays logged in.

### 1.3 Per-Session Profile Isolation

When `POST /join-silent` is called, the API needs to give each recording session its **own Chrome instance** so multiple recordings don't interfere. But it also needs every instance to have the Google login.

The solution: **copy-on-start**.

```
POST /join-silent
    │
    ├── session_id = uuid4()
    ├── chrome_profile = /tmp/chrome-profile-{session_id}
    │
    ├── [API creates PulseAudio sink VSpk_{short_id}]
    │
    └── spawns silent_recorder.py with env:
           CHROME_USER_DATA_DIR=/tmp/chrome-profile-{session_id}
```

Inside `silent_recorder.py`, the `run_meet()` function receives `CHROME_USER_DATA_DIR` and passes it directly to Playwright:

```python
context = await p.chromium.launch_persistent_context(
    user_data_dir="/tmp/chrome-profile-{session_id}",  # unique per session
    channel="chrome",
    headless=False,
    ...
)
```

Playwright's `launch_persistent_context()` behaves differently from `launch()`:
- `launch()` creates a fresh ephemeral profile each time — no cookies, no login
- `launch_persistent_context()` uses a **persistent user data directory**. If the directory already exists, it reuses it. If not, Playwright creates it fresh.

So the first time a session starts:
1. No `/tmp/chrome-profile-{session_id}` exists yet
2. Playwright creates a new empty profile at that path
3. The bot joins the Meet as a logged-out user → **fails**

To fix this, the old (previous full) codebase had a `prepare_session_profile()` function in `orchestrator.py` that **copied the base profile**:

```python
shutil.copytree("/tmp/chrome-profile", "/tmp/chrome-profile-{session_id}")
```

This means every session starts with a fresh **copy** of the logged-in base profile. Multiple sessions can run concurrently because each has its own isolated copy. And since each is a copy, changes made during one session (e.g., cookies expiring) don't affect the base profile or other sessions.

> **Current state:** The current `silent_recorder.py` does NOT copy the base profile — it relies on Playwright creating a fresh profile at the session path. This works only if you don't need Google login. To restore isolated logged-in sessions, you would add a `copytree` step before calling `run_meet()`, mirroring the old `prepare_session_profile()` logic.

### 1.4 Summary — Chrome Profile Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│                        HOST FILESYSTEM                          │
│                                                                  │
│  ./chrome-profile/              ./chrome-profile-backup/         │
│  └── Default/                   └── Default/                     │
│      ├── Preferences                ├── Preferences              │
│      ├── Cookies                    ├── Cookies                  │
│      ├── Login Data                 ├── Login Data               │
│      └── ...                        └── ...                      │
│         ▲                            ▲                           │
│         │ mount                      │ mount                     │
│         └──────────┬─────────────────┘                           │
│                    │                                             │
├────────────────────┼─────────────────────────────────────────────┤
│                    │            DOCKER CONTAINER                  │
│                    │                                             │
│  /tmp/chrome-profile ← base, persists across restarts            │
│  /tmp/chrome-profile-backup ← safety copy                        │
│                    │                                             │
│  ── Session 1 ────│──────────────────────────────────────        │
│  /tmp/chrome-profile-{sid-a}  ← copytree from base (logged in)   │
│       │                                                          │
│  ── Session 2 ────│──────────────────────────────────────        │
│  /tmp/chrome-profile-{sid-b}  ← copytree from base (logged in)   │
│       │                                                          │
│  Each session:                                                    │
│    1. Gets its own Playwright context with user_data_dir          │
│    2. Chrome loads the copied profile → user is already logged in │
│    3. Joins the Meet without any login prompt                     │
│    4. On exit, the copy is discarded                              │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Part 2: Recording — How Audio Flows from Meet to .webm

### 2.1 The Problem

A Google Meet runs inside a Chrome browser. All participant audio plays through Chrome's audio output. To record it, we need to:

1. Capture Chrome's audio output **before** it reaches a physical speaker
2. Route that audio into our recording pipeline
3. Encode it into a standard audio file format

Chrome runs inside a Docker container with no physical audio hardware. We must create virtual audio devices.

### 2.2 Solution: Virtual Audio Pipeline

The system uses **PulseAudio** — a sound server that acts as a middleware between audio sources and sinks. PulseAudio can create **null sinks** (virtual output devices that accept audio but discard it) and **monitor sources** (virtual inputs that capture whatever is playing to a sink).

#### Step 1 — Default Virtual Cables (created once at container start)

In `start.sh`, step 4/6 creates the default infrastructure:

```bash
pactl load-module module-null-sink sink_name=VirtualMic
pactl load-module module-null-sink sink_name=VirtualSpeaker
pactl load-module module-virtual-source source_name=VirtualMicSource master=VirtualMic.monitor
pactl set-default-sink VirtualSpeaker
pactl set-default-source VirtualMicSource
```

| Component | Type | Purpose |
|---|---|---|
| `VirtualMic` | Null Sink | Where TTS audio would go → Chrome mic (not used in record-only mode) |
| `VirtualSpeaker` | Null Sink | Where Chrome outputs Meet audio |
| `VirtualMicSource` | Virtual Source | Captures from VirtualMic.monitor (not used in record-only mode) |

Chrome's ALSA output is configured via `--alsa-output-device=pulse`, which sends all audio to the PulseAudio default sink (`VirtualSpeaker`).

#### Step 2 — Per-Session Sink (created on each /join-silent)

When `POST /join-silent` is called, the API creates an **additional** per-session sink:

```python
def _pactl("load-module", "module-null-sink",
           f"sink_name=VSpk_{sid8}",
           f"sink_properties=device.description=VSpk_{sid8}")
```

This is a second null sink named `VSpk_{short_session_id}`. It exists so that each recording session can have its own dedicated audio capture point, independent of the global defaults.

The environment passed to `silent_recorder.py` overrides the PulseAudio routing:

```python
env["PULSE_SPK_SINK"] = "VSpk_{sid8}"      # Where Chrome sends audio
env["PULSE_SINK"]     = "VSpk_{sid8}"      # Default sink for Chrome
env["PULSE_SOURCE"]   = "VSpk_{sid8}.monitor"  # Where we capture from
```

This means Chrome's ALSA output (`--alsa-output-device=pulse`) resolves to `VSpk_{sid8}`, not the default `VirtualSpeaker`.

#### Step 3 — Audio Capture (inside silent_recorder.py)

The recording process starts three concurrent threads:

```
┌──────────────────────────────────────────────────────────────────┐
│                    silent_recorder.py                             │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  MAIN THREAD (async)                                       │  │
│  │  Calls run_meet(joined_event)                              │  │
│  │  → Playwright launches Chrome with user_data_dir           │  │
│  │  → Chrome loads the isolated profile copy                  │  │
│  │  → Opens meet.google.com, clicks mic/join, stays in loop   │  │
│  │  → Sets joined_event when successfully in the meeting      │  │
│  │  → Loops in 5s intervals until stop or duration expires    │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  AUDIO THREAD (sounddevice callback)                        │  │
│  │  Runs in a native C thread from portaudio                    │  │
│  │  Opens sd.InputStream(device="pulse")                        │  │
│  │  → PulseAudio routes this to VSpk_{sid8}.monitor            │  │
│  │  → Callback fires every ~10ms with a PCM float32 buffer     │  │
│  │  → Converts stereo → mono (mean of channels)                │  │
│  │  → Appends to shared audio_buffer under thread lock         │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  FLUSH THREAD (every 5s)                                    │  │
│  │  Takes audio_buffer contents under lock                     │  │
│  │  Concatenates all chunks into one float32 array             │  │
│  │  Calls _encode_chunk():                                     │  │
│  │    1. Resample 44100Hz → 48000Hz (Opus native rate)        │  │
│  │    2. Clip to [-1.0, 1.0], convert to int16                │  │
│  │    3. Write int16 PCM to temp file                         │  │
│  │    4. Run: ffmpeg -f s16le -ar 48000 -ac 1 -i temp.pcm     │  │
│  │            -c:a libopus -b:a 32k -application voip out.webm │  │
│  │    5. Read the encoded WebM bytes                          │  │
│  │    6. Append to the .webm on disk                          │  │
│  │    7. Clean up temp files                                  │  │
│  │  → Result: each 5s chunk of audio is encoded and appended  │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### 2.3 Full Audio Pipeline Diagram

```
Google Meet Participants
         │
         │ RTP/audio packets over WebRTC
         ▼
┌──────────────────┐
│   Chrome (in Xvfb) │  Running in virtual framebuffer :99
│                  │
│  ALSA output ────┼──→ --alsa-output-device=pulse
│  ALSA input  ────┼──→ --alsa-input-device=pulse
└──────────────────┘
         │
         │ Chrome's decoded audio (all participants mixed)
         │ PulseAudio routes to default sink
         ▼
┌──────────────────────────────────────┐
│  PulseAudio Null Sink: VSpk_{sid8}   │
│                                      │
│  Accepts PCM audio from any source   │
│  Discards it (no physical output)    │
│  But exposes a .monitor interface    │
└──────────────────────────────────────┘
         │
         │ VSpk_{sid8}.monitor
         ▼
┌──────────────────────────────────────┐
│  sounddevice.InputStream             │
│  (portaudio backend → PulseAudio)    │
│                                      │
│  Device: "pulse"                     │
│  Rate: 44100 Hz                      │
│  Channels: 2 (stereo from Chrome)    │
│  Format: float32                     │
└──────────────────────────────────────┘
         │
         │ audio_callback()  (every ~10ms)
         ▼
┌──────────────────────────────────────┐
│  Stereo → Mono (np.mean)             │
│  → append to audio_buffer[]          │
│    (thread-safe, lock-protected)     │
└──────────────────────────────────────┘
         │
         │ flush_loop()  (every 5 seconds)
         ▼
┌──────────────────────────────────────┐
│  np.concatenate(audio_buffer)        │
│  → float32 array at 44100 Hz         │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│  _encode_chunk()                     │
│                                      │
│  1. Resample: 44100 → 48000 Hz      │
│     (linear interpolation)           │
│                                      │
│  2. Clip to [-1.0, 1.0]             │
│     Convert to int16 (* 32767)       │
│                                      │
│  3. Write raw int16 PCM to temp file │
│                                      │
│  4. ffmpeg -f s16le -ar 48000       │
│         -ac 1 -i temp.pcm            │
│         -c:a libopus -b:a 32k        │
│         -application voip out.webm   │
│                                      │
│     -c:a libopus  → Opus codec       │
│     -b:a 32k      → 32 kbps bitrate  │
│     -application voip → optimised    │
│                        for voice     │
│                                      │
│  5. Read encoded .webm bytes         │
│  6. Append to output file            │
│  7. Delete temp files                │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│  /app/recordings/{short_id}.webm     │
│                                      │
│  Format: WebM container              │
│  Audio: Opus @ 32 kbps, mono        │
│  Written incrementally (append mode) │
│  Survives container restart via      │
│  Docker volume mount                 │
└──────────────────────────────────────┘
```

### 2.4 Why Opus in WebM?

| Choice | Why |
|---|---|
| **Opus codec** | Designed for voice. Handles speech at 32 kbps with excellent clarity. Native sample rate 48kHz. Built-in packet loss concealment. |
| **WebM container** | Matroska-based, supports streaming (no moov atom needed at start). Can append clusters incrementally — ideal for real-time recording where we don't know the total duration in advance. |
| **32 kbps** | Sweet spot for voice: intelligible, small file sizes (~240 KB per minute). |
| **-application voip** | Opus mode optimized for voice with low bitrate, prioritizes speech frequencies. |
| **48kHz resample** | Opus operates natively at 48kHz. Our PCM arrives at 44.1kHz (standard ALSA rate). Linear interpolation resampling adds ~0.5ms latency — negligible. |

### 2.5 Session Cleanup

When a session ends (either by `POST /stop/{id}` or by reaching `stay_duration`):

1. `CancelledError` propagates through `run_meet()` → Chrome closes → Playwright context shuts down
2. `silent_recorder.py`'s `main()` catches the exception
3. `stop_event.set()` signals the flush thread to exit
4. `stream.stop()` stops the sounddevice InputStream
5. `flusher.join(timeout=10)` waits for the final `_encode_chunk()` to complete
6. The `.webm` file is fully written and remains on disk at `/app/recordings/`

The per-session PulseAudio sink (`VSpk_{sid8}`) is **not explicitly cleaned up** in the current code. Over many sessions, orphaned sinks could accumulate. A production improvement would be to unload the module in a finalizer.

---

## Part 3: Data Flow Summary — From API Call to Recording File

```
User calls: POST /join-silent {"meet_link": "...", "duration_minutes": 60}
    │
    ▼
routes/sessions.py
    │
    ├── Generate UUID session_id
    ├── Create PulseAudio sink: pactl load-module module-null-sink name=VSpk_{sid8}
    ├── Set env vars:
    │     SESSION_ID, MEETING_LINK, CHROME_USER_DATA_DIR,
    │     PULSE_SPK_SINK, PULSE_SINK, PULSE_SOURCE, STAY_DURATION_SECONDS
    │
    └── Spawn subprocess: python /app/silent_recorder.py
              │
              ▼
    silent_recorder.py
        │
        ├── Create output path: /app/recordings/{sid8}.webm
        ├── Find PulseAudio device index
        ├── Start sd.InputStream (audio callback thread)
        ├── Start flush_loop (flush thread, 5s interval)
        │
        └── await run_meet(joined_event)
                  │
                  ▼
        join_meet.py :: run_meet()
            │
            ├── Set PulseAudio defaults for this session
            ├── Launch Chrome (persistent context) with isolated profile
            ├── Navigate to meet.google.com/{meeting_code}
            ├── [If login required] Wait for manual login via noVNC
            ├── Click mic button (up to 6 attempts)
            ├── Click join button (up to 10 attempts)
            ├── Verify mic is ON (Ctrl+D if OFF)
            ├── Set joined_event → signals silent_recorder that capture is live
            │
            └── Loop: sleep(5) until:
                  ├── CancelledError (user stopped via API)
                  └── stay_duration elapsed
                        │
                        ▼
                  Chrome navigates away / closes
                        │
                        ▼
        Back in silent_recorder.py:
            ├── stop_event.set() → flush thread exits
            ├── stream.stop() → audio capture stops
            ├── flusher.join(10) → final WebM append completes
            └── /app/recordings/{sid8}.webm is ready
                  │
                  ▼
        User downloads via: GET /recordings/{filename}
```

### Key Design Decisions

1. **Subprocess per session** — Each recording runs in its own Python process. This provides true isolation: if one crashes, others are unaffected. The API tracks PIDs and can `terminate()` / `kill()`.

2. **Thread-safe audio buffer** — `audio_buffer` is a plain list protected by `threading.Lock`. The audio callback pushes data; the flush thread pops it. No lock contention because each side holds the lock for <1ms.

3. **Incremental file writing** — WebM supports appending clusters. Instead of buffering the entire recording in memory, we write every 5 seconds. This means:
   - Memory usage is bounded (~5s × 44100 × 4 bytes × 2 channels ≈ 1.7 MB)
   - If the process crashes mid-recording, you still have audio up to the last flush
   - The file is playable as soon as the first chunk is written

4. **No API keys needed** — Unlike the original interview bot (which needed OpenAI and Cartesia keys), the recorder-only mode requires zero external services. It just needs a Chrome browser and PulseAudio.
