"""POST /resolve orchestration (walking skeleton: Spotify track links → YouTube candidates)."""

import asyncio
import logging
from collections.abc import Awaitable

import httpx

from nidol import scoring
from nidol.config import settings
from nidol.inputs import parse_input
from nidol.models import Canonical, NidolError, ResolveRequest, ResolveResponse
from nidol.sources import deezer, musicbrainz, spotify, ytmusic

log = logging.getLogger(__name__)


async def _optional[T](name: str, coro: Awaitable[T]) -> T | None:
    """Enrichment source (D11): a miss, error or timeout just means less data."""
    try:
        return await asyncio.wait_for(coro, settings.source_timeout_s)
    except Exception as e:  # noqa: BLE001 — enrichment must never fail the request
        log.warning("%s skipped: %r", name, e)
        return None


async def resolve(req: ResolveRequest, client: httpx.AsyncClient) -> ResolveResponse:
    parsed = parse_input(req.input)
    if parsed is None:
        raise NidolError(422, "unsupported_input")
    if parsed.kind != "track":
        raise NidolError(409, "collection_input", kind=parsed.kind)

    try:
        track = await asyncio.wait_for(
            spotify.fetch_track(client, parsed.id), settings.required_timeout_s
        )
    except spotify.SpotifyNotFound:
        raise NidolError(404, "not_found") from None
    except (httpx.HTTPError, TimeoutError, spotify.SpotifyParseError) as e:
        log.warning("spotify embed failed: %r", e)
        raise NidolError(502, "upstream_error", source="spotify") from None

    track_url = f"https://open.spotify.com/track/{track.id}"
    yt_task = asyncio.wait_for(
        ytmusic.search(f"{track.artists[0]} {track.title}", req.country),
        settings.required_timeout_s,
    )
    try:
        results, dz, mbid = await asyncio.gather(
            yt_task,
            _optional(
                "deezer",
                deezer.find_track(client, track.artists, track.title, track.duration_ms),
            ),
            _optional(
                "musicbrainz",
                musicbrainz.recording_for_url(client, track_url, settings.source_timeout_s / 2),
            ),
        )
    except Exception as e:  # noqa: BLE001 — ytmusicapi raises many types when it breaks
        log.warning("youtube search failed: %r", e)
        raise NidolError(503, "search_unavailable") from None

    canonical = Canonical(
        isrc=dz.isrc if dz else None,
        mbid=mbid,
        title=track.title,
        artists=track.artists,
        durationMs=track.duration_ms,
        year=track.year,
        explicit=track.explicit if track.explicit is not None else (dz.explicit if dz else None),
        artworkUrl=track.artwork_url or (dz.cover_url if dz else None),
    )
    candidates = scoring.rank(canonical, results, settings.max_candidates)
    if not candidates:
        raise NidolError(404, "no_match")
    return ResolveResponse(
        canonical=canonical,
        candidates=candidates,
        needsConfirmation=scoring.needs_confirmation(candidates, settings.confirm_threshold),
    )
