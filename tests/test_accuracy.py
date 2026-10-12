import pytest

from nidol import accuracy
from nidol.accuracy import (
    Case,
    auto_tags,
    load_cases,
    parse_pick,
    report,
    run_cases,
    save_cases,
)
from nidol.models import Candidate, Canonical, NidolError, ResolveResponse

SPOTIFY = "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"
DEEZER = "https://www.deezer.com/track/3135553"


def response(*video_ids, confirm=False, **canonical):
    canonical = {"title": "One More Time", "artists": ["Daft Punk"]} | canonical
    return ResolveResponse(
        canonical=Canonical(**canonical),
        candidates=[
            Candidate(videoId=v, title="t", kind="official_audio", thumbnailUrl="x", confidence=1.0)
            for v in video_ids
        ],
        needsConfirmation=confirm,
    )


def fake_resolver(answers):
    async def resolver(link):
        result = answers[link]
        if isinstance(result, NidolError):
            raise result
        return result

    return resolver


def test_cases_round_trip(tmp_path):
    path = tmp_path / "cases.yaml"
    cases = [
        Case(SPOTIFY, ["wU26xVT_vBU", "fa5IWHDbftI"], ["spotify"], "either Topic upload"),
        Case("https://youtu.be/夜", ["by4SYYWlhEs"]),
    ]
    save_cases(path, cases)
    assert path.read_text().startswith("# nidol accuracy test set")
    assert load_cases(path) == cases
    assert load_cases(tmp_path / "missing.yaml") == []


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("1", ["a"]),
        ("1,3", ["a", "c"]),
        (" 2 # the album version ", ["b"]),
        ("o FGBhQbmPwH8", ["FGBhQbmPwH8"]),
        ("o FGBhQbmPwH8,wU26xVT_vBU", ["FGBhQbmPwH8", "wU26xVT_vBU"]),
        ("s", "skip"),
        ("q", "quit"),
        ("4", None),
        ("0", None),
        ("x", None),
        ("o short", None),
        ("", None),
    ],
)
def test_parse_pick(answer, expected):
    assert parse_pick(answer, ["a", "b", "c"]) == expected


def test_auto_tags():
    assert auto_tags(SPOTIFY, Canonical(title="One More Time", artists=["Daft Punk"])) == [
        "spotify"
    ]
    remaster = Canonical(title="Jump - 2015 Remaster", artists=["Van Halen"])
    assert auto_tags(SPOTIFY, remaster) == ["spotify", "remaster"]
    live = Canonical(title="Hotel California (Live On MTV, 1994)", artists=["Eagles"])
    assert "live" in auto_tags(DEEZER, live)
    yoasobi = Canonical(title="夜に駆ける", artists=["YOASOBI"], explicit=True)
    assert auto_tags(DEEZER, yoasobi) == ["deezer", "non-latin", "explicit"]
    # Accented Latin letters are not "non-latin".
    rosalia = Canonical(title="DESPECHÁ", artists=["ROSALÍA"])
    assert "non-latin" not in auto_tags(DEEZER, rosalia)
    feat = Canonical(title="Rein Me In", artists=["Sam Fender", "Olivia Dean"])
    assert "feat" in auto_tags(SPOTIFY, feat)


async def test_run_and_report():
    cases = [
        Case("hit", ["a"], ["spotify"]),
        Case("second", ["b"], ["spotify", "live"]),  # top-3 only
        Case("miss", ["z"], ["deezer"], "the cover won"),
        Case("broken", ["a"], ["deezer"]),
    ]
    outcomes = await run_cases(
        cases,
        fake_resolver(
            {
                "hit": response("a", "b"),
                "second": response("a", "b", confirm=True),
                "miss": response("x", "y", "w"),
                "broken": NidolError(503, "search_unavailable"),
            }
        ),
        concurrency=2,
    )
    assert [(o.top1, o.top3) for o in outcomes] == [
        (True, True),
        (False, True),
        (False, False),
        (False, False),
    ]
    text = report(outcomes)
    assert "top-1  25.0%  (1/4, target 95%)" in text
    assert "top-3  50.0%  (2/4)" in text
    assert "needsConfirmation  50.0%   errors 1" in text
    assert "live               1    0.0%  100.0%" in text
    assert "got x, want z  # the cover won" in text
    assert "got search_unavailable, want a" in text


def test_report_empty():
    assert report([]) == "0 cases"


def test_label_flow(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cases.yaml"
    save_cases(path, [Case("already", ["a"])])
    inbox = tmp_path / "inbox.txt"
    inbox.write_text(f"# comment\n{SPOTIFY}  # Daft Punk\nalready\n\n{DEEZER}\nbroken\nlast\n")
    answers = {
        SPOTIFY: response("wU26xVT_vBU", "fa5IWHDbftI", "FGBhQbmPwH8"),
        DEEZER: response("a", "b"),
        "broken": NidolError(404, "not_found"),
        "last": response("c"),
    }

    async def close():
        pass

    monkeypatch.setattr(accuracy, "_live_resolver", lambda country: (fake_resolver(answers), close))
    typed = iter(["nope", "1,2 # both Topic uploads", "s", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(typed))
    monkeypatch.setattr("sys.argv", ["label", "--inbox", str(inbox), "--cases", str(path)])
    accuracy.label_main()

    cases = load_cases(path)
    assert cases == [
        Case("already", ["a"]),
        Case(SPOTIFY, ["wU26xVT_vBU", "fa5IWHDbftI"], ["spotify"], "both Topic uploads"),
    ]
    out = capsys.readouterr().out
    assert "4 to label (1 already" in out
    assert "error: not_found (skipped)" in out


def test_accuracy_main_exit_code(tmp_path, monkeypatch):
    path = tmp_path / "cases.yaml"
    save_cases(path, [Case("hit", ["a"]), Case("miss", ["z"])])

    async def close():
        pass

    answers = {"hit": response("a"), "miss": response("x")}
    monkeypatch.setattr(accuracy, "_live_resolver", lambda country: (fake_resolver(answers), close))
    monkeypatch.setattr("sys.argv", ["accuracy", "--cases", str(path)])
    with pytest.raises(SystemExit) as exit_:
        accuracy.accuracy_main()
    assert exit_.value.code == 1  # 50% < 95%

    monkeypatch.setattr("sys.argv", ["accuracy", "--cases", str(path), "--min-top1", "0.5"])
    accuracy.accuracy_main()  # no exit
