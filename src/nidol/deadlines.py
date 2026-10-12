"""Deadline helpers (D11)."""

import asyncio
import logging
from collections.abc import Awaitable

from nidol.config import settings

log = logging.getLogger(__name__)


async def optional[T](
    name: str, coro: Awaitable[T], timeout: float = settings.source_timeout_s
) -> T | None:
    """Enrichment source (D11): a miss, error or timeout just means less data."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception as e:  # noqa: BLE001 — enrichment must never fail the request
        log.warning("%s skipped: %r", name, e)
        return None
