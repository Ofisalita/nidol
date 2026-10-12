"""YouTube oEmbed (no key): title + channel only. Fallback when ytmusicapi can't describe a video."""

from dataclasses import dataclass

import httpx

OEMBED_URL = "https://www.youtube.com/oembed"


@dataclass(frozen=True)
class OEmbed:
    title: str
    author: str


async def oembed(client: httpx.AsyncClient, video_id: str) -> OEmbed | None:
    """None when YouTube says the video doesn't exist or can't be embedded."""
    r = await client.get(
        OEMBED_URL,
        params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
    )
    if r.status_code in (400, 401, 403, 404):
        return None
    r.raise_for_status()
    body = r.json()
    return OEmbed(title=body.get("title") or "", author=body.get("author_name") or "")
