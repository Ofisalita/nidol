# nidol

The matching service for [bye0](https://github.com/Ofisalita/bye0): turns a music link into a canonical track
plus ranked, playable YouTube candidates. Design and decisions: [`MATCHING_SERVICE.md`](MATCHING_SERVICE.md).

**Status: walking skeleton (D16).** Resolves **Spotify track links** and **YouTube / YT Music video links**
(`watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/`, `/v/`). Every other input returns
`422 unsupported_input`, and album/playlist links return `409 collection_input`. No cache yet.

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
docker build -t nidol . && docker run --rm -p 8000:8000 nidol
```

## API (implemented so far)

| Endpoint | Notes |
|---|---|
| `POST /resolve` | Body `{input, target?: "youtube", country?: "CL", preferAudio?: false}` → `{canonical, candidates[], needsConfirmation}` |
| `GET /healthz` | `{"status":"ok"}` |

Errors are `{"error": "<code>", ...}`:

| Status | `error` | Meaning |
|---|---|---|
| 404 | `not_found` | The Spotify track or YouTube video doesn't exist |
| 404 | `no_match` | YouTube search returned nothing usable |
| 409 | `collection_input` | Album/playlist link (`kind` says which); `/expand` comes later |
| 422 | `unsupported_input` | Not a supported link yet |
| 502 | `upstream_error` | The Spotify embed page failed or changed shape (`source: "spotify"`), or both ytmusicapi and oEmbed failed for a YouTube link (`source: "youtube"`) |
| 503 | `search_unavailable` | YouTube Music search failed or timed out |

## Config

Environment variables (see `src/nidol/config.py`):

| Variable | Default |
|---|---|
| `PORT` / `HOST` | `8000` / `0.0.0.0` |
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
