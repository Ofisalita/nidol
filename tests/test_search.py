import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nidol.app import app
from nidol.inputs import ParsedInput, parse_input
from nidol.search import merge
from nidol.sources import musicbrainz, ytmusic
from nidol.sources.deezer import _track
from nidol.sources.musicbrainz import RateLimiter
from nidol.sources.ytmusic import YTResult, _artwork

FIXTURES = Path(__file__).parent / "fixtures"
DEEZER_SEARCH = json.loads((FIXTURES / "deezer_search.json").read_text())
DEEZER_TRACK = json.loads((FIXTURES / "deezer_track.json").read_text())

# Live ytmusicapi songs for "feid luna" (country CL, 2026-10-12), trimmed.
YT_SONGS = [
    YTResult("aqs0aXBBC8g", "LUNA", ["Feid", "ATL Jacob"], 197000, "official_audio", False, "a1"),
    YTResult("_5A1wKdOfa0", "Si Te La Encuentras Por Ahí", ["Feid"], 192000, "official_audio"),
    YTResult("FWgaCxW0hW0", "Brickell", ["Yandel", "Feid"], 181000, "official_audio"),
]


def dz():
    return [_track(item) for item in DEEZER_SEARCH["data"]]


# --- merge ----------------------------------------------------------------------------------


def test_merge_dedupes_and_takes_deezer_isrc():
    results = merge(YT_SONGS, dz(), limit=10)
    luna = results[0]
    assert (luna.source.platform, luna.source.id) == ("youtube", "aqs0aXBBC8g")
    assert luna.isrc == "USUM72318216"  # from Deezer's LUNA (196 s vs 197 s)
    assert sum(r.title.casefold() == "luna" for r in results) == 1


def test_merge_order_two_yt_per_popular_deezer_then_obscure():
    results = merge(YT_SONGS, dz(), limit=10)
    assert [(r.source.platform, r.title) for r in results] == [
        ("youtube", "LUNA"),
        ("youtube", "Si Te La Encuentras Por Ahí"),
        ("deezer", "Felina"),  # rank 639935
        ("youtube", "Brickell"),
        # low-rank covers / remixes last
        ("deezer", "Feid - Luna (feat. SouliosG)"),
        ("deezer", "Feid - Luna (5Hart Remix)"),
    ]


def test_merge_limit():
    assert len(merge(YT_SONGS, dz(), limit=2)) == 2


def test_merge_with_one_source_only():
    assert [r.source.platform for r in merge([], dz(), limit=10)] == ["deezer"] * 4
    assert [r.source.platform for r in merge(YT_SONGS, [], limit=10)] == ["youtube"] * 3


def test_artwork_is_upsized():
    thumbs = [
        {"url": "https://yt3.googleusercontent.com/abc=w60-h60-l90-rj", "width": 60},
        {"url": "https://yt3.googleusercontent.com/abc=w120-h120-l90-rj", "width": 120},
    ]
    assert _artwork(thumbs) == "https://yt3.googleusercontent.com/abc=w544-h544-l90-rj"
    assert _artwork([]) is None


def test_deezer_track_lists_all_contributors():
    track = _track(DEEZER_TRACK)
    assert track.artists == ["Robin Schulz", "Felix Jaehn", "Alida"]
    assert (track.isrc, track.year, track.duration_ms) == ("DEA622001260", 2021, 199000)


# --- GET /search ----------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def fake_search_songs(query, country):
        calls.append((query, country))
        return YT_SONGS

    async def fake_search(query, country):
        return YT_SONGS

    monkeypatch.setattr(ytmusic, "search_songs", fake_search_songs)
    monkeypatch.setattr(ytmusic, "search", fake_search)
    monkeypatch.setattr(musicbrainz, "limiter", RateLimiter(interval=0))
    with respx.mock(assert_all_called=False) as mock, TestClient(app) as c:
        mock.get("https://api.deezer.com/search").respond(json=DEEZER_SEARCH)
        mock.get("https://api.deezer.com/track/1233990342").respond(json=DEEZER_TRACK)
        mock.get(url__startswith="https://musicbrainz.org/ws/2/url").respond(404)
        c.mock = mock
        c.calls = calls
        yield c


def test_search(client):
    r = client.get("/search", params={"q": "feid luna", "country": "CL"})
    assert r.status_code == 200
    first = r.json()["results"][0]
    assert first == {
        "title": "LUNA",
        "artists": ["Feid", "ATL Jacob"],
        "durationMs": 197000,
        "artworkUrl": "a1",
        "isrc": "USUM72318216",
        "source": {
            "platform": "youtube",
            "id": "aqs0aXBBC8g",
            "url": "https://music.youtube.com/watch?v=aqs0aXBBC8g",
        },
    }
    assert client.calls == [("feid luna", "CL")]


def test_search_every_result_is_resolvable(client):
    for result in client.get("/search", params={"q": "feid luna"}).json()["results"]:
        parsed = parse_input(result["source"]["url"])
        assert parsed is not None and parsed.kind == "track"
        assert parsed.id == result["source"]["id"]


def test_search_survives_one_source_down(client, monkeypatch):
    async def broken(query, country):
        raise RuntimeError("ytmusicapi broke")

    monkeypatch.setattr(ytmusic, "search_songs", broken)
    results = client.get("/search", params={"q": "feid luna"}).json()["results"]
    assert {r["source"]["platform"] for r in results} == {"deezer"}


def test_search_both_down_is_503(client, monkeypatch):
    async def broken(query, country):
        raise RuntimeError("ytmusicapi broke")

    monkeypatch.setattr(ytmusic, "search_songs", broken)
    client.mock.get("https://api.deezer.com/search").mock(side_effect=httpx.ConnectError("down"))
    r = client.get("/search", params={"q": "feid luna"})
    assert (r.status_code, r.json()) == (503, {"error": "search_unavailable"})


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "x", "country": "chile"}])
def test_search_bad_params(client, params):
    assert client.get("/search", params=params).status_code == 422


# --- POST /resolve with Deezer track links ----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.deezer.com/track/1233990342",
        "https://deezer.com/en/track/1233990342?utm=x",
        "https://www.deezer.com/pt-br/track/1233990342",
    ],
)
def test_parse_deezer_track(raw):
    assert parse_input(raw) == ParsedInput("deezer", "track", "1233990342")


def test_deezer_collections_are_unsupported_for_now():
    assert parse_input("https://www.deezer.com/album/302127") is None


def test_resolve_deezer_track(client):
    r = client.post("/resolve", json={"input": "https://www.deezer.com/track/1233990342"})
    assert r.status_code == 200
    canonical = r.json()["canonical"]
    assert canonical["isrc"] == "DEA622001260"
    assert canonical["artists"] == ["Robin Schulz", "Felix Jaehn", "Alida"]
    assert canonical["durationMs"] == 199000
    assert canonical["year"] == 2021
    # The ISRC came with the track, so no Deezer search was needed.
    assert not client.mock.routes[0].called


def test_resolve_deezer_not_found(client):
    client.mock.get("https://api.deezer.com/track/1").respond(
        json={"error": {"type": "DataException", "message": "no data", "code": 800}}
    )
    r = client.post("/resolve", json={"input": "https://www.deezer.com/track/1"})
    assert (r.status_code, r.json()) == (404, {"error": "not_found"})


def test_resolve_deezer_down(client):
    client.mock.get("https://api.deezer.com/track/2").mock(side_effect=httpx.ConnectError("down"))
    r = client.post("/resolve", json={"input": "https://www.deezer.com/track/2"})
    assert (r.status_code, r.json()) == (502, {"error": "upstream_error", "source": "deezer"})
