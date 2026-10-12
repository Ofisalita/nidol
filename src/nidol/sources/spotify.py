"""Spotify metadata from the public embed page (no API key).

Unofficial: the page's __NEXT_DATA__ JSON is not a stable contract, so the parser is covered by
recorded fixtures (tests/fixtures/spotify_embed_*.html).
"""

import json
import re
from dataclasses import dataclass

import httpx

EMBED_URL = "https://open.spotify.com/embed/{kind}/{id}"
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
# The embed page is served to browsers; a bare client UA is not guaranteed to get the same markup.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; nidol/0.1; +https://github.com/Ofisalita/nidol)"
}


class SpotifyNotFound(Exception):
    pass


class SpotifyParseError(Exception):
    pass


@dataclass(frozen=True)
class SpotifyTrack:
    id: str
    title: str
    artists: list[str]
    duration_ms: int | None
    year: int | None
    explicit: bool | None
    artwork_url: str | None


def parse_track_embed(html: str) -> SpotifyTrack:
    m = _NEXT_DATA.search(html)
    if not m:
        raise SpotifyParseError("no __NEXT_DATA__ on embed page")
    try:
        page = json.loads(m.group(1))["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError) as e:
        raise SpotifyParseError(str(e)) from e
    if page.get("status") == 404:
        raise SpotifyNotFound()
    try:
        entity = page["state"]["data"]["entity"]
        release = (entity.get("releaseDate") or {}).get("isoString")
        images = (entity.get("visualIdentity") or {}).get("image") or []
        biggest = max(images, key=lambda i: i.get("maxWidth", 0), default=None)
        return SpotifyTrack(
            id=entity["id"],
            title=entity["title"],
            artists=[a["name"] for a in entity.get("artists", [])],
            duration_ms=entity.get("duration") or None,
            year=int(release[:4]) if release else None,
            explicit=entity.get("isExplicit"),
            artwork_url=biggest["url"] if biggest else None,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise SpotifyParseError(str(e)) from e


async def fetch_track(client: httpx.AsyncClient, track_id: str) -> SpotifyTrack:
    r = await client.get(EMBED_URL.format(kind="track", id=track_id), headers=_HEADERS)
    if r.status_code == 404:
        raise SpotifyNotFound()
    r.raise_for_status()
    return parse_track_embed(r.text)
