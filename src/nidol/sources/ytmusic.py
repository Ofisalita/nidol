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
    explicit: bool | None = None


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


class VideoUnavailable(Exception):
    pass


@dataclass(frozen=True)
class YTVideo:
    video_id: str
    title: str
    author: str
    duration_ms: int | None  # None for live streams (YouTube reports length 0)
    is_live: bool
    kind: str


def parse_song(video_id: str, song: dict) -> YTVideo:
    """`YTMusic.get_song` response → YTVideo. Raises VideoUnavailable when YouTube has no details."""
    details = song.get("videoDetails")
    if not details:
        reason = (song.get("playabilityStatus") or {}).get("reason")
        raise VideoUnavailable(reason or "no videoDetails")
    secs = int(details.get("lengthSeconds") or 0)
    is_live = bool(details.get("isLive") or (details.get("isLiveContent") and not secs))
    return YTVideo(
        video_id=details.get("videoId") or video_id,
        title=details.get("title") or "",
        author=details.get("author") or "",
        duration_ms=secs * 1000 if secs else None,
        is_live=is_live,
        kind=_KINDS.get(details.get("musicVideoType") or "", "other"),
    )


async def get_video(video_id: str) -> YTVideo:
    song = await asyncio.to_thread(_client(None).get_song, video_id)
    return parse_song(video_id, song)


@dataclass(frozen=True)
class YTCollection:
    title: str
    tracks: list[YTResult]
    total: int | None  # YouTube's own count; may exceed len(tracks)


def parse_collection(raw: dict) -> YTCollection:
    """`get_album` / `get_playlist` response → YTCollection. Unplayable entries are dropped."""
    tracks = [
        YTResult(
            video_id=t["videoId"],
            title=t.get("title") or "",
            artists=[a["name"] for a in t.get("artists") or [] if a.get("name")],
            duration_ms=t["duration_seconds"] * 1000 if t.get("duration_seconds") else None,
            kind=_KINDS.get(t.get("videoType") or "", "other"),
            explicit=t.get("isExplicit"),
        )
        for t in raw.get("tracks") or []
        if t.get("videoId") and t.get("isAvailable", True)
    ]
    total = raw.get("trackCount")
    return YTCollection(title=raw.get("title") or "", tracks=tracks, total=total)


def _collection_sync(id_: str, limit: int) -> dict:
    yt = _client(None)
    return yt.get_album(id_) if id_.startswith("MPREb_") else yt.get_playlist(id_, limit=limit)


async def get_collection(id_: str, limit: int) -> YTCollection:
    """Album (MPREb_… browse ID) or playlist. ytmusicapi raises KeyError for missing/private
    lists, the same error it raises if YouTube changes its page layout."""
    return parse_collection(await asyncio.to_thread(_collection_sync, id_, limit))
