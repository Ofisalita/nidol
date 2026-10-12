import json
import re
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from nidol.app import app
from nidol.inputs import ParsedInput, parse_input
from nidol.sources import ytmusic
from nidol.sources.ytmusic import parse_collection

FIXTURES = Path(__file__).parent / "fixtures"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
ALBUM_ID = "2noRn2Aes5aoNVsU6iWThc"
EMBED = "https://open.spotify.com/embed/{kind}/{id}"


def fixture(name):
    return (FIXTURES / name).read_text()


def spotify_embed_with(n_tracks):
    """The playlist fixture, padded to `n_tracks` tracks with distinct IDs."""
    html = fixture("spotify_embed_playlist.html")
    data = json.loads(re.search(r'json">(.*)</script>', html).group(1))
    entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    template = entity["trackList"][0]
    entity["trackList"] = [{**template, "uri": f"spotify:track:{i:022d}"} for i in range(n_tracks)]
    return html.replace(re.search(r'json">(.*)</script>', html).group(1), json.dumps(data))


@pytest.fixture
def client(monkeypatch):
    lists = {"MPREb_7ltM34kr0mH": "ytmusic_get_album.json", "PLx": "ytmusic_get_playlist.json"}

    async def fake_get_collection(id_, limit):
        if id_ not in lists:
            raise KeyError("Unable to find 'contents'")
        return parse_collection(json.loads(fixture(lists[id_])))

    monkeypatch.setattr(ytmusic, "get_collection", fake_get_collection)
    with respx.mock(assert_all_called=False) as mock, TestClient(app) as c:
        mock.get(EMBED.format(kind="playlist", id=PLAYLIST_ID)).respond(
            text=fixture("spotify_embed_playlist.html")
        )
        mock.get(EMBED.format(kind="album", id=ALBUM_ID)).respond(
            text=fixture("spotify_embed_album.html")
        )
        c.mock = mock
        yield c


def post(client, input_, **extra):
    return client.post("/expand", json={"input": input_, **extra})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://music.youtube.com/browse/MPREb_7ltM34kr0mH", ("album", "MPREb_7ltM34kr0mH")),
        ("https://music.youtube.com/playlist?list=OLAK5uy_abc", ("album", "OLAK5uy_abc")),
        ("https://www.youtube.com/playlist?list=PLabc", ("playlist", "PLabc")),
    ],
)
def test_parse_youtube_collections(raw, expected):
    assert parse_input(raw) == ParsedInput("youtube", *expected)


def test_parse_radio_mix_is_unsupported():
    assert parse_input("https://music.youtube.com/playlist?list=RDCLAK5uy_abc") is None


def test_spotify_playlist(client):
    r = post(client, f"https://open.spotify.com/playlist/{PLAYLIST_ID}", country="CL")
    assert r.status_code == 200
    body = r.json()
    assert body["collection"] == {
        "kind": "playlist",
        "platform": "spotify",
        "title": "Today’s Top Hits",
    }
    assert body["truncated"] is False
    assert body["tracks"][0] == {
        "title": "Stop The Wedding!",
        "artists": ["Ashe"],
        "durationMs": 198840,
        "explicit": False,
        "source": {
            "platform": "spotify",
            "id": "3zOYmiNOuyDL0ClxJcEJ0T",
            "url": "https://open.spotify.com/track/3zOYmiNOuyDL0ClxJcEJ0T",
        },
    }


def test_spotify_album(client):
    body = post(client, f"https://open.spotify.com/album/{ALBUM_ID}").json()
    assert body["collection"]["kind"] == "album"
    assert body["collection"]["title"] == "Discovery"
    assert [t["title"] for t in body["tracks"]] == ["One More Time", "Aerodynamic", "Digital Love"]


@pytest.mark.parametrize(
    ("page", "truncated"),
    [
        ('<meta name="music:song_count" content="150"/>', True),
        ('<meta name="music:song_count" content="100"/>', False),
        ("<html>no count</html>", True),  # unknown: assume there were more
    ],
)
def test_spotify_100_tracks_checks_the_real_count(client, page, truncated):
    url = EMBED.format(kind="playlist", id=PLAYLIST_ID)
    client.mock.get(url).respond(text=spotify_embed_with(100))
    client.mock.get(f"https://open.spotify.com/playlist/{PLAYLIST_ID}").respond(text=page)
    body = post(client, f"https://open.spotify.com/playlist/{PLAYLIST_ID}").json()
    assert len(body["tracks"]) == 100
    assert body["truncated"] is truncated


def test_spotify_count_page_down_means_truncated(client):
    client.mock.get(EMBED.format(kind="playlist", id=PLAYLIST_ID)).respond(
        text=spotify_embed_with(100)
    )
    client.mock.get(f"https://open.spotify.com/playlist/{PLAYLIST_ID}").mock(
        side_effect=httpx.ConnectError("down")
    )
    assert post(client, f"https://open.spotify.com/playlist/{PLAYLIST_ID}").json()["truncated"]


def test_spotify_collection_not_found(client):
    client.mock.get(EMBED.format(kind="album", id=ALBUM_ID)).respond(
        text=fixture("spotify_embed_404.html")
    )
    r = post(client, f"https://open.spotify.com/album/{ALBUM_ID}")
    assert (r.status_code, r.json()) == (404, {"error": "not_found"})


def test_spotify_page_changed(client):
    client.mock.get(EMBED.format(kind="album", id=ALBUM_ID)).respond(text="<html></html>")
    r = post(client, f"https://open.spotify.com/album/{ALBUM_ID}")
    assert (r.status_code, r.json()) == (502, {"error": "upstream_error", "source": "spotify"})


def test_youtube_album(client):
    body = post(client, "https://music.youtube.com/browse/MPREb_7ltM34kr0mH").json()
    assert body["collection"] == {"kind": "album", "platform": "youtube", "title": "Discovery"}
    assert body["truncated"] is False
    assert body["tracks"][0]["source"] == {
        "platform": "youtube",
        "id": "FGBhQbmPwH8",
        "url": "https://music.youtube.com/watch?v=FGBhQbmPwH8",
    }
    assert body["tracks"][0]["durationMs"] == 321000


def test_youtube_playlist_truncated_by_track_count(client):
    body = post(client, "https://www.youtube.com/playlist?list=PLx").json()
    assert len(body["tracks"]) == 3  # fixture is trimmed; YouTube says 495
    assert body["truncated"] is True


def test_youtube_collection_drops_unplayable_entries():
    raw = json.loads(fixture("ytmusic_get_album.json"))
    raw["tracks"][1]["isAvailable"] = False
    raw["tracks"][2]["videoId"] = None
    assert [t.video_id for t in parse_collection(raw).tracks] == ["FGBhQbmPwH8"]


def test_youtube_collection_not_found(client):
    r = post(client, "https://www.youtube.com/playlist?list=PLmissing")
    assert (r.status_code, r.json()) == (404, {"error": "not_found"})


def test_youtube_collection_upstream_error(client, monkeypatch):
    async def broken(id_, limit):
        raise RuntimeError("ytmusicapi broke")

    monkeypatch.setattr(ytmusic, "get_collection", broken)
    r = post(client, "https://www.youtube.com/playlist?list=PLx")
    assert (r.status_code, r.json()) == (502, {"error": "upstream_error", "source": "youtube"})


@pytest.mark.parametrize(
    "raw",
    [
        "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV",
        "https://youtu.be/FGBhQbmPwH8",
        "https://music.youtube.com/playlist?list=RDCLAK5uy_abc",
        "not a link",
    ],
)
def test_non_collections_are_unsupported(client, raw):
    r = post(client, raw)
    assert (r.status_code, r.json()) == (422, {"error": "unsupported_input"})


@pytest.mark.parametrize(
    "link",
    [
        f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
        f"https://open.spotify.com/album/{ALBUM_ID}",
        "https://music.youtube.com/browse/MPREb_7ltM34kr0mH",
        "https://www.youtube.com/playlist?list=PLx",
    ],
)
def test_every_source_url_is_resolvable(client, link):
    for track in post(client, link).json()["tracks"]:
        parsed = parse_input(track["source"]["url"])
        assert parsed is not None
        assert parsed.kind == "track"
        assert parsed.id == track["source"]["id"]
