"""POST /expand (D9): album/playlist link → lightweight track stubs, resolved lazily by bye0."""

import asyncio
import logging

import httpx

from nidol.config import settings
from nidol.deadlines import optional
from nidol.inputs import ParsedInput, parse_input
from nidol.models import (
    Collection,
    ExpandRequest,
    ExpandResponse,
    ExpandTrack,
    NidolError,
    Source,
)
from nidol.sources import spotify, ytmusic

log = logging.getLogger(__name__)


async def expand(req: ExpandRequest, client: httpx.AsyncClient) -> ExpandResponse:
    parsed = parse_input(req.input)
    if parsed is None or parsed.kind == "track":
        raise NidolError(422, "unsupported_input")
    if parsed.platform == "spotify":
        return await _expand_spotify(client, parsed)
    return await _expand_youtube(parsed)


async def _expand_spotify(client: httpx.AsyncClient, parsed: ParsedInput) -> ExpandResponse:
    limit = settings.max_expand_tracks
    try:
        coll = await asyncio.wait_for(
            spotify.fetch_collection(client, parsed.kind, parsed.id), settings.required_timeout_s
        )
    except spotify.SpotifyNotFound:
        raise NidolError(404, "not_found") from None
    except (httpx.HTTPError, TimeoutError, spotify.SpotifyParseError) as e:
        log.warning("spotify embed failed: %r", e)
        raise NidolError(502, "upstream_error", source="spotify") from None

    # The embed stops at 100 tracks without saying whether there were more.
    truncated = len(coll.tracks) > limit
    if len(coll.tracks) >= limit:
        total = await optional(
            "spotify song count", spotify.song_count(client, parsed.kind, parsed.id)
        )
        truncated = total is None or total > limit
    tracks = [
        ExpandTrack(
            title=t.title,
            artists=[t.artist_line] if t.artist_line else [],
            durationMs=t.duration_ms,
            explicit=t.explicit,
            source=Source(
                platform="spotify", id=t.id, url=f"https://open.spotify.com/track/{t.id}"
            ),
        )
        for t in coll.tracks[:limit]
    ]
    return ExpandResponse(
        collection=Collection(kind=parsed.kind, platform="spotify", title=coll.title),  # type: ignore[arg-type]
        tracks=tracks,
        truncated=truncated,
    )


async def _expand_youtube(parsed: ParsedInput) -> ExpandResponse:
    limit = settings.max_expand_tracks
    try:
        coll = await asyncio.wait_for(
            ytmusic.get_collection(parsed.id, limit), settings.required_timeout_s
        )
    except KeyError:
        raise NidolError(404, "not_found") from None
    except Exception as e:  # noqa: BLE001 — ytmusicapi raises many types when it breaks
        log.warning("ytmusic collection failed: %r", e)
        raise NidolError(502, "upstream_error", source="youtube") from None

    tracks = [
        ExpandTrack(
            title=t.title,
            artists=t.artists,
            durationMs=t.duration_ms,
            explicit=t.explicit,
            source=Source(
                platform="youtube",
                id=t.video_id,
                url=f"https://music.youtube.com/watch?v={t.video_id}",
            ),
        )
        for t in coll.tracks[:limit]
    ]
    truncated = len(coll.tracks) > limit or (coll.total is not None and coll.total > limit)
    return ExpandResponse(
        collection=Collection(kind=parsed.kind, platform="youtube", title=coll.title),  # type: ignore[arg-type]
        tracks=tracks,
        truncated=truncated,
    )
