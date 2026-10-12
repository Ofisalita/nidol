"""GET /search (D8): the guest search box lists songs; picking one calls /resolve on its source.url."""

import asyncio
from itertools import zip_longest

import httpx

from nidol.config import settings
from nidol.deadlines import optional
from nidol.models import NidolError, SearchResponse, SearchResult, Source
from nidol.sources import deezer, ytmusic
from nidol.text import core_title, normalize

SAME_SONG_MS = 5_000
# Below this Deezer popularity, unmatched results were mostly covers, fan remixes and TikTok edits
# (spot check 2026-10-12); they go after everything else instead of being dropped.
DEEZER_LOW_RANK = 100_000


def _same_song(yt: ytmusic.YTResult, dz: deezer.DeezerTrack) -> bool:
    return (
        core_title(yt.title) == core_title(dz.title)
        and bool(yt.artists)
        and normalize(yt.artists[0]) == normalize(dz.artists[0])
        and yt.duration_ms is not None
        and abs(yt.duration_ms - dz.duration_ms) <= SAME_SONG_MS
    )


def _from_yt(r: ytmusic.YTResult, isrc: str | None) -> SearchResult:
    return SearchResult(
        title=r.title,
        artists=r.artists,
        durationMs=r.duration_ms,
        artworkUrl=r.artwork_url,
        isrc=isrc,
        source=Source(
            platform="youtube", id=r.video_id, url=f"https://music.youtube.com/watch?v={r.video_id}"
        ),
    )


def _from_deezer(t: deezer.DeezerTrack) -> SearchResult:
    return SearchResult(
        title=t.title,
        artists=t.artists,
        durationMs=t.duration_ms,
        artworkUrl=t.cover_url,
        isrc=t.isrc,
        source=Source(platform="deezer", id=str(t.id), url=t.url),
    )


def merge(
    yt: list[ytmusic.YTResult], dz: list[deezer.DeezerTrack], limit: int
) -> list[SearchResult]:
    """YT Music leads (two results per Deezer one): it's the playback target and ranked better in
    spot checks. A song on both keeps the YT Music entry (already playable, so /resolve is
    quicker) and takes Deezer's ISRC. Low-popularity Deezer results go last."""
    unmatched = list(dz)
    yt_results = []
    for r in yt:
        match = next((t for t in unmatched if _same_song(r, t)), None)
        if match:
            unmatched.remove(match)
        yt_results.append(_from_yt(r, match.isrc if match else None))
    popular = [_from_deezer(t) for t in unmatched if (t.rank or 0) >= DEEZER_LOW_RANK]
    obscure = [_from_deezer(t) for t in unmatched if (t.rank or 0) < DEEZER_LOW_RANK]
    yt_pairs = [yt_results[i : i + 2] for i in range(0, len(yt_results), 2)]
    merged = [
        r
        for pair, extra in zip_longest(yt_pairs, popular, fillvalue=None)
        for r in [*(pair or []), extra]
        if r
    ]
    return (merged + obscure)[:limit]


async def search(q: str, country: str | None, client: httpx.AsyncClient) -> SearchResponse:
    yt, dz = await asyncio.gather(
        optional("ytmusic search", ytmusic.search_songs(q, country), settings.required_timeout_s),
        optional("deezer search", deezer.search(client, q), settings.required_timeout_s),
    )
    if yt is None and dz is None:
        raise NidolError(503, "search_unavailable")
    return SearchResponse(results=merge(yt or [], dz or [], settings.max_search_results))
