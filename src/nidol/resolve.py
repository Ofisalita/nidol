"""POST /resolve orchestration: Spotify and Deezer track links, YouTube / YT Music video links."""

import asyncio
import logging

import httpx

from nidol import scoring
from nidol.cache import NO_CACHE, Cache
from nidol.config import settings
from nidol.deadlines import optional
from nidol.inputs import parse_input
from nidol.models import Candidate, Canonical, NidolError, ResolveRequest, ResolveResponse
from nidol.sources import deezer, musicbrainz, spotify, youtube, ytmusic

log = logging.getLogger(__name__)


# Background ISRC/MBID backfills (D11), one attempt per link per process: MusicBrainz has no
# relation for many tracks, and retrying on every request would just burn its rate limit.
_backfills: set[asyncio.Task] = set()
_attempted: set[str] = set()


def _schedule_backfill(cache: Cache, client: httpx.AsyncClient, key: str, url: str) -> None:
    canonical = cache.canonical(key)
    if canonical is None or (canonical.isrc and canonical.mbid) or key in _attempted:
        return
    _attempted.add(key)
    task = asyncio.create_task(_backfill(cache, client, key, url))
    _backfills.add(task)
    task.add_done_callback(_backfills.discard)


async def _backfill(cache: Cache, client: httpx.AsyncClient, key: str, url: str) -> None:
    """Fill in what missed the request deadline. Waits its turn in the MusicBrainz limiter."""
    current = cache.canonical(key)
    if current is None:
        return
    update: dict[str, str] = {}
    try:
        isrc = current.isrc
        if isrc is None:
            dz = await deezer.find_track(client, current.artists, current.title, current.durationMs)
            if dz and dz.isrc:
                isrc = update["isrc"] = dz.isrc
        if current.mbid is None:
            wait = settings.backfill_wait_s
            mbid = await musicbrainz.recording_for_url(client, url, wait)
            if mbid is None and isrc:
                mbid = await musicbrainz.recording_for_isrc(client, isrc, current.durationMs, wait)
            if mbid:
                update["mbid"] = mbid
    except Exception as e:  # noqa: BLE001 — a failed backfill only means the gap stays
        log.warning("backfill %s failed: %r", key, e)
    if update:
        latest = cache.canonical(key) or current
        cache.put_canonical(key, latest.model_copy(update=update))


async def cancel_backfills() -> None:
    for task in list(_backfills):
        task.cancel()
    await asyncio.gather(*_backfills, return_exceptions=True)


def _thumbnail(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


async def resolve(
    req: ResolveRequest, client: httpx.AsyncClient, cache: Cache = NO_CACHE
) -> ResolveResponse:
    parsed = parse_input(req.input)
    if parsed is None:
        raise NidolError(422, "unsupported_input")
    if parsed.kind != "track":
        raise NidolError(409, "collection_input", kind=parsed.kind)
    match parsed.platform:
        case "youtube":
            return await _resolve_youtube(req, client, cache, parsed.id)
        case "deezer":
            return await _resolve_deezer(req, client, cache, parsed.id)
        case _:
            return await _resolve_spotify(req, client, cache, parsed.id)


async def _resolve_spotify(
    req: ResolveRequest, client: httpx.AsyncClient, cache: Cache, track_id: str
) -> ResolveResponse:
    key, url = f"spotify:{track_id}", f"https://open.spotify.com/track/{track_id}"
    if known := cache.canonical(key):
        return await _match(req, client, cache, key, known, url)
    try:
        track = await asyncio.wait_for(
            spotify.fetch_track(client, track_id), settings.required_timeout_s
        )
    except spotify.SpotifyNotFound:
        raise NidolError(404, "not_found") from None
    except (httpx.HTTPError, TimeoutError, spotify.SpotifyParseError) as e:
        log.warning("spotify embed failed: %r", e)
        raise NidolError(502, "upstream_error", source="spotify") from None
    known = Canonical(
        title=track.title,
        artists=track.artists,
        durationMs=track.duration_ms,
        year=track.year,
        explicit=track.explicit,
        artworkUrl=track.artwork_url,
    )
    return await _match(req, client, cache, key, known, url)


async def _resolve_deezer(
    req: ResolveRequest, client: httpx.AsyncClient, cache: Cache, track_id: str
) -> ResolveResponse:
    key, url = f"deezer:{track_id}", f"https://www.deezer.com/track/{track_id}"
    if known := cache.canonical(key):
        return await _match(req, client, cache, key, known, url)
    try:
        track = await asyncio.wait_for(
            deezer.get_track(client, track_id), settings.required_timeout_s
        )
    except deezer.DeezerNotFound:
        raise NidolError(404, "not_found") from None
    except (httpx.HTTPError, TimeoutError, KeyError, ValueError) as e:
        log.warning("deezer track failed: %r", e)
        raise NidolError(502, "upstream_error", source="deezer") from None
    known = Canonical(
        isrc=track.isrc,
        title=track.title,
        artists=track.artists,
        durationMs=track.duration_ms,
        year=track.year,
        explicit=track.explicit,
        artworkUrl=track.cover_url,
    )
    return await _match(req, client, cache, key, known, url)


def _keys(key: str, canonical: Canonical) -> list[str]:
    return [key, f"isrc:{canonical.isrc}"] if canonical.isrc else [key]


def _enrichment(known: Canonical, dz: deezer.DeezerTrack | None, mbid: str | None) -> dict:
    """Fields Deezer / MusicBrainz add to what the link's own platform gave us."""
    return {
        "isrc": known.isrc or (dz.isrc if dz else None),
        "mbid": known.mbid or mbid,
        "explicit": known.explicit if known.explicit is not None else (dz.explicit if dz else None),
        "artworkUrl": known.artworkUrl or (dz.cover_url if dz else None),
    }


def _response(canonical: Canonical, candidates: list[Candidate]) -> ResolveResponse:
    return ResolveResponse(
        canonical=canonical,
        candidates=candidates,
        needsConfirmation=scoring.needs_confirmation(candidates, settings.confirm_threshold),
    )


async def _match(
    req: ResolveRequest,
    client: httpx.AsyncClient,
    cache: Cache,
    key: str,
    known: Canonical,
    track_url: str,
) -> ResolveResponse:
    """Known track metadata → YouTube candidates, filling in ISRC (Deezer) and MBID (MusicBrainz)
    when they arrive within their deadlines (D10, D11), or later in the background."""
    if cached := cache.candidates(_keys(key, known), req.country):
        cache.put_canonical(key, known)
        _schedule_backfill(cache, client, key, track_url)
        return _response(known, cached)

    # The calls are created inside the tasks, so cancelling one before it starts (the ISRC
    # shortcut below) doesn't leave an un-awaited coroutine behind.
    async def search() -> list[ytmusic.YTResult]:
        return await asyncio.wait_for(
            ytmusic.search(f"{known.artists[0]} {known.title}", req.country),
            settings.required_timeout_s,
        )

    async def mbid_lookup() -> str | None:
        return await optional(
            "musicbrainz",
            musicbrainz.recording_for_url(client, track_url, settings.source_timeout_s / 2),
        )

    yt_task = asyncio.create_task(search())
    mb_task = asyncio.create_task(mbid_lookup())
    dz = None
    if not known.isrc:
        # Deezer is only needed for the ISRC (a Deezer link already has it). It usually answers
        # before the YouTube search, and if another link to the same recording already has
        # cached candidates, the search isn't needed at all.
        dz = await optional(
            "deezer", deezer.find_track(client, known.artists, known.title, known.durationMs)
        )
        if dz and dz.isrc and (cached := cache.candidates([f"isrc:{dz.isrc}"], req.country)):
            yt_task.cancel()
            mb_task.cancel()  # the backfill below fills the MBID without holding the response
            canonical = known.model_copy(update=_enrichment(known, dz, None))
            cache.put_canonical(key, canonical)
            cache.put_candidates([key], req.country, cached)
            _schedule_backfill(cache, client, key, track_url)
            return _response(canonical, cached)
    try:
        results = await yt_task
    except Exception as e:  # noqa: BLE001 — ytmusicapi raises many types when it breaks
        mb_task.cancel()
        log.warning("youtube search failed: %r", e)
        raise NidolError(503, "search_unavailable") from None
    mbid = await mb_task

    canonical = known.model_copy(update=_enrichment(known, dz, mbid))
    candidates = scoring.rank(canonical, results, settings.max_candidates)
    if not candidates:
        raise NidolError(404, "no_match")
    cache.put_canonical(key, canonical)
    cache.put_candidates(_keys(key, canonical), req.country, candidates)
    _schedule_backfill(cache, client, key, track_url)
    return _response(canonical, candidates)


def _youtube_response(
    canonical: Canonical, candidates: list[Candidate], prefer_audio: bool
) -> ResolveResponse:
    if prefer_audio and candidates[0].kind != "official_audio":
        audio = next((c for c in candidates[1:] if c.kind == "official_audio"), None)
        if audio:
            candidates = [audio, *(c for c in candidates if c is not audio)]
    return ResolveResponse(canonical=canonical, candidates=candidates, needsConfirmation=False)


async def _resolve_youtube(
    req: ResolveRequest, client: httpx.AsyncClient, cache: Cache, video_id: str
) -> ResolveResponse:
    """The guest picked this exact video, so it is candidates[0] with confidence 1 (D7, D18).
    Same-song official uploads follow it, for the player to fall through to on embed errors."""
    key = f"youtube:{video_id}"
    if (canonical := cache.canonical(key)) and (cached := cache.candidates([key], req.country)):
        return _youtube_response(canonical, cached, req.preferAudio)

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
    candidates = [pasted, *(alternates or [])]
    # Not cached: live streams (they end and become normal videos), oEmbed fallbacks (no
    # duration), and results whose alternates search failed.
    if not video.is_live and video.duration_ms is not None and alternates is not None:
        cache.put_canonical(key, canonical)
        cache.put_candidates([key], req.country, candidates)
    return _youtube_response(canonical, candidates, req.preferAudio)


async def _alternates(
    canonical: Canonical, video: ytmusic.YTVideo, country: str | None
) -> list[Candidate] | None:
    """Official uploads of the same recording: a fall-through target must not be a different
    song, edit or reupload. None when the search itself failed."""
    # For official uploads the author is the artist; uploaders' names would only add noise.
    official = video.kind in scoring.AUTHORITATIVE_ARTISTS
    query = f"{video.author} {video.title}" if official else video.title
    results = await optional(
        "youtube alternates", ytmusic.search(query, country), settings.required_timeout_s
    )
    if results is None:
        return None
    same_recording = [
        r
        for r in results
        if r.video_id != video.video_id
        and r.kind in scoring.AUTHORITATIVE_ARTISTS
        and scoring.same_artist(canonical.artists, r)  # not a soundalike "- Topic" cover
        and _same_length(r.duration_ms, video.duration_ms)
    ]
    ranked = scoring.rank(canonical, same_recording, limit=settings.max_candidates - 1)
    return [c for c in ranked if c.confidence >= settings.confirm_threshold]


def _same_length(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= scoring.DIFFERENT_RECORDING_MS
