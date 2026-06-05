import os
import uuid
import subprocess
import sys
import threading
import time
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from models.schemas import (
    JoinSilentRequest, StartResponse, SessionStatus,
    StopResponse, RecordingInfo, RecordingListResponse,
)

router = APIRouter()

active_sessions: dict[str, dict] = {}
sessions_lock = threading.Lock()


def _short_id(session_id: str) -> str:
    return session_id.replace("-", "")[:8]


def _stream_output(proc: subprocess.Popen, session_id: str):
    sid8 = _short_id(session_id)
    try:
        for line in iter(proc.stdout.readline, b""):
            text = line.decode("utf-8", errors="replace").rstrip()
            print(f"[{sid8}] {text}", flush=True)
    except Exception:
        pass


def _reap_dead_sessions():
    while True:
        time.sleep(10)
        with sessions_lock:
            dead = [
                sid for sid, info in active_sessions.items()
                if info["process"].poll() is not None
            ]
            for sid in dead:
                print(f"[API] Session {_short_id(sid)} ended — removing.", flush=True)
                del active_sessions[sid]


threading.Thread(target=_reap_dead_sessions, daemon=True).start()


def _pactl(*args) -> bool:
    try:
        result = subprocess.run(
            ["pactl"] + list(args), capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[API] pactl error: {e}", flush=True)
        return False


@router.post("/join-silent", response_model=StartResponse)
async def join_silent(req: JoinSilentRequest):
    session_id = str(uuid.uuid4())
    sid8 = _short_id(session_id)

    chrome_profile = f"/tmp/chrome-profile-{session_id}"
    spk_sink = f"VSpk_{sid8}"

    stay_seconds = max(300, min(req.duration_minutes * 60 + 300, 10800))

    env = os.environ.copy()
    env["SESSION_ID"]            = session_id
    env["MEETING_LINK"]          = req.meet_link
    env["CHROME_USER_DATA_DIR"]  = chrome_profile
    env["PULSE_SPK_SINK"]        = spk_sink
    env["PULSE_SINK"]            = spk_sink
    env["PULSE_SOURCE"]          = f"{spk_sink}.monitor"
    env["STAY_DURATION_SECONDS"] = str(stay_seconds)

    _pactl("load-module", "module-null-sink",
           f"sink_name={spk_sink}",
           f"sink_properties=device.description={spk_sink}")

    proc = subprocess.Popen(
        [sys.executable, "/app/silent_recorder.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    t = threading.Thread(target=_stream_output, args=(proc, session_id), daemon=True)
    t.start()

    with sessions_lock:
        active_sessions[session_id] = {
            "process":          proc,
            "meetLink":         req.meet_link,
            "type":             "silent_recorder",
            "started_at":       time.time(),
            "duration_minutes": req.duration_minutes,
            "chrome_profile":   chrome_profile,
            "spk_sink":         spk_sink,
        }

    print(f"[API] Silent recorder started: {sid8} (PID {proc.pid})", flush=True)
    return StartResponse(session_id=session_id, status="recording", meetLink=req.meet_link)


@router.get("/status/{session_id}", response_model=SessionStatus)
async def get_status(session_id: str):
    with sessions_lock:
        info = active_sessions.get(session_id)

    if not info:
        return SessionStatus(session_id=session_id, status="not_found")

    if info["process"].poll() is None:
        return SessionStatus(
            session_id=session_id,
            status="running",
            meetLink=info["meetLink"],
            started_at=info["started_at"],
        )

    with sessions_lock:
        active_sessions.pop(session_id, None)

    return SessionStatus(session_id=session_id, status="idle")


@router.post("/stop/{session_id}", response_model=StopResponse)
async def stop_session(session_id: str):
    with sessions_lock:
        info = active_sessions.get(session_id)

    if not info:
        return StopResponse(status="not_found", session_id=session_id)

    proc = info["process"]
    sid8 = _short_id(session_id)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    with sessions_lock:
        active_sessions.pop(session_id, None)

    print(f"[API] Session stopped: {sid8}", flush=True)
    return StopResponse(status="stopped", session_id=session_id)


@router.get("/sessions")
async def list_sessions():
    with sessions_lock:
        result = []
        for sid, info in active_sessions.items():
            running = info["process"].poll() is None
            result.append({
                "session_id":       sid,
                "short_id":         _short_id(sid),
                "status":           "running" if running else "idle",
                "meetLink":         info["meetLink"],
                "started_at":       info["started_at"],
                "duration_minutes": info.get("duration_minutes", "?"),
                "uptime_sec":       int(time.time() - info["started_at"]),
            })
    return {"sessions": result, "count": len(result)}


@router.get("/recordings", response_model=RecordingListResponse)
async def list_recordings():
    path = "/app/recordings"
    if not os.path.exists(path):
        return RecordingListResponse(recordings=[])

    files = [f for f in os.listdir(path) if f.endswith(".webm")]
    result = []
    for f in files:
        fpath = os.path.join(path, f)
        stats = os.stat(fpath)
        result.append(RecordingInfo(
            filename=f,
            size_mb=round(stats.st_size / (1024 * 1024), 2),
            created_at=stats.st_ctime,
        ))

    result.sort(key=lambda x: x.created_at, reverse=True)
    return RecordingListResponse(recordings=result)


@router.get("/recordings/{filename}")
async def download_recording(filename: str):
    path = os.path.join("/app/recordings", filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="audio/webm", filename=filename)


@router.get("/health")
async def health():
    with sessions_lock:
        count = len(active_sessions)
    return {"ok": True, "active_sessions": count}
