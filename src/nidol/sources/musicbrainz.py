"""MusicBrainz web service (D1): public API behind a process-wide 1 req/s limiter."""

import asyncio
import time

import httpx

from nidol.config import settings


class RateLimiter:
    """Spaces calls at least `interval` apart. `acquire` gives up (returns False) rather than
    queueing past `max_wait`, so a busy limiter can't blow the request deadline."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, max_wait: float) -> bool:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_slot - now)
            if wait > max_wait:
                return False
            self._next_slot = max(now, self._next_slot) + self.interval
        await asyncio.sleep(wait)
        return True


limiter = RateLimiter(settings.mb_min_interval_s)


async def recording_for_url(client: httpx.AsyncClient, url: str, max_wait: float) -> str | None:
    """MBID of the recording linked to a platform URL, if MusicBrainz has the relation."""
    if not await limiter.acquire(max_wait):
        return None
    r = await client.get(
        f"{settings.mb_base_url}/url",
        params={"resource": url, "inc": "recording-rels", "fmt": "json"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    for rel in r.json().get("relations", []):
        if rel.get("target-type") == "recording" and rel.get("recording"):
            return rel["recording"]["id"]
    return None
