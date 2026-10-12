import json
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nidol import resolve as resolve_module
from nidol.app import app
from nidol.cache import NO_CACHE, Cache
from nidol.config import settings
from nidol.models import Candidate, Canonical
from nidol.sources import musicbrainz, ytmusic
from nidol.sources.musicbrainz import RateLimiter
from nidol.sources.ytmusic import YTResult, parse_song

FIXTURES = Path(__file__).parent / "fixtures"
SPOTIFY = "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"
EMBED = "https://open.spotify.com/embed/track/0DiWol3AO6WpXZgp0goxAV"
DEEZER = "https://www.deezer.com/track/3135553"
MBID = "60fa767a-d85d-4991-82bc-4294e0b11ae7"

CANONICAL = Canonical(title="One More Time", artists=["Daft Punk"], isrc="GBDUW0000053")
CANDS = [
    Candidate(
        videoId="wU26xVT_vBU",
        title="One More Time",
        kind="official_audio",
        thumbnailUrl="t",
        confidence=1.0,
    )
]
DEEZER_TRACK = {
    "id": 3135553,
    "title": "One More Time",
    "isrc": "GBDUW0000053",
    "duration": 320,
    "release_date": "2001-03-12",
    "explicit_lyrics": False,
    "artist": {"name": "Daft Punk"},
    "contributors": [{"name": "Daft Punk"}],
    "album": {"cover_xl": "c"},
}
DEEZER_SEARCH = {"data": [DEEZER_TRACK | {"contributors": None}]}
YT = [YTResult("wU26xVT_vBU", "One More Time", ["Daft Punk"], 320000, "official_audio")]


# --- Cache unit -----------------------------------------------------------------------------


def test_canonical_round_trip(tmp_path):
    cache = Cache(str(tmp_path / "sub" / "nidol.sqlite3"))
    assert cache.canonical("spotify:x") is None
    cache.put_canonical("spotify:x", CANONICAL)
    assert cache.canonical("spotify:x") == CANONICAL
    cache.close()
    assert Cache(str(tmp_path / "sub" / "nidol.sqlite3")).canonical("spotify:x") == CANONICAL


def test_candidates_by_any_key_and_country():
    cache = Cache(":memory:")
    cache.put_candidates(["spotify:x", "isrc:GB1"], "CL", CANDS)
    assert cache.candidates(["deezer:y", "isrc:GB1"], "CL") == CANDS
    assert cache.candidates(["spotify:x"], "AR") is None
    assert cache.candidates(["spotify:x"], None) is None


def test_candidates_expire(monkeypatch):
    cache = Cache(":memory:", candidates_ttl_s=60)
    cache.put_candidates(["k"], None, CANDS)
    assert cache.candidates(["k"], None) == CANDS
    later = time.time() + 61
    monkeypatch.setattr(time, "time", lambda: later)
    assert cache.candidates(["k"], None) is None


def test_no_cache_is_a_no_op():
    NO_CACHE.put_canonical("k", CANONICAL)
    NO_CACHE.put_candidates(["k"], None, CANDS)
    assert NO_CACHE.canonical("k") is None
    assert NO_CACHE.candidates(["k"], None) is None
    assert not NO_CACHE.enabled


# --- through the app ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_backfills():
    resolve_module._attempted.clear()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cache_path", str(tmp_path / "nidol.sqlite3"))
    monkeypatch.setattr(musicbrainz, "limiter", RateLimiter(interval=0))
    calls = {"search": 0, "get_video": 0}

    async def fake_search(query, country):
        calls["search"] += 1
        return YT

    async def fake_get_video(video_id):
        calls["get_video"] += 1
        name = {"FGBhQbmPwH8": "omv", "rFZHOHl-L8A": "live"}[video_id]
        return parse_song(
            video_id, json.loads((FIXTURES / f"ytmusic_get_song_{name}.json").read_text())
        )

    monkeypatch.setattr(ytmusic, "search", fake_search)
    monkeypatch.setattr(ytmusic, "get_video", fake_get_video)
    with respx.mock(assert_all_called=False) as mock, TestClient(app) as c:
        c.embed = mock.get(EMBED).respond(text=(FIXTURES / "spotify_embed_track.html").read_text())
        mock.get("https://api.deezer.com/search").respond(json=DEEZER_SEARCH)
        c.deezer_track = mock.get("https://api.deezer.com/track/3135553").respond(json=DEEZER_TRACK)
        mock.get(url__startswith="https://musicbrainz.org/ws/2/url").respond(
            json={"relations": [{"target-type": "recording", "recording": {"id": MBID}}]}
        )
        c.calls = calls
        yield c


def post(client, input_, **extra):
    r = client.post("/resolve", json={"input": input_, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_second_spotify_request_is_served_from_cache(client):
    first = post(client, SPOTIFY, country="CL")
    assert (client.embed.call_count, client.calls["search"]) == (1, 1)
    second = post(client, SPOTIFY, country="CL")
    assert (client.embed.call_count, client.calls["search"]) == (1, 1)
    assert second == first
    assert second["canonical"]["isrc"] == "GBDUW0000053"
    assert second["canonical"]["mbid"] == MBID


def test_other_country_reuses_canonical_but_searches_again(client):
    post(client, SPOTIFY, country="CL")
    post(client, SPOTIFY, country="AR")
    assert (client.embed.call_count, client.calls["search"]) == (1, 2)


def test_deezer_link_reuses_candidates_by_isrc(client):
    post(client, SPOTIFY)
    body = post(client, DEEZER)
    assert client.deezer_track.call_count == 1  # its canonical is new…
    assert client.calls["search"] == 1  # …but the candidates come from the Spotify link's ISRC
    assert body["candidates"][0]["videoId"] == "wU26xVT_vBU"


def test_spotify_link_reuses_candidates_cached_by_another_link(client):
    post(client, DEEZER)  # caches candidates under its ISRC
    body = post(client, SPOTIFY)
    assert client.embed.call_count == 1
    assert client.calls["search"] == 1  # Deezer found the same ISRC, so no second search
    assert body["canonical"]["isrc"] == "GBDUW0000053"
    assert body["candidates"][0]["videoId"] == "wU26xVT_vBU"


def test_youtube_cached_and_prefer_audio_still_applies(client):
    link = "https://youtu.be/FGBhQbmPwH8"
    first = post(client, link)
    assert client.calls == {"search": 1, "get_video": 1}
    again = post(client, link, preferAudio=True)
    assert client.calls == {"search": 1, "get_video": 1}
    assert first["candidates"][0]["videoId"] == "FGBhQbmPwH8"
    assert [c["videoId"] for c in again["candidates"]] == ["wU26xVT_vBU", "FGBhQbmPwH8"]


def test_live_streams_are_not_cached(client):
    link = "https://www.youtube.com/live/rFZHOHl-L8A"
    post(client, link)
    post(client, link)
    assert client.calls["get_video"] == 2


# --- backfill -------------------------------------------------------------------------------


async def test_backfill_fills_missing_mbid_and_isrc(monkeypatch):
    monkeypatch.setattr(musicbrainz, "limiter", RateLimiter(interval=0))
    cache = Cache(":memory:")
    key = "spotify:0DiWol3AO6WpXZgp0goxAV"
    cache.put_canonical(key, CANONICAL.model_copy(update={"isrc": None, "durationMs": 320357}))
    with respx.mock:
        respx.get(url__startswith="https://musicbrainz.org/ws/2/url").respond(
            json={"relations": [{"target-type": "recording", "recording": {"id": MBID}}]}
        )
        respx.get("https://api.deezer.com/search").respond(json=DEEZER_SEARCH)
        async with httpx.AsyncClient() as http:
            await resolve_module._backfill(cache, http, key, SPOTIFY)
    filled = cache.canonical(key)
    assert (filled.mbid, filled.isrc) == (MBID, "GBDUW0000053")


async def test_backfill_falls_back_to_isrc_lookup(monkeypatch):
    monkeypatch.setattr(musicbrainz, "limiter", RateLimiter(interval=0))
    cache = Cache(":memory:")
    cache.put_canonical("k", CANONICAL.model_copy(update={"durationMs": 320000}))
    with respx.mock:
        respx.get(url__startswith="https://musicbrainz.org/ws/2/url").respond(404)
        respx.get("https://musicbrainz.org/ws/2/isrc/GBDUW0000053").respond(
            json={
                "recordings": [
                    {"id": "radio-edit", "length": 235000},
                    {"id": "album-version", "length": 320746},
                ]
            }
        )
        async with httpx.AsyncClient() as http:
            await resolve_module._backfill(cache, http, "k", SPOTIFY)
    assert cache.canonical("k").mbid == "album-version"  # closest in length


async def test_backfill_failure_keeps_the_entry(monkeypatch):
    monkeypatch.setattr(musicbrainz, "limiter", RateLimiter(interval=0))
    cache = Cache(":memory:")
    cache.put_canonical("k", CANONICAL)
    with respx.mock:
        respx.get(url__startswith="https://musicbrainz.org").mock(
            side_effect=httpx.ConnectError("down")
        )
        async with httpx.AsyncClient() as http:
            await resolve_module._backfill(cache, http, "k", SPOTIFY)
    assert cache.canonical("k") == CANONICAL


async def test_backfill_is_attempted_once_per_link():
    cache = Cache(":memory:")
    cache.put_canonical("k", CANONICAL)  # has no MBID
    async with httpx.AsyncClient() as http:
        resolve_module._schedule_backfill(cache, http, "k", SPOTIFY)
        resolve_module._schedule_backfill(cache, http, "k", SPOTIFY)
        assert len(resolve_module._backfills) == 1
        await resolve_module.cancel_backfills()
