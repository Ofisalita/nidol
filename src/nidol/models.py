from typing import Literal

from pydantic import BaseModel, Field

CandidateKind = Literal["official_audio", "official_video", "user_upload", "other"]


class ResolveRequest(BaseModel):
    input: str
    target: Literal["youtube"] = "youtube"
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    preferAudio: bool = False


class Canonical(BaseModel):
    isrc: str | None = None
    mbid: str | None = None
    title: str
    artists: list[str]
    durationMs: int | None = None
    year: int | None = None
    explicit: bool | None = None
    artworkUrl: str | None = None


class Candidate(BaseModel):
    videoId: str
    title: str
    channel: str | None = None
    durationMs: int | None = None
    kind: CandidateKind
    thumbnailUrl: str
    confidence: float


class ResolveResponse(BaseModel):
    canonical: Canonical
    candidates: list[Candidate]
    needsConfirmation: bool


class ErrorBody(BaseModel):
    error: str
    kind: str | None = None
    source: str | None = None


class NidolError(Exception):
    def __init__(self, status: int, error: str, **extra: str):
        super().__init__(error)
        self.status = status
        self.body = ErrorBody(error=error, **extra)
