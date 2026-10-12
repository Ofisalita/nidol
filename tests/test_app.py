from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nidol.app import app
from nidol.sources import musicbrainz, ytmusic
from nidol.sources.musicbrainz import RateLimiter
from nidol.sources.ytmusic import YTResult

FIXTURES = Path(__file__).parent / "fixtures"
TRACK_URL = "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"
EMBED_URL = "https://open.spotify.com/embed/track/0DiWol3AO6WpXZgp0goxAV"
MBID = "60fa767a-d85d-4991-82bc-4294e0b11ae7"

DEEZER_SEARCH = {
    "data": [
        {
            "id": 3157685,
            "title": "One More Time (Short Radio Edit)",
            "artist": {"name": "Daft Punk"},
            "duration": 235,
            "isrc": "GBDUW0000054",
            "explicit_lyrics": False,
            "album": {"cover_xl": "https://cdn.example/short.jpg"},
        },
        {
            "id": 3135553,
            "title": "One More Time",
            "artist": {"name": "Daft Punk"},
            "duration": 320,
            "isrc": "GBDUW0000053",
            "explicit_lyrics": False,
            "album": {"cover_xl": "https://cdn.example/discovery.jpg"},
        },
    ]
}
MB_URL = {
    "relations": [{"target-type": "recording", "recording": {"id": MBID, "title": "One More Time"}}]
}
YT_RESULTS = [
    YTResult("FGBhQbmPwH8", "One More Time", ["Daft Punk"], 322000, "official_video"),
    YTResult("wU26xVT_vBU", "One More Time (Radio Edit)", ["Daft Punk"], 321000, "official_audio"),
]


@pytest.fixture
def client(monkeypatch):
    async def fake_search(query, country):
        assert query == "Daft Punk One More Time"
        return YT_RESULTS

    monkeypatch.setattr(ytmusic, "search", fake_search)
    monkeypatch.setattr(musicbrainz, "limiter", RateLimiter(interval=0))
    with respx.mock(assert_all_called=False) as mock, TestClient(app) as c:
        mock.get(EMBED_URL).respond(text=(FIXTURES / "spotify_embed_track.html").read_text())
        mock.get("https://api.deezer.com/search").respond(json=DEEZER_SEARCH)
        mock.get(url__startswith="https://musicbrainz.org/ws/2/url").respond(json=MB_URL)
        c.mock = mock
        yield c


def post(client, input_, **extra):
    return client.post("/resolve", json={"input": input_, **extra})


def test_resolve_spotify_track(client):
    r = post(client, TRACK_URL, country="CL")
    assert r.status_code == 200
    body = r.json()
    assert body["canonical"] == {
        "isrc": "GBDUW0000053",  # duration-matched, not the short edit
        "mbid": MBID,
        "title": "One More Time",
        "artists": ["Daft Punk"],
        "durationMs": 320357,
        "year": 2001,
        "explicit": False,
        "artworkUrl": body["canonical"]["artworkUrl"],
    }
    assert [c["videoId"] for c in body["candidates"]] == ["wU26xVT_vBU", "FGBhQbmPwH8"]
    assert body["candidates"][0]["kind"] == "official_audio"
    assert body["candidates"][0]["thumbnailUrl"].endswith("/wU26xVT_vBU/hqdefault.jpg")
    assert body["needsConfirmation"] is False


def test_enrichment_failures_are_not_fatal(client):
    client.mock.get("https://api.deezer.com/search").mock(side_effect=httpx.ConnectError("down"))
    client.mock.get(url__startswith="https://musicbrainz.org/ws/2/url").respond(503)
    r = post(client, TRACK_URL)
    assert r.status_code == 200
    assert r.json()["canonical"]["isrc"] is None
    assert r.json()["canonical"]["mbid"] is None


def test_collection_input(client):
    r = post(client, "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
    assert (r.status_code, r.json()) == (409, {"error": "collection_input", "kind": "playlist"})


def test_unsupported_input(client):
    r = post(client, "https://youtu.be/FGBhQbmPwH8")
    assert (r.status_code, r.json()) == (422, {"error": "unsupported_input"})


def test_spotify_not_found(client):
    client.mock.get(EMBED_URL).respond(text=(FIXTURES / "spotify_embed_404.html").read_text())
    assert post(client, TRACK_URL).json() == {"error": "not_found"}


def test_spotify_page_changed(client):
    client.mock.get(EMBED_URL).respond(text="<html>new layout</html>")
    r = post(client, TRACK_URL)
    assert (r.status_code, r.json()) == (502, {"error": "upstream_error", "source": "spotify"})


def test_youtube_search_down(client, monkeypatch):
    async def broken(query, country):
        raise RuntimeError("ytmusicapi broke")

    monkeypatch.setattr(ytmusic, "search", broken)
    r = post(client, TRACK_URL)
    assert (r.status_code, r.json()) == (503, {"error": "search_unavailable"})


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
