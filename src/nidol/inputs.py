import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["track", "album", "playlist"]

_SPOTIFY_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[A-Za-z]{2})?/)?(?:embed/)?"
    r"(track|album|playlist)/([A-Za-z0-9]{22})"
)
_SPOTIFY_URI = re.compile(r"^spotify:(track|album|playlist):([A-Za-z0-9]{22})$")


@dataclass(frozen=True)
class ParsedInput:
    platform: str
    kind: Kind
    id: str


def parse_input(raw: str) -> ParsedInput | None:
    """Identify platform + kind + ID. Returns None for anything not supported yet."""
    raw = raw.strip()
    m = _SPOTIFY_URL.match(raw) or _SPOTIFY_URI.match(raw)
    if m:
        return ParsedInput("spotify", m.group(1), m.group(2))  # type: ignore[arg-type]
    return None
