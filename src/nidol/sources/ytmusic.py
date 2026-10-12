"""YouTube Music search via ytmusicapi (unofficial, synchronous, so it runs in a thread)."""

import asyncio
import logging
from dataclasses import dataclass
from functools import cache

from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicUserError

log = logging.getLogger(__name__)

_KINDS = {
    "MUSIC_VIDEO_TYPE_ATV": "official_audio",
    "MUSIC_VIDEO_TYPE_OMV": "official_video",
    "MUSIC_VIDEO_TYPE_UGC": "user_upload",
}


@dataclass(frozen=True)
class YTResult:
    video_id: str
    title: str
    artists: list[str]
    duration_ms: int | None
    kind: str


@cache
def _client(country: str | None) -> YTMusic:
    if country:
        try:
            return YTMusic(location=country)
        except YTMusicUserError:
            log.warning("ytmusicapi does not support location %s, searching unlocalized", country)
    return YTMusic()


def _search_sync(query: str, country: str | None, filter_: str) -> list[YTResult]:
    results = []
    for item in _client(country).search(query, filter=filter_, limit=10):
        if not item.get("videoId"):
            continue
        secs = item.get("duration_seconds")
        results.append(
            YTResult(
                video_id=item["videoId"],
                title=item.get("title") or "",
                artists=[a["name"] for a in item.get("artists") or [] if a.get("name")],
                duration_ms=secs * 1000 if secs else None,
                kind=_KINDS.get(item.get("videoType") or "", "other"),
            )
        )
    return results


async def search(query: str, country: str | None) -> list[YTResult]:
    """Songs (official audio) and videos, searched concurrently, deduped by videoId."""
    songs, videos = await asyncio.gather(
        asyncio.to_thread(_search_sync, query, country, "songs"),
        asyncio.to_thread(_search_sync, query, country, "videos"),
    )
    unique: dict[str, YTResult] = {}
    for r in songs + videos:
        unique.setdefault(r.video_id, r)
    return list(unique.values())
