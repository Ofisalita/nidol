import re
import unicodedata

_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FEAT = re.compile(r"\b(?:feat|ft|featuring)\b.*$")
# " - Remastered 2011", " - Radio Edit", " - From "Footloose" Soundtrack": version suffixes, not part
# of the song title.
_VERSION_SUFFIX = re.compile(
    r"\s+-\s+.*\b(?:remaster(?:ed)?|version|edit|mix|live|mono|stereo|demo|single|from|soundtrack"
    r"|recorded|anniversary|bonus|acoustic)\b.*$"
)
_NON_WORD = re.compile(r"[^\w]+")


def normalize(s: str) -> str:
    """Lowercase, strip accents (any script's letters survive), punctuation to spaces."""
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ")
    return " ".join(_NON_WORD.sub(" ", s).replace("_", " ").split())


def core_title(title: str) -> str:
    """The song name without bracketed tags, featured artists or version suffixes."""
    t = _VERSION_SUFFIX.sub("", title.casefold())
    t = _BRACKETS.sub(" ", t)
    t = _FEAT.sub("", t)
    return normalize(t)


def contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word containment on normalized strings."""
    return bool(phrase) and f" {phrase} " in f" {text} "
