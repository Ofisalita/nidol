import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

Kind = Literal["track", "album", "playlist"]

_SPOTIFY_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?/)?(?:embed/)?"
    r"(track|album|playlist)/([A-Za-z0-9]{22})"
)
_SPOTIFY_URI = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")

_YOUTUBE_HOSTS = {"youtube.com", "youtube-nocookie.com"}
_YOUTUBE_PATH_ID = {"shorts", "embed", "live", "v"}
_YOUTUBE_ID = re.compile(r"^[\w-]{11}$")


@dataclass(frozen=True)
class ParsedInput:
    platform: str
    kind: Kind
    id: str


def parse_input(raw: str) -> ParsedInput | None:
    """Identify platform + kind + ID. Returns None for anything not supported yet."""
    raw = raw.strip()
    m = _SPOTIFY_URL.match(raw) or _SPOTIFY_URI.match(raw)
    if m:
        return ParsedInput("spotify", m.group(1), m.group(2))  # type: ignore[arg-type]
    return _parse_youtube(raw)


def _parse_youtube(raw: str) -> ParsedInput | None:
    """Same link set as bye0's `youtubeId()` (apps/server/src/matching.ts), plus playlist links."""
    url = urlsplit(raw)
    if url.scheme not in ("http", "https") or not url.hostname:
        return None
    host = re.sub(r"^(www|m|music)\.", "", url.hostname)
    _, first, second, *_ = [*url.path.split("/"), "", ""]
    query = parse_qs(url.query)
    video_id: str | None = None
    if host == "youtu.be":
        video_id = first
    elif host in _YOUTUBE_HOSTS:
        if first == "watch":
            video_id = query.get("v", [None])[0]
        elif first in _YOUTUBE_PATH_ID:
            video_id = second
        elif first == "playlist" and query.get("list"):
            return ParsedInput("youtube", "playlist", query["list"][0])
    if video_id and _YOUTUBE_ID.match(video_id):
        return ParsedInput("youtube", "track", video_id)
    return None
