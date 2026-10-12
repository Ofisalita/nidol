import pytest

from nidol.config import settings


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    """App tests run without the SQLite cache (it has its own tests): each test starts cold."""
    monkeypatch.setattr(settings, "cache_path", "")
