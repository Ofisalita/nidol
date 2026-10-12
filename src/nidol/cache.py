"""SQLite lookup cache (D3).

- canonical: link key ("spotify:<id>", "deezer:<id>", "youtube:<id>") → Canonical, kept forever.
- candidates: (key, country) → ranked candidates, for `candidates_ttl_s` (videos get removed,
  search results drift). A track's candidates are also stored under "isrc:<ISRC>", so a Spotify
  and a Deezer link to the same recording share them.

Queries are sub-millisecond on local disk, so they run inline on the event loop.
`Cache(None)` is a no-op, for tests and the accuracy runner (which must measure live matching).
"""

import sqlite3
import time
from pathlib import Path

from pydantic import TypeAdapter

from nidol.models import Candidate, Canonical

_CANDIDATES = TypeAdapter(list[Candidate])

SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    key TEXT NOT NULL,
    country TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (key, country)
);
"""


class Cache:
    def __init__(self, path: str | None, candidates_ttl_s: float = 7 * 86400):
        self.ttl = candidates_ttl_s
        self._db: sqlite3.Connection | None = None
        if path:
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            # The app runs in one event loop, but TestClient drives it from another thread.
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    @property
    def enabled(self) -> bool:
        return self._db is not None

    def canonical(self, key: str) -> Canonical | None:
        if not self._db:
            return None
        row = self._db.execute("SELECT data FROM canonical WHERE key = ?", (key,)).fetchone()
        return Canonical.model_validate_json(row[0]) if row else None

    def put_canonical(self, key: str, canonical: Canonical) -> None:
        if self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO canonical VALUES (?, ?, ?)",
                (key, canonical.model_dump_json(), time.time()),
            )

    def candidates(self, keys: list[str], country: str | None) -> list[Candidate] | None:
        """The first fresh candidate list stored under any of `keys`."""
        if not self._db:
            return None
        fresh_after = time.time() - self.ttl
        for key in keys:
            row = self._db.execute(
                "SELECT data FROM candidates WHERE key = ? AND country = ? AND created_at > ?",
                (key, country or "", fresh_after),
            ).fetchone()
            if row:
                return _CANDIDATES.validate_json(row[0])
        return None

    def put_candidates(self, keys: list[str], country: str | None, cands: list[Candidate]) -> None:
        if self._db:
            data = _CANDIDATES.dump_json(cands).decode()
            now = time.time()
            self._db.executemany(
                "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?)",
                [(key, country or "", data, now) for key in keys],
            )

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None


NO_CACHE = Cache(None)
