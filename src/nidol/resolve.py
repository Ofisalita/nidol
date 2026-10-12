"""POST /resolve orchestration: Spotify track links and YouTube / YT Music video links."""

import asyncio
import logging

import httpx

from nidol import scoring
from nidol.config import settings
from nidol.deadlines import optional
from nidol.inputs import parse_input
from nidol.models import Candidate, Canonical, NidolError, ResolveRequest, ResolveResponse
from nidol.sources import deezer, musicbrainz, spotify, youtube, ytmusic

log = logging.getLogger(__name__)


def _thumbnail(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


async def resolve(req: ResolveRequest, client: httpx.AsyncClient) -> ResolveResponse:
    parsed = parse_input(req.input)
    if parsed is None:
        raise NidolError(422, "unsupported_input")
    if parsed.kind != "track":
        raise NidolError(409, "collection_input", kind=parsed.kind)
    if parsed.platform == "youtube":
        return await _resolve_youtube(req, client, parsed.id)
    return await _resolve_spotify(req, client, parsed.id)


async def _resolve_spotify(
    req: ResolveRequest, client: httpx.AsyncClient, track_id: str
) -> ResolveResponse:
    try:
        track = await asyncio.wait_for(
            spotify.fetch_track(client, track_id), settings.required_timeout_s
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
            optional(
                "deezer",
                deezer.find_track(client, track.artists, track.title, track.duration_ms),
            ),
            optional(
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


async def _resolve_youtube(
    req: ResolveRequest, client: httpx.AsyncClient, video_id: str
) -> ResolveResponse:
    """The guest picked this exact video, so it is candidates[0] with confidence 1 (D7, D18).
    Same-song official uploads follow it, for the player to fall through to on embed errors."""
    video: ytmusic.YTVideo | None = None
    try:
        video = await asyncio.wait_for(ytmusic.get_video(video_id), settings.required_timeout_s)
    except ytmusic.VideoUnavailable:
        raise NidolError(404, "not_found") from None
    except Exception as e:  # noqa: BLE001 — ytmusicapi raises many types when it breaks
        log.warning("ytmusic get_song failed, falling back to oEmbed: %r", e)

    if video is None:
        # No duration from oEmbed: bye0 treats a null durationMs as unknown.
        try:
            meta = await asyncio.wait_for(
                youtube.oembed(client, video_id), settings.source_timeout_s
            )
        except (httpx.HTTPError, TimeoutError) as e:
            log.warning("youtube oembed failed: %r", e)
            raise NidolError(502, "upstream_error", source="youtube") from None
        if meta is None:
            raise NidolError(404, "not_found")
        video = ytmusic.YTVideo(video_id, meta.title, meta.author, None, False, "other")

    canonical = Canonical(
        title=video.title,
        artists=[video.author] if video.author else [],
        durationMs=video.duration_ms,
        artworkUrl=_thumbnail(video_id),
        isLive=video.is_live,
    )
    pasted = Candidate(
        videoId=video_id,
        title=video.title,
        channel=video.author or None,
        durationMs=video.duration_ms,
        kind=video.kind,  # type: ignore[arg-type]
        thumbnailUrl=_thumbnail(video_id),
        confidence=1.0,
    )
    alternates = [] if video.is_live else await _alternates(canonical, video, req.country)
    candidates = [pasted, *alternates]
    if req.preferAudio and pasted.kind != "official_audio":
        audio = next((c for c in alternates if c.kind == "official_audio"), None)
        if audio:
            candidates.remove(audio)
            candidates.insert(0, audio)
    return ResolveResponse(canonical=canonical, candidates=candidates, needsConfirmation=False)


async def _alternates(
    canonical: Canonical, video: ytmusic.YTVideo, country: str | None
) -> list[Candidate]:
    """Official uploads of the same recording: a fall-through target must not be a different
    song, edit or reupload."""
    # For official uploads the author is the artist; uploaders' names would only add noise.
    official = video.kind in scoring.AUTHORITATIVE_ARTISTS
    query = f"{video.author} {video.title}" if official else video.title
    results = await optional(
        "youtube alternates", ytmusic.search(query, country), settings.required_timeout_s
    )
    same_recording = [
        r
        for r in results or []
        if r.video_id != video.video_id
        and r.kind in scoring.AUTHORITATIVE_ARTISTS
        and scoring.same_artist(canonical.artists, r)  # not a soundalike "- Topic" cover
        and _same_length(r.duration_ms, video.duration_ms)
    ]
    ranked = scoring.rank(canonical, same_recording, limit=settings.max_candidates - 1)
    return [c for c in ranked if c.confidence >= settings.confirm_threshold]


def _same_length(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= scoring.DIFFERENT_RECORDING_MS
