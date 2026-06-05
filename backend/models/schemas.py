from pydantic import BaseModel


class JoinSilentRequest(BaseModel):
    meet_link: str
    duration_minutes: int = 60


class StartResponse(BaseModel):
    session_id: str
    status: str
    meetLink: str


class SessionStatus(BaseModel):
    session_id: str
    status: str
    meetLink: str = ""
    started_at: float = 0.0
    duration_minutes: int = 0


class StopResponse(BaseModel):
    status: str
    session_id: str


class RecordingInfo(BaseModel):
    filename: str
    size_mb: float
    created_at: float


class RecordingListResponse(BaseModel):
    recordings: list[RecordingInfo]
