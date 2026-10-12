"""Deezer public API (no key): track lookups and text search, both with ISRCs."""

from dataclasses import dataclass

import httpx

from nidol.text import normalize

API = "https://api.deezer.com"


class DeezerNotFound(Exception):
    pass


@dataclass(frozen=True)
class DeezerTrack:
    id: int
    title: str
    artists: list[str]
    duration_ms: int
    isrc: str | None
    explicit: bool | None
    cover_url: str | None
    year: int | None = None
    rank: int | None = None  # Deezer's popularity score, on search results

    @property
    def url(self) -> str:
        return f"https://www.deezer.com/track/{self.id}"


def _track(item: dict) -> DeezerTrack:
    # /track/<id> lists every artist under `contributors`; search results only have `artist`.
    artists = [c["name"] for c in item.get("contributors") or []] or [item["artist"]["name"]]
    release = item.get("release_date") or ""
    album = item.get("album") or {}
    return DeezerTrack(
        id=item["id"],
        title=item["title"],
        artists=artists,
        duration_ms=item["duration"] * 1000,
        isrc=item.get("isrc"),
        explicit=item.get("explicit_lyrics"),
        cover_url=album.get("cover_xl") or album.get("cover_big"),
        year=int(release[:4]) if release[:4].isdigit() else None,
        rank=item.get("rank"),
    )


async def get_track(client: httpx.AsyncClient, track_id: str) -> DeezerTrack:
    r = await client.get(f"{API}/track/{track_id}")
    r.raise_for_status()
    body = r.json()
    if "error" in body:  # a missing track is HTTP 200 with {"error": {"code": 800, …}}
        if body["error"].get("code") == 800:
            raise DeezerNotFound()
        raise httpx.HTTPError(f"deezer error: {body['error']}")
    return _track(body)


async def search(client: httpx.AsyncClient, query: str, limit: int = 10) -> list[DeezerTrack]:
    # The advanced `artist:"…" track:"…"` syntax returned no results on 2026-10-11; a plain query works.
    r = await client.get(f"{API}/search", params={"q": query, "limit": limit})
    r.raise_for_status()
    return [_track(item) for item in r.json().get("data", [])]


async def find_track(
    client: httpx.AsyncClient, artists: list[str], title: str, duration_ms: int | None
) -> DeezerTrack | None:
    """Best Deezer match for a known track: same main artist, closest duration (within 5 s)."""
    main_artist = normalize(artists[0])
    best: tuple[int, DeezerTrack] | None = None
    for track in await search(client, f"{artists[0]} {title}"):
        if normalize(track.artists[0]) != main_artist:
            continue
        delta = abs(track.duration_ms - duration_ms) if duration_ms else 0
        if delta > 5000:
            continue
        if best is None or delta < best[0]:
            best = (delta, track)
    return best[1] if best else None
