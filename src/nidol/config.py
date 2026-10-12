from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIDOL_")

    # MusicBrainz (D1): public API by default, a mirror is just a different base URL.
    mb_base_url: str = "https://musicbrainz.org/ws/2"
    mb_min_interval_s: float = 1.0
    user_agent: str = "nidol/0.1.0 ( https://github.com/Ofisalita/nidol )"

    # Deadlines (D11). Enrichment sources (MB, Deezer) are dropped when they miss theirs;
    # the Spotify embed and the YouTube search are required.
    source_timeout_s: float = 0.8
    required_timeout_s: float = 1.5

    # Cache (D3). Empty disables it.
    cache_path: str = "nidol.sqlite3"
    candidates_ttl_s: float = 7 * 86400
    # How long a background ISRC/MBID backfill may queue for its MusicBrainz slot (D11).
    backfill_wait_s: float = 60.0

    max_candidates: int = 5
    max_expand_tracks: int = 100  # D9
    max_search_results: int = 10
    confirm_threshold: float = 0.75


settings = Settings()
