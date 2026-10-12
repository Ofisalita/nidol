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
    isLive: bool = False  # a live stream: durationMs is null but the length is unbounded


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


class ExpandRequest(BaseModel):
    input: str


class Collection(BaseModel):
    kind: Literal["album", "playlist"]
    platform: str
    title: str


class Source(BaseModel):
    platform: str
    id: str
    url: str  # always something POST /resolve accepts


class ExpandTrack(BaseModel):
    title: str
    artists: list[str]
    durationMs: int | None = None
    explicit: bool | None = None
    source: Source


class ExpandResponse(BaseModel):
    collection: Collection
    tracks: list[ExpandTrack]
    truncated: bool


class SearchResult(BaseModel):
    title: str
    artists: list[str]
    durationMs: int | None = None
    artworkUrl: str | None = None
    isrc: str | None = None
    source: Source


class SearchResponse(BaseModel):
    results: list[SearchResult]


class ErrorBody(BaseModel):
    error: str
    kind: str | None = None
    source: str | None = None


class NidolError(Exception):
    def __init__(self, status: int, error: str, **extra: str):
        super().__init__(error)
        self.status = status
        self.body = ErrorBody(error=error, **extra)
