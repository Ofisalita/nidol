import json
from pathlib import Path

import pytest

from nidol.inputs import ParsedInput, parse_input
from nidol.models import Canonical
from nidol.scoring import needs_confirmation, rank
from nidol.sources.musicbrainz import RateLimiter
from nidol.sources.spotify import SpotifyNotFound, parse_track_embed
from nidol.sources.ytmusic import VideoUnavailable, YTResult, parse_song
from nidol.text import core_title, normalize

FIXTURES = Path(__file__).parent / "fixtures"
TRACK_ID = "0DiWol3AO6WpXZgp0goxAV"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"https://open.spotify.com/track/{TRACK_ID}", ("track", TRACK_ID)),
        (f"https://open.spotify.com/track/{TRACK_ID}?si=abc123", ("track", TRACK_ID)),
        (f"https://open.spotify.com/intl-es/track/{TRACK_ID}", ("track", TRACK_ID)),
        (f"https://open.spotify.com/embed/album/{TRACK_ID}", ("album", TRACK_ID)),
        (f"spotify:playlist:{TRACK_ID}", ("playlist", TRACK_ID)),
        (f"  https://open.spotify.com/track/{TRACK_ID}\n", ("track", TRACK_ID)),
    ],
)
def test_parse_spotify(raw, expected):
    assert parse_input(raw) == ParsedInput("spotify", *expected)


@pytest.mark.parametrize(
    "raw",
    [
        "daft punk one more time",
        "https://open.spotify.com/track/x",
        "https://youtube.com/watch?v=tooshort",
        "https://example.com/watch?v=FGBhQbmPwH8",
        "youtube.com/watch?v=FGBhQbmPwH8",  # no scheme, like bye0's `new URL()`
        "https://www.youtube.com/",
    ],
)
def test_parse_unsupported(raw):
    assert parse_input(raw) is None


VID = "FGBhQbmPwH8"


@pytest.mark.parametrize(
    "raw",
    [
        f"https://www.youtube.com/watch?v={VID}&t=42",
        f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}?si=abc",
        f"https://youtube.com/shorts/{VID}",
        f"https://www.youtube-nocookie.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}",
        f"https://www.youtube.com/v/{VID}",
    ],
)
def test_parse_youtube_video(raw):
    assert parse_input(raw) == ParsedInput("youtube", "track", VID)


def test_parse_youtube_playlist():
    assert parse_input("https://music.youtube.com/playlist?list=PLx") == ParsedInput(
        "youtube", "playlist", "PLx"
    )


def load_song(name):
    return json.loads((FIXTURES / f"ytmusic_get_song_{name}.json").read_text())


def test_parse_song_official_video():
    video = parse_song(VID, load_song("omv"))
    assert (video.title, video.author, video.duration_ms) == ("One More Time", "Daft Punk", 321000)
    assert (video.kind, video.is_live) == ("official_video", False)


def test_parse_song_ten_hours():
    assert parse_song("B4LvDiIi128", load_song("ten_hours")).duration_ms == 36_000_000


def test_parse_song_live_stream_has_no_duration():
    video = parse_song("rFZHOHl-L8A", load_song("live"))
    assert (video.duration_ms, video.is_live) == (None, True)


def test_parse_song_unavailable():
    with pytest.raises(VideoUnavailable, match="Video unavailable"):
        parse_song("aaaaaaaaaaa", load_song("unavailable"))


def test_parse_track_embed():
    track = parse_track_embed((FIXTURES / "spotify_embed_track.html").read_text())
    assert track.id == TRACK_ID
    assert track.title == "One More Time"
    assert track.artists == ["Daft Punk"]
    assert track.duration_ms == 320357
    assert track.year == 2001
    assert track.explicit is False
    assert track.artwork_url and "ab67616d0000b273" in track.artwork_url  # the 640px image


def test_parse_track_embed_404():
    with pytest.raises(SpotifyNotFound):
        parse_track_embed((FIXTURES / "spotify_embed_404.html").read_text())


def test_normalize_keeps_non_latin_and_strips_accents():
    assert normalize("Canción — Ñandú & Café!") == "cancion nandu and cafe"
    assert normalize("夜に駆ける") == "夜に駆ける"


@pytest.mark.parametrize(
    ("title", "core"),
    [
        ("One More Time (Radio Edit)", "one more time"),
        ("Here Comes the Sun - Remastered 2019", "here comes the sun"),
        ("Despacito (feat. Daddy Yankee)", "despacito"),
        ("Song feat. Someone", "song"),
        ('Holding Out for a Hero - From "Footloose" Soundtrack', "holding out for a hero"),
        ("Daft Punk - One More Time", "daft punk one more time"),  # not a version suffix
    ],
)
def test_core_title(title, core):
    assert core_title(title) == core


CANONICAL = Canonical(title="One More Time", artists=["Daft Punk"], durationMs=320357)
# Live ytmusicapi results for "Daft Punk One More Time" (country CL, 2026-10-11), slightly trimmed.
RESULTS = [
    YTResult("wU26xVT_vBU", "One More Time (Radio Edit)", ["Daft Punk"], 321000, "official_audio"),
    YTResult("Qs2HuZN8KDk", "One More Time", ["Amari Monroe"], 135000, "official_audio"),
    YTResult(
        "qmO5ZLjVVUU",
        "One More Time (Originally Performed by Daft Punk) [Lullaby Version]",
        ["Lullabye Baby Ensemble"],
        301000,
        "official_audio",
    ),
    YTResult("FGBhQbmPwH8", "One More Time", ["Daft Punk"], 322000, "official_video"),
    YTResult(
        "s2JXPPu7FAs", 'Daft Punk: "One More Time" [slowed]', ["sl9wed"], 323000, "user_upload"
    ),
]


def test_rank_prefers_official_and_buries_variants():
    ranked = rank(CANONICAL, RESULTS, limit=5)
    assert [c.videoId for c in ranked[:2]] == ["wU26xVT_vBU", "FGBhQbmPwH8"]
    by_id = {c.videoId: c.confidence for c in ranked}
    assert by_id["qmO5ZLjVVUU"] < 0.5  # lullaby cover
    assert by_id["s2JXPPu7FAs"] < 0.5  # slowed
    assert by_id["Qs2HuZN8KDk"] < 0.6  # same title, different artist
    assert not needs_confirmation(ranked, threshold=0.75)


def test_variant_not_penalized_when_source_has_it():
    live = Canonical(title="One More Time (Live)", artists=["Daft Punk"], durationMs=400000)
    result = YTResult("x", "One More Time (Live)", ["Daft Punk"], 400000, "official_audio")
    assert rank(live, [result], limit=1)[0].confidence == 1.0


def test_stripped_version_ranks_below_original():
    source = Canonical(title="bloodstream", artists=["Alyssa Grace"], durationMs=177000)
    results = [
        YTResult("s", "bloodstream (stripped)", ["Alyssa Grace"], 176000, "official_audio"),
        YTResult("o", "bloodstream", ["Alyssa Grace"], 178000, "official_audio"),
    ]
    assert [c.videoId for c in rank(source, results, limit=2)] == ["o", "s"]


def test_needs_confirmation_when_weak_or_ambiguous():
    weak = rank(CANONICAL, RESULTS[1:2], limit=1)
    assert needs_confirmation(weak, threshold=0.75)
    assert needs_confirmation([], threshold=0.75)
    # Two near-equal candidates of clearly different length: probably different recordings.
    a = YTResult("a", "One More Time", ["Daft Punk"], 320000, "official_audio")
    b = YTResult("b", "One More Time", ["Daft Punk"], 335000, "official_audio")
    ambiguous = rank(Canonical(title="One More Time", artists=["Daft Punk"]), [a, b], limit=2)
    assert needs_confirmation(ambiguous, threshold=0.0)


async def test_rate_limiter_gives_up_instead_of_queueing():
    limiter = RateLimiter(interval=10)
    assert await limiter.acquire(max_wait=0.1)
    assert not await limiter.acquire(max_wait=0.1)
