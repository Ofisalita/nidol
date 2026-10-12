import httpx

from nidol.config import settings


def make_client() -> httpx.AsyncClient:
    """The shared upstream client: MusicBrainz requires the User-Agent (D1)."""
    return httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.required_timeout_s,
        follow_redirects=True,
    )
