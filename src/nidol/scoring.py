"""Candidate scoring. Weights and thresholds are first guesses, to be tuned on the accuracy set."""

from difflib import SequenceMatcher

from nidol.models import Candidate, Canonical
from nidol.sources.ytmusic import YTResult
from nidol.text import contains_phrase, core_title, normalize

# Markers of a different rendition. Penalized unless the source title has them too.
VARIANT_MARKERS = (
    "live", "remix", "cover", "karaoke", "instrumental", "acapella", "a cappella", "sped up",
    "slowed", "nightcore", "reverb", "8d", "1 hour", "loop", "extended", "mashup", "tutorial",
    "lesson", "reaction", "bass boosted", "lullaby", "tribute", "originally performed",
    "made famous", "in the style of", "bardcore", "8 bit", "music box", "stripped", "acoustic",
    "unplugged", "demo", "orchestral", "reversed",
)  # fmt: skip
VARIANT_PENALTY = 0.4

KIND_SCORE = {
    "official_audio": 1.0,
    "official_video": 0.8,
    "other": 0.4,
    "user_upload": 0.2,
}
WEIGHTS = {"title": 0.35, "artist": 0.25, "duration": 0.25, "kind": 0.15}

# Candidates this close in confidence but with different durations are likely different recordings.
CLOSE_MARGIN = 0.05
DIFFERENT_RECORDING_MS = 10_000


def _title_score(source: str, candidate: str) -> float:
    src_core = core_title(source)
    cand_core = core_title(candidate)
    if src_core == cand_core:
        return 1.0
    # UGC titles like "Daft Punk - One More Time (Official Video)" contain the song name.
    if contains_phrase(normalize(candidate), src_core):
        return 0.9
    return SequenceMatcher(None, src_core, cand_core).ratio()


# YouTube Music's artist field is authoritative for these; for uploads it's just the channel.
AUTHORITATIVE_ARTISTS = {"official_audio", "official_video"}


def same_artist(artists: list[str], result: YTResult) -> bool:
    return bool({normalize(a) for a in artists} & {normalize(a) for a in result.artists})


def _artist_score(artists: list[str], result: YTResult) -> float:
    wanted = {normalize(a) for a in artists}
    if same_artist(artists, result):
        return 1.0
    if result.kind in AUTHORITATIVE_ARTISTS:
        # "Song (Originally Performed by <artist>)" from someone else is a cover.
        return 0.0
    title = normalize(result.title)
    if any(contains_phrase(title, a) for a in wanted):
        return 0.8
    return 0.0


def _duration_score(source_ms: int | None, candidate_ms: int | None) -> float:
    if not source_ms or not candidate_ms:
        return 0.3
    delta_s = abs(source_ms - candidate_ms) / 1000
    if delta_s <= 3:
        return 1.0
    if delta_s <= 10:
        return 0.6
    if delta_s <= 30:
        return 0.6 * (30 - delta_s) / 20
    return 0.0


def _variant_factor(source_title: str, candidate_title: str) -> float:
    src, cand = normalize(source_title), normalize(candidate_title)
    factor = 1.0
    for marker in VARIANT_MARKERS:
        if contains_phrase(cand, marker) and not contains_phrase(src, marker):
            factor *= VARIANT_PENALTY
    return factor


def score(canonical: Canonical, result: YTResult) -> float:
    s = (
        WEIGHTS["title"] * _title_score(canonical.title, result.title)
        + WEIGHTS["artist"] * _artist_score(canonical.artists, result)
        + WEIGHTS["duration"] * _duration_score(canonical.durationMs, result.duration_ms)
        + WEIGHTS["kind"] * KIND_SCORE[result.kind]
    )
    return round(s * _variant_factor(canonical.title, result.title), 3)


def rank(canonical: Canonical, results: list[YTResult], limit: int) -> list[Candidate]:
    scored = sorted(((score(canonical, r), r) for r in results), key=lambda p: p[0], reverse=True)
    return [
        Candidate(
            videoId=r.video_id,
            title=r.title,
            channel=", ".join(r.artists) or None,
            durationMs=r.duration_ms,
            kind=r.kind,  # type: ignore[arg-type]
            thumbnailUrl=f"https://i.ytimg.com/vi/{r.video_id}/hqdefault.jpg",
            confidence=s,
        )
        for s, r in scored[:limit]
    ]


def needs_confirmation(candidates: list[Candidate], threshold: float) -> bool:
    if not candidates or candidates[0].confidence < threshold:
        return True
    if len(candidates) > 1:
        top, second = candidates[0], candidates[1]
        if (
            top.confidence - second.confidence < CLOSE_MARGIN
            and top.durationMs
            and second.durationMs
            and abs(top.durationMs - second.durationMs) > DIFFERENT_RECORDING_MS
        ):
            return True
    return False
