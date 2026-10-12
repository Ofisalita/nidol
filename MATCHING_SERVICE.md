# nidol — bye0 matching service

> The matching service for [bye0](https://github.com/Ofisalita/bye0). Context: see bye0's
> [`SPEC.md`](https://github.com/Ofisalita/bye0/blob/main/SPEC.md).
> Written 2026-10-11; decisions (§ Decisions) settled through a Q&A session the same day.
> Everything marked ✅ was tested live that day; **(verify)** means it wasn't.

## Job

Turn **anything a guest gives us** (a link from any platform, or a free-text query) into:
1. a **canonical track** (ISRC, MusicBrainz recording ID, title, artists, duration, release year, explicit flag), and
2. a **ranked list of playable candidates on the target platform**. In v1 the target is always **YouTube video IDs**.
   A Spotify URI target comes later, for the power host.

It also expands album/playlist links into track lists and powers the guest search box.

It's a separate HTTP service (Python, because ytmusicapi is Python) that **only the bye0 room server calls**.

## Why it's its own project

- **Odesli / song.link is dead for us.** `api.song.link` now returns
  `HTTP 401 {"code":"PUBLIC_API_ACCESS_DEPRECATED"}` ✅. Linktree retired the keyless API on 2026-07-31 and has
  stopped issuing keys ([AubeSonore PR](https://github.com/VictorNain26/AubeSonore/pull/221),
  [music-bot issue](https://github.com/eeegoloauq/music-bot/issues/7)).
- **No Spotify Web API key** (team decision; see [SPEC §2](https://github.com/Ofisalita/bye0/blob/main/SPEC.md#2-scope-decisions)).
- Matching has to be accurate (≥95% on a test set) and fast enough for a 10 s QTE prompt.

## Building blocks

| Source | Gives us | Auth | Status |
|---|---|---|---|
| **Spotify embed page** `open.spotify.com/embed/{track,album,playlist}/<id>` | `__NEXT_DATA__` JSON. Track: title, **artists**, **duration**, release date, explicit. Album/playlist: name + `trackList` (title, artist line, duration, explicit, track URI). | none | ✅ track `0DiWol3AO6WpXZgp0goxAV` → Daft Punk, 320357 ms, 2001-03-12; album *Discovery* → 14/14 tracks; *Today's Top Hits* → 50; *All Out 80s* → **100 of 150**: the embed caps at 100 (✅ 2026-10-12, the full page's `music:song_count` meta says 150). Unofficial scrape, ToS-gray, can break; parser covered by recorded fixtures. |
| **MusicBrainz** `/ws/2/url?resource=<platform URL>&inc=recording-rels` | Platform URL → MB recording | none, `User-Agent` required, ~1 req/s on the public server | ✅ Spotify track URL → recording `60fa767a-…` ("One More Time") |
| **MusicBrainz** `/ws/2/isrc/<ISRC>?inc=url-rels` | ISRC → recording + **links to Spotify/Deezer/Apple/Tidal** | same | ✅ returned 4 platform URLs for GBDUW0000053. Links are crowd-sourced, so obscure tracks often have none. |
| **Deezer** `api.deezer.com/search?q=` | Search with **ISRC**, duration, rank, `explicit_lyrics` | none | ✅ |
| **Deezer** `api.deezer.com/track/isrc:<ISRC>` | ISRC → track, release date, explicit | none | ✅ |
| **iTunes Search API** `itunes.apple.com/search?entity=song` | Apple catalog search + IDs | none | ✅ worked. Rate limit is reportedly ~20/min **(verify)** |
| **Spotify oEmbed** `open.spotify.com/oembed?url=` | Title + artwork only | none | ✅. Superseded by the embed page; keep only for artwork if needed. |
| **YouTube oEmbed** `youtube.com/oembed?url=` | Video title + channel | none | ✅ |
| **ytmusicapi** (unofficial) | YT Music search with `filter="songs"` → official-audio videoIds, durations; `location=<CC>` localizes results; metadata for YT Music links | none (no login needed for search) | Not tested here. Unofficial, can break, against YouTube ToS. |
| **YouTube Data API v3** | Fallback search; `videos.list` `contentDetails.regionRestriction` for region checks | API key | 10k units/day default; search ~100 units, `videos.list` 1 unit per batch of ≤50 IDs **(verify in the Cloud console)**. **No GCP project yet**, so the key is optional (see D6). |

## Decisions

| # | Topic | Decision |
|---|---|---|
| D1 | MusicBrainz | **Public API** (`musicbrainz.org/ws/2`) behind a **global 1 req/s limiter** and the cache. No self-hosted mirror. Base URL and rate are config, so a mirror stays a one-env-var change. `User-Agent: nidol/<version> ( https://github.com/Ofisalita/nidol )`. |
| D2 | Framework / tooling | **FastAPI** + Pydantic, **httpx** (async) for every upstream. **uv**, **ruff** (lint + format), **pytest** + **respx** (recorded upstream fixtures). **Python 3.14**. |
| D3 | Cache | **SQLite** file on a Docker volume (WAL mode). URL → canonical and ISRC → canonical are kept **forever**; candidate lists expire after **7 days**; `/feedback` overrides win over both. Independent of bye0's Redis. |
| D4 | Who calls nidol | **Only the bye0 room server**, over Coolify's internal network. No public port, no auth, no CORS. Per-guest rate limiting lives in the room server. |
| D5 | Region blocking | bye0 sends the party's **`country`** (ISO 3166-1 alpha-2). nidol searches with ytmusicapi `location=<country>` and, **when a Data API key is configured**, drops candidates whose `regionRestriction` blocks that country. The player's fall-through (SPEC §3) handles anything that slips past. |
| D6 | ytmusicapi down | **Automatic fallback to Data API search** under a configurable daily unit budget (`YT_DATA_API_DAILY_UNITS`). Cache hits cost nothing. When the budget is spent, or no key is configured, return `503 {"error":"search_unavailable"}` so bye0 can ask for a YouTube link. **No GCP project exists yet**: the key is optional, and without it there are no region checks and no fallback. |
| D7 | Music-video links | Driven by a **bye0 host setting**, passed as `preferAudio` (default `false`). `false` = play what the guest linked, with the official audio as the next candidate. `true` = official audio first, with the linked video as the next candidate. Only applies to YouTube / YT Music inputs. |
| D8 | Guest search box | **Songs, then resolve.** `/search` returns canonical-looking song results (Deezer + YT Music songs, deduped), not videos. Picking one calls `/resolve` with its `source.url`. |
| D9 | Albums / playlists | **Expanded, host-gated in bye0.** `/resolve` on a collection URL returns `409 {"error":"collection_input"}`; bye0 calls `/expand` if the host allows it. Works for Spotify (embed-page scrape), Deezer, YT Music and Apple. **Lazy:** `/expand` returns lightweight stubs in about 1 s, and bye0 calls `/resolve` per stub only as it nears the front of the queue. Expansion items are **auto-picked** (no QTE prompt storm). **Capped at 100 tracks** on every platform, with `truncated: true` when it applies. |
| D10 | Spotify single tracks | **Embed page first** (artists/title/duration in one request); YT candidate search starts immediately. In parallel: MB URL lookup (ISRC + MBID when crowd-linked) and Deezer search by artist + title filtered on duration (otherwise ISRC). |
| D11 | Latency | **Per-source deadlines** (~800 ms each, concurrent) and a ~1.8 s overall deadline for `/resolve`. Return what we have by then; missing ISRC/MBID is backfilled into the cache afterwards. Lower confidence simply triggers the QTE. |
| D12 | Explicit / clean | `explicit` is reported on the canonical. **No clean-version preference in v1.** |
| D13 | Feedback | **`POST /feedback`** from bye0 on QTE picks and on player embed errors. Picks become cache overrides (input / ISRC → videoId); embed-blocked videoIds (101/150/100) get skipped from then on. The log also seeds new test-set cases. No guest or party identifiers are stored. |
| D14 | Accuracy test set | `tests/accuracy/cases.yaml` (input, accepted videoIds, tags like `remaster`/`live`/`non-latin`, notes). A **label CLI** (`uv run label <link>`) resolves the link, shows the candidates and appends your pick. Both of you add cases. |
| D15 | Accuracy runs | **Manual only** (`uv run accuracy`), before deploys and after ytmusicapi upgrades. PR CI runs unit tests against recorded fixtures only. |
| D16 | Build order | **Walking skeleton first:** `/resolve` Spotify → YouTube end to end, deployed on Coolify so bye0 can integrate. Then the test set + label CLI, then the remaining platforms + scoring tuning to ≥95%, then `/search`, `/expand`, `/feedback` and the Data API fallback. **Changed 2026-10-12:** `/expand` and `/search` move ahead of the test set so bye0 can build against them in parallel. |
| D17 | Contract sharing | FastAPI's OpenAPI schema is the contract; bye0 generates TS types from it. |
| D18 | Odd inputs | Free text in `/resolve` is auto-picked (top search result; `needsConfirmation` applies as usual). Any YouTube link, music or not, is accepted as-is (canonical from oEmbed). |
| D19 | License | **WTFPL**, same as bye0 (ytmusicapi is MIT, compatible). |

## Pipeline

```
input ─┬─ URL ──► identify platform + ID + kind (track | album | playlist)
       │           ├─ collection → 409 collection_input (bye0 calls /expand)
       │           ├─ YouTube / YT Music URL → already playable; enrich via ytmusicapi/oEmbed (+MB if possible)
       │           │                            preferAudio decides linked video vs official audio first (D7)
       │           ├─ Spotify URL → embed page (artists, title, duration, explicit)
       │           │                 ├─ ‖ MB url lookup → recording → ISRC/MBID
       │           │                 └─ ‖ Deezer search artist+title, duration-filtered → ISRC
       │           ├─ Deezer URL → Deezer API → ISRC
       │           ├─ Apple Music URL → iTunes lookup → (ISRC via MB or Deezer by title+artist+duration)
       │           └─ Tidal / other → MB url lookup → … / text search
       └─ text ──► Deezer search (gives ISRC) + ytmusicapi search → auto-pick top
                         │
canonical {isrc?, mbid?, title, artists[], durationMs, year, explicit}
                         │
target=youtube ──► (cache / feedback override hit? → done)
                ──► ytmusicapi search "artist title" (filter=songs, then videos), location=country
                      └─ unavailable → Data API search under daily budget (D6)
                ──► drop known embed-blocked IDs; drop region-blocked IDs if a key is set (D5)
                ──► score each candidate:
                      duration delta (±3 s strong, ±10 s weak) · title/artist fuzzy match ·
                      "official audio"/Topic channel bonus · penalties for live, remix, cover,
                      karaoke, sped up, nightcore, slowed (unless the source title has them too)
                ──► return the top N (N≥3, so the player can fall through embed-disabled videos)
```

## API contract (v1)

```http
POST /resolve
{ "input": "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV", "target": "youtube",
  "country": "CL", "preferAudio": false }

200 OK
{
  "canonical": { "isrc": "GBDUW0000053", "mbid": "60fa767a-…", "title": "One More Time",
                 "artists": ["Daft Punk"], "durationMs": 320357, "year": 2001, "explicit": false,
                 "artworkUrl": "…", "isLive": false },   // isLive: YouTube live stream, durationMs null
  "candidates": [
    { "videoId": "…", "title": "…", "channel": "Daft Punk - Topic", "durationMs": 320000,
      "kind": "official_audio", "thumbnailUrl": "…", "confidence": 0.97 },
    …
  ],
  "needsConfirmation": false        // true when top confidence < threshold or top-2 are too close
}
409 { "error": "collection_input", "kind": "album" | "playlist" }
422 { "error": "unsupported_input" }
503 { "error": "search_unavailable" }

POST /expand
{ "input": "https://open.spotify.com/playlist/…" }
200 OK
{ "collection": { "kind": "playlist", "platform": "spotify", "title": "…" },
  "tracks": [ { "title": "…", "artists": ["…"], "durationMs": 198840, "explicit": false,
                "source": { "platform": "spotify", "id": "…", "url": "…" } }, … ],
  "truncated": false }

// tracks[].artists: for Spotify, one display string ("A, B") — the embed has no per-artist list,
//   and splitting on commas breaks names like "Tyler, The Creator". /resolve returns the real list.
// truncated: true when the list had more than 100 tracks. Spotify embeds stop at 100 without a
//   count; nidol then reads the playlist page's song count, and assumes true if it can't.
// YouTube radio/mix lists (list=RD…) are 422 unsupported_input: they're endless and generated.

GET  /search?q=...&country=CL
200 OK
{ "results": [ { "title": "…", "artists": ["…"], "durationMs": …, "artworkUrl": "…", "isrc": "…",
                 "source": { "platform": "deezer" | "ytmusic", "id": "…", "url": "…" } }, … ] }

POST /feedback
{ "input": "…", "isrc": "…", "videoId": "…", "event": "picked" | "embed_error", "errorCode": 150 }
204 No Content

GET  /healthz                       // includes upstream status + Data API units left today
```

- `needsConfirmation` triggers bye0's 10 s QTE (the adder picks, the host can override, otherwise auto-pick the
  top candidate). Never shown for `/expand` items (D9). Tune the threshold on the test set.
- Latency budget: **p95 < 2 s** for `/resolve` (D11).
- `canonical.isrc`/`mbid` will later power dedup guards, decade flow, and song trivia.

## Accuracy test set (v1 "done" criterion)

- ≥200 real links pasted by the two of you: Spotify + YT Music mostly, plus some Apple/Deezer.
- Deliberately include hard cases: remasters, live vs studio, clean vs explicit, songs with covers far more popular
  than the original, non-Latin-script titles, Spanish-language releases, regional exclusives.
- Label the "correct" YouTube video(s) with the label CLI (D14); the metric is **top-1 accuracy ≥95%**, and also
  report top-3.
- Run it by hand (D15). ytmusicapi breakage will show up as a sudden accuracy drop.

## What bye0 needs from this

- A host setting **"prefer audio versions"** → `preferAudio` (D7).
- A host setting **"allow albums/playlists"**, plus lazy per-stub `/resolve` calls as items near the front (D9).
- Send the party's `country` on `/resolve` and `/search` (D5).
- Report QTE picks and IFrame `onError` codes to `/feedback` (D13).
- Handle `search_unavailable` by asking for a YouTube link (D6).

## Open items

- **GCP project for the Data API key**: none exists yet. Until there is one, no region checks and no fallback (D6).
- Verify the iTunes rate limit and the Data API unit costs.
