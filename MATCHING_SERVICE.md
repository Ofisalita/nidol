# nidol — bye0 matching service

> The matching service for [bye0](https://github.com/Ofisalita/bye0). Context: see bye0's
> [`SPEC.md`](https://github.com/Ofisalita/bye0/blob/main/SPEC.md).
> Written 2026-10-11. Everything marked ✅ was tested live that day; **(verify)** means it wasn't.

## Job

Turn **anything a guest gives us** (a link from any platform, or a free-text query) into:
1. a **canonical track** (ISRC, MusicBrainz recording ID, title, artists, duration, release year, explicit flag), and
2. a **ranked list of playable candidates on the target platform**. In v1 the target is always **YouTube video IDs**.
   A Spotify URI target comes later, for the power host.

It's a separate HTTP service (Python, because ytmusicapi is Python) that the bye0 room server calls.

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
| **MusicBrainz** `/ws/2/url?resource=<platform URL>&inc=recording-rels` | Platform URL → MB recording | none, `User-Agent` required, ~1 req/s on the public server | ✅ Spotify track URL → recording `60fa767a-…` ("One More Time") |
| **MusicBrainz** `/ws/2/isrc/<ISRC>?inc=url-rels` | ISRC → recording + **links to Spotify/Deezer/Apple/Tidal** | same | ✅ returned 4 platform URLs for GBDUW0000053. Links are crowd-sourced, so obscure tracks often have none. Core data is CC0, so it **can be self-hosted** (musicbrainz-docker) to avoid the rate limit. |
| **Deezer** `api.deezer.com/search?q=` | Search with **ISRC**, duration, rank, `explicit_lyrics` | none | ✅ |
| **Deezer** `api.deezer.com/track/isrc:<ISRC>` | ISRC → track, release date, explicit | none | ✅ |
| **iTunes Search API** `itunes.apple.com/search?entity=song` | Apple catalog search + IDs | none | ✅ worked. Rate limit is reportedly ~20/min **(verify)** |
| **Spotify oEmbed** `open.spotify.com/oembed?url=` | **Title + artwork only** (no artist, no ISRC) | none | ✅. Use as a last-resort hint when MusicBrainz has no URL relation. |
| **YouTube oEmbed** `youtube.com/oembed?url=` | Video title + channel | none | ✅ |
| **ytmusicapi** (unofficial) | YT Music search with `filter="songs"` → official-audio videoIds, durations; can also look up metadata for YT Music links | none (no login needed for search) | Not tested here. Unofficial, can break, against YouTube ToS. |
| **YouTube Data API v3** | Official search fallback | API key | 10k units/day default; most sources say ~100 units per search, so about 100 searches/day **(verify in the Cloud console)** |

## Pipeline (first proposal)

```
input ─┬─ URL ──► identify platform + ID
       │           ├─ YouTube / YT Music URL → already playable; enrich via ytmusicapi/oEmbed (+MB if possible)
       │           ├─ Spotify URL → MB url lookup → recording → ISRC
       │           │                 └─ miss → oEmbed title → text search (below)
       │           ├─ Deezer URL → Deezer API → ISRC
       │           ├─ Apple Music URL → iTunes lookup → (ISRC via MB or Deezer by title+artist+duration)
       │           └─ Tidal / other → MB url lookup → … / text search
       └─ text ──► Deezer search (gives ISRC) + ytmusicapi search
                         │
canonical {isrc, mbid?, title, artists[], durationMs, year, explicit}
                         │
target=youtube ──► ytmusicapi search "artist title" (filter=songs, then videos)
                ──► score each candidate:
                      duration delta (±3 s strong, ±10 s weak) · title/artist fuzzy match ·
                      "official audio"/Topic channel bonus · penalties for live, remix, cover,
                      karaoke, sped up, nightcore, slowed (unless the source title has them too)
                ──► return the top N (N≥3, so the player can fall through embed-disabled videos)
```

## API contract (draft, for the bye0 room server)

```http
POST /resolve
{ "input": "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV", "target": "youtube", "locale": "es-CL" }

200 OK
{
  "canonical": { "isrc": "GBDUW0000053", "mbid": "60fa767a-…", "title": "One More Time",
                 "artists": ["Daft Punk"], "durationMs": 320000, "year": 2001, "explicit": false },
  "candidates": [
    { "videoId": "…", "title": "…", "channel": "Daft Punk - Topic", "durationMs": 320000,
      "kind": "official_audio", "confidence": 0.97 },
    …
  ],
  "needsConfirmation": false        // true when top confidence < threshold or top-2 are too close
}

GET  /search?q=...&target=youtube   // the guest search box; same candidate shape
GET  /healthz
```

- `needsConfirmation` triggers bye0's 10 s QTE (the adder picks, the host can override, otherwise auto-pick the
  top candidate). Tune the threshold on the test set.
- Latency budget: **p95 < 2 s** for `/resolve` so the QTE doesn't feel laggy. Cache aggressively (URL → canonical,
  ISRC → candidates).
- Not needed in v1 but cheap to support: `canonical.isrc`/`mbid` will later power dedup guards, decade flow, and
  song trivia.

## Accuracy test set (v1 "done" criterion)

- ≥200 real links pasted by the two of you: Spotify + YT Music mostly, plus some Apple/Deezer.
- Deliberately include hard cases: remasters, live vs studio, clean vs explicit, songs with covers far more popular
  than the original, non-Latin-script titles, Spanish-language releases, regional exclusives.
- Label the "correct" YouTube video(s) by hand; the metric is **top-1 accuracy ≥95%**, and also report top-3.
- Run it in CI. ytmusicapi breakage will show up as a sudden accuracy drop.

## Open questions for the new session

1. Self-host a MusicBrainz mirror from day one, or start on the public API (1 req/s) with a cache?
2. Framework: FastAPI vs Litestar; Redis cache shared with the room server or separate?
3. How should a YT Music link to a *music video* be handled when an official-audio version exists?
   (Probably play what the guest linked.)
4. Region availability: does YouTube region-blocking on the VPS's IP differ from the party's country?
   (The VPS location matters.)
5. Fallback policy when ytmusicapi is down: switch to the YouTube Data API automatically, under a daily budget?
6. Repo name and license (bye0 uses WTFPL; ytmusicapi is MIT, which is compatible).
