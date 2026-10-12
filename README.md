# nidol

The matching service for [bye0](https://github.com/Ofisalita/bye0): turns a music link into a canonical track
plus ranked, playable YouTube candidates. Design and decisions: [`MATCHING_SERVICE.md`](MATCHING_SERVICE.md).

**Status: walking skeleton (D16).** Resolves **Spotify and Deezer track links** and **YouTube / YT Music video links**
(`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/`, `/v/`). Every other input returns
`422 unsupported_input`, and album/playlist links return `409 collection_input`. **`POST /expand`** turns
Spotify albums/playlists and YT Music / YouTube albums/playlists into up to 100 track stubs, and
**`GET /search`** lists songs from YT Music and Deezer for the guest search box.

`/resolve` results are cached in SQLite (D3): a repeated link answers in about 2 ms.

A YouTube link comes back as `candidates[0]` with confidence 1 and `needsConfirmation: false`, and
`canonical.durationMs` is the video's real length (from ytmusicapi's `get_song`). Live streams have
`durationMs: null` and `canonical.isLive: true`. If `get_song` fails, nidol falls back to oEmbed and
returns `durationMs: null`. The other candidates are official uploads of the same recording (same artist,
within 10 s), for the player to fall through to on embed errors; `preferAudio: true` moves the official
audio first (D7).

## Run

```sh
uv sync
uv run nidol                    # http://localhost:8000, OpenAPI at /docs and /openapi.json
```

```sh
curl -X POST localhost:8000/resolve -H 'content-type: application/json' \
  -d '{"input":"https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV","country":"CL"}'
```

Docker:

```sh
docker build -t nidol . && docker run --rm -p 8000:8000 -v nidol-data:/data nidol
```

The cache lives at `/data/nidol.sqlite3` in the image. Mount a persistent volume at `/data` (in Coolify:
a persistent storage on `/data`), or the cache starts empty after every redeploy.

## Cache (D3)

- **Canonical track data** (title, artists, duration, ISRC, MBID) is kept forever, per link
  (`spotify:<id>`, `deezer:<id>`, `youtube:<id>`).
- **Candidates** expire after 7 days, per link and country. Spotify/Deezer candidates are also stored
  under the track's ISRC, so a Spotify link and a Deezer link to the same recording share them (a
  Spotify link learns its ISRC from Deezer while the YouTube search runs, and skips the search on a hit).
- **ISRC / MBID that missed the request deadline are filled in afterwards** by a background task, once
  per link per process. The MBID comes from MusicBrainz's link lookup, or else from its ISRC lookup.
- Not cached: errors, live streams, oEmbed fallbacks (no duration), and YouTube links whose
  alternates search failed. `/expand` and `/search` aren't cached.

## API (implemented so far)

| Endpoint | Notes |
|---|---|
| `POST /resolve` | Body `{input, target?: "youtube", country?: "CL", preferAudio?: false}` → `{canonical, candidates[], needsConfirmation}` |
| `POST /expand` | Body `{input}` (album/playlist link) → `{collection: {kind, platform, title}, tracks: [{title, artists, durationMs, explicit, source: {platform, id, url}}], truncated}`. Each `source.url` is a track link `/resolve` accepts. Track links and radio mixes are `422`. |
| `GET /search` | `?q=…&country=CL` → `{results: [{title, artists, durationMs, artworkUrl, isrc, source: {platform, id, url}}]}`, up to 10. YT Music songs lead (two per Deezer result); a song on both keeps the YT Music entry with Deezer's ISRC; low-popularity Deezer results (mostly covers and remixes) go last. `source.url` is a track link `/resolve` accepts. `503 search_unavailable` only when both sources fail. |
| `GET /healthz` | `{"status":"ok"}` |

Errors are `{"error": "<code>", ...}`:

| Status | `error` | Meaning |
|---|---|---|
| 404 | `not_found` | The Spotify track/album/playlist, Deezer track or YouTube video/playlist doesn't exist (or is private) |
| 404 | `no_match` | YouTube search returned nothing usable |
| 409 | `collection_input` | `/resolve` got an album/playlist link (`kind` says which); call `/expand` |
| 422 | `unsupported_input` | Not a supported link yet |
| 502 | `upstream_error` | The Spotify embed page failed or changed shape (`source: "spotify"`), both ytmusicapi and oEmbed failed for a YouTube link (`source: "youtube"`), or the Deezer API failed (`source: "deezer"`) |
| 503 | `search_unavailable` | YouTube Music search failed or timed out (for `/search`: both sources did) |

## Config

Environment variables (see `src/nidol/config.py`):

| Variable | Default |
|---|---|
| `PORT` / `HOST` | `8000` / `0.0.0.0` |
| `NIDOL_CACHE_PATH` | `nidol.sqlite3` (`/data/nidol.sqlite3` in Docker; empty disables the cache) |
| `NIDOL_CANDIDATES_TTL_S` | `604800` (7 days) |
| `NIDOL_BACKFILL_WAIT_S` | `60` (how long a background lookup may queue for MusicBrainz) |
| `NIDOL_MB_BASE_URL` | `https://musicbrainz.org/ws/2` |
| `NIDOL_MB_MIN_INTERVAL_S` | `1.0` |
| `NIDOL_SOURCE_TIMEOUT_S` | `0.8` (MusicBrainz, Deezer: skipped when late) |
| `NIDOL_REQUIRED_TIMEOUT_S` | `1.5` (Spotify embed, YouTube search) |
| `NIDOL_MAX_CANDIDATES` | `5` |
| `NIDOL_CONFIRM_THRESHOLD` | `0.75` |

## Develop

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Tests use recorded fixtures and never call upstream services.

## License

[WTFPL](LICENSE)
