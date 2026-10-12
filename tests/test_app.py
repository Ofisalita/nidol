import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nidol.app import app
from nidol.sources import musicbrainz, ytmusic
from nidol.sources.musicbrainz import RateLimiter
from nidol.sources.ytmusic import YTResult, parse_song

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
        "isLive": False,
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
    r = post(client, "https://soundcloud.com/daftpunk/one-more-time")
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


# --- YouTube / YT Music links -------------------------------------------------------------

VIDEO_URL = "https://music.youtube.com/watch?v=FGBhQbmPwH8"
OEMBED = "https://www.youtube.com/oembed"


def song(name):
    return json.loads((FIXTURES / f"ytmusic_get_song_{name}.json").read_text())


@pytest.fixture
def yt_client(client, monkeypatch):
    """`client` plus get_song answering from recorded fixtures, keyed by video ID."""
    songs = {"FGBhQbmPwH8": "omv", "B4LvDiIi128": "ten_hours", "rFZHOHl-L8A": "live"}
    searches = []

    async def fake_get_video(video_id):
        return parse_song(video_id, song(songs.get(video_id, "unavailable")))

    async def fake_search(query, country):
        searches.append(query)
        return YT_RESULTS

    monkeypatch.setattr(ytmusic, "get_video", fake_get_video)
    monkeypatch.setattr(ytmusic, "search", fake_search)
    client.searches = searches
    return client


def test_youtube_link_is_first_candidate_with_its_duration(yt_client):
    r = post(yt_client, VIDEO_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["canonical"]["title"] == "One More Time"
    assert body["canonical"]["artists"] == ["Daft Punk"]
    assert body["canonical"]["durationMs"] == 321000
    assert body["canonical"]["isLive"] is False
    first = body["candidates"][0]
    assert (first["videoId"], first["confidence"], first["kind"]) == (
        "FGBhQbmPwH8",
        1.0,
        "official_video",
    )
    assert first["channel"] == "Daft Punk"
    assert body["needsConfirmation"] is False
    # Same-song official audio follows, for the player to fall through to.
    assert [c["videoId"] for c in body["candidates"][1:]] == ["wU26xVT_vBU"]
    assert yt_client.searches == ["Daft Punk One More Time"]


def test_youtube_alternates_are_official_same_length_only(yt_client, monkeypatch):
    async def search(query, country):
        return [
            YTResult("short", "One More Time", ["Daft Punk"], 236000, "official_audio"),  # edit
            YTResult("reup", "Daft Punk - One More Time", ["someone"], 321000, "user_upload"),
            YTResult("cover", "One More Time", ["Disc Jockey's EDM"], 321000, "official_audio"),
            YTResult("wU26xVT_vBU", "One More Time", ["Daft Punk"], 320000, "official_audio"),
        ]

    monkeypatch.setattr(ytmusic, "search", search)
    body = post(yt_client, VIDEO_URL).json()
    assert [c["videoId"] for c in body["candidates"]] == ["FGBhQbmPwH8", "wU26xVT_vBU"]


def test_youtube_prefer_audio_puts_official_audio_first(yt_client):
    body = post(yt_client, VIDEO_URL, preferAudio=True).json()
    assert [c["videoId"] for c in body["candidates"]] == ["wU26xVT_vBU", "FGBhQbmPwH8"]


def test_ten_hour_video_reports_its_length(yt_client):
    body = post(yt_client, "https://youtu.be/B4LvDiIi128").json()
    assert body["canonical"]["durationMs"] == 36_000_000
    assert body["candidates"][0]["videoId"] == "B4LvDiIi128"


def test_live_stream_is_flagged_without_duration(yt_client):
    body = post(yt_client, "https://www.youtube.com/live/rFZHOHl-L8A").json()
    assert body["canonical"]["durationMs"] is None
    assert body["canonical"]["isLive"] is True
    assert [c["videoId"] for c in body["candidates"]] == ["rFZHOHl-L8A"]
    assert yt_client.searches == []  # no alternates for a live stream


def test_unavailable_video(yt_client):
    r = post(yt_client, "https://youtu.be/aaaaaaaaaaa")
    assert (r.status_code, r.json()) == (404, {"error": "not_found"})


def test_youtube_alternate_search_failure_still_returns_the_video(yt_client, monkeypatch):
    async def broken(query, country):
        raise RuntimeError("ytmusicapi search broke")

    monkeypatch.setattr(ytmusic, "search", broken)
    body = post(yt_client, VIDEO_URL).json()
    assert [c["videoId"] for c in body["candidates"]] == ["FGBhQbmPwH8"]
    assert body["canonical"]["durationMs"] == 321000


def test_get_song_failure_falls_back_to_oembed_without_duration(yt_client, monkeypatch):
    async def broken(video_id):
        raise RuntimeError("ytmusicapi broke")

    monkeypatch.setattr(ytmusic, "get_video", broken)
    yt_client.mock.get(OEMBED).respond(
        json={"title": "Daft Punk - One More Time (Official Video)", "author_name": "Daft Punk"}
    )
    r = post(yt_client, VIDEO_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["canonical"]["durationMs"] is None
    assert body["canonical"]["title"] == "Daft Punk - One More Time (Official Video)"
    assert body["candidates"][0]["videoId"] == "FGBhQbmPwH8"


def test_everything_down_is_a_5xx(yt_client, monkeypatch):
    async def broken(video_id):
        raise RuntimeError("ytmusicapi broke")

    monkeypatch.setattr(ytmusic, "get_video", broken)
    yt_client.mock.get(OEMBED).mock(side_effect=httpx.ConnectError("down"))
    r = post(yt_client, VIDEO_URL)
    assert (r.status_code, r.json()) == (502, {"error": "upstream_error", "source": "youtube"})


def test_youtube_playlist_is_a_collection(yt_client):
    r = post(yt_client, "https://music.youtube.com/playlist?list=PLx")
    assert (r.status_code, r.json()) == (409, {"error": "collection_input", "kind": "playlist"})
