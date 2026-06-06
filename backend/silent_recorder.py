#!/usr/bin/env python3
"""
silent_recorder.py — Joins a Google Meet and records audio of all participants.

multi-threaded architecture:
  - Main thread: Playwright loop (joins and stays in Meet)
  - Audio thread: sounddevice callback (captures raw PCM from PulseAudio)
  - Flush thread: periodic WebM encoder (appends clusters to final file)
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time
import numpy as np
import sounddevice as sd
from join_meet import run_meet

# ── Config ────────────────────────────────────────────────────────────────────
SESSION_ID    = os.getenv("SESSION_ID", "default")
MEET_LINK     = os.getenv("MEETING_LINK")
STAY_DURATION = int(os.getenv("STAY_DURATION_SECONDS", "3600"))
PULSE_SERVER  = os.getenv("PULSE_SERVER", "unix:/var/run/pulse/native")
PULSE_SOURCE  = os.getenv("PULSE_SOURCE") # Should be VSpk_{sid}.monitor
PULSE_SINK    = os.getenv("PULSE_SPK_SINK")  # Where Chrome writes audio

RECORDINGS_DIR = "/app/recordings"
OUTPUT_FILE    = os.path.join(RECORDINGS_DIR, f"{SESSION_ID[:8]}.webm")

os.environ["DISPLAY"] = ":99"
os.environ["PULSE_SERVER"] = PULSE_SERVER

audio_buffer = []
buffer_lock = threading.Lock()
stop_event = threading.Event()

# ── Audio Callback ────────────────────────────────────────────────────────────

def audio_callback(indata, frames, time_info, status):
    if status:
        print(f"[REC] ⚠️ {status}", flush=True)
    # Convert stereo to mono
    mono = np.mean(indata, axis=1) if indata.ndim > 1 else indata.flatten()
    with buffer_lock:
        audio_buffer.append(mono.copy())

# ── Encoding ──────────────────────────────────────────────────────────────────

def _resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if from_rate == to_rate:
        return audio
    new_len = int(len(audio) * to_rate / from_rate)
    return np.interp(
        np.linspace(0, len(audio) - 1, new_len),
        np.arange(len(audio)),
        audio,
    ).astype(np.float32)

def _encode_chunk(pcm_float32: np.ndarray, rate: int) -> bytes:
    """Encode float32 PCM to Opus-in-WebM cluster bytes."""
    # Resample to 48kHz (Opus native)
    resampled = _resample(pcm_float32, rate, 48000)
    
    # Clip and convert to int16
    pcm_int16 = (np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16)
    
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f_in:
        f_in.write(pcm_int16.tobytes())
        pcm_path = f_in.name
        
    out_path = pcm_path + ".webm"
    
    try:
        # Encode to WebM using ffmpeg
        # -application voip optimizes for voice
        subprocess.run(
            ["ffmpeg", "-y", "-f", "s16le", "-ar", "48000", "-ac", "1",
             "-i", pcm_path, "-c:a", "libopus", "-b:a", "32k",
             "-application", "voip", out_path],
            capture_output=True, timeout=15, check=True
        )
        
        with open(out_path, "rb") as f:
            data = f.read()
        return data
    except Exception as e:
        print(f"[REC] ❌ Encoding error: {e}", flush=True)
        return b""
    finally:
        if os.path.exists(pcm_path): os.unlink(pcm_path)
        if os.path.exists(out_path): os.unlink(out_path)

# ── Flush Loop ────────────────────────────────────────────────────────────────

def flush_loop():
    print(f"[REC] 💾 Recording to: {OUTPUT_FILE}", flush=True)
    
    # Ensure directory exists
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    
    # Initialize file if it doesn't exist
    if not os.path.exists(OUTPUT_FILE):
        open(OUTPUT_FILE, "wb").close()

    while not stop_event.is_set():
        time.sleep(5.0)  # Flush every 5 seconds
        
        with buffer_lock:
            if not audio_buffer:
                continue
            chunks = list(audio_buffer)
            audio_buffer.clear()
            
        combined = np.concatenate(chunks)
        webm_chunk = _encode_chunk(combined, 44100)
        
        if webm_chunk:
            with open(OUTPUT_FILE, "ab") as f:
                f.write(webm_chunk)
            print(f"[REC] 📝 Appended {len(webm_chunk)} bytes to recording", flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not MEET_LINK:
        print("[REC] ❌ No MEETING_LINK provided", file=sys.stderr)
        sys.exit(1)

    print(f"[REC] 🚀 Session: {SESSION_ID}", flush=True)
    print(f"[REC] 🎙️ Source : {PULSE_SOURCE}", flush=True)
    
    # Find device index for PULSE_SOURCE
    device_index = None
    try:
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            if dev['name'] == 'pulse' or dev['name'] == 'default':
                device_index = i
                break
    except Exception as e:
        print(f"[REC] ⚠️ Could not query devices: {e}", flush=True)

    # Start capture
    try:
        stream = sd.InputStream(
            device=device_index,
            samplerate=44100,
            channels=2,
            callback=audio_callback
        )
        stream.start()
        print("[REC] ✅ Audio capture started", flush=True)
    except Exception as e:
        print(f"[REC] ❌ Failed to start capture: {e}", file=sys.stderr)
        sys.exit(1)

    # Start flush thread
    flusher = threading.Thread(target=flush_loop, daemon=True)
    flusher.start()

    # Join Meet
    joined_event = asyncio.Event()
    meet_task = asyncio.create_task(run_meet(joined_event))
    
    try:
        await meet_task
    except asyncio.CancelledError:
        print("[REC] 🛑 Stop signal received", flush=True)
    except Exception as e:
        print(f"[REC] ❌ Meet error: {e}", flush=True)
    finally:
        stop_event.set()
        stream.stop()
        flusher.join(timeout=10)
        print("[REC] 👋 Session complete", flush=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
