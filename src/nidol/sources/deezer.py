"""Deezer public API (no key): text search that returns ISRCs."""

from dataclasses import dataclass

import httpx

from nidol.text import normalize

SEARCH_URL = "https://api.deezer.com/search"


@dataclass(frozen=True)
class DeezerTrack:
    id: int
    title: str
    artist: str
    duration_ms: int
    isrc: str | None
    explicit: bool | None
    cover_url: str | None


async def find_track(
    client: httpx.AsyncClient, artists: list[str], title: str, duration_ms: int | None
) -> DeezerTrack | None:
    """Best Deezer match for a known track: same main artist, closest duration (within 5 s)."""
    # The advanced `artist:"…" track:"…"` syntax returned no results on 2026-10-11; a plain query works.
    r = await client.get(SEARCH_URL, params={"q": f"{artists[0]} {title}", "limit": 10})
    r.raise_for_status()
    main_artist = normalize(artists[0])
    best: tuple[int, DeezerTrack] | None = None
    for item in r.json().get("data", []):
        if normalize(item["artist"]["name"]) != main_artist:
            continue
        track = DeezerTrack(
            id=item["id"],
            title=item["title"],
            artist=item["artist"]["name"],
            duration_ms=item["duration"] * 1000,
            isrc=item.get("isrc"),
            explicit=item.get("explicit_lyrics"),
            cover_url=(item.get("album") or {}).get("cover_xl"),
        )
        delta = abs(track.duration_ms - duration_ms) if duration_ms else 0
        if delta > 5000:
            continue
        if best is None or delta < best[0]:
            best = (delta, track)
    return best[1] if best else None
