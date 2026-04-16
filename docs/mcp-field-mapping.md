# MCP Field Mapping & Data Flow

Documents every field available from the DVR backends, what the MCP servers
return, and what the LLM actually receives after slimming/truncation.

## Data Flow

```
DVR API (raw)  →  MCP Server (enrich/slim)  →  Orchestrator (_slim_for_llm)  →  LLM (4KB max)
  ~5KB/rec          ~200B/rec (SageTV)            strips 10 fields             record-level
                    ~400B/rec (Channels)          from Channels results        truncation
```

## Truncation Strategy

- **Record-level, not string-level**: When results exceed 4KB, the orchestrator
  drops records from the end of the list (not mid-JSON) and appends
  `"note": "N more results omitted"`.
- **Field-level at MCP**: SageTV's `_slim_recording()` extracts 13 fields from
  a raw 3-5KB object. Channels' `_enrich_channels_recording()` extracts 15 fields.
- **Field-level at Orchestrator**: `_slim_for_llm()` strips 10 additional fields
  (cast, genres, image, etc.) that the frontend needs but the LLM doesn't.

---

## Channels DVR — Recording Fields

Source: `/dvr/files` API → `_enrich_channels_recording()` → `_slim_for_llm()`

| DVR API Field | MCP Returns | LLM Receives | Notes |
|---------------|:-----------:|:------------:|-------|
| `Airing.Title` | ✅ `title` | ✅ `title` | Show name |
| `Airing.EpisodeTitle` | ✅ `episode_title` | ✅ `episode_title` | Episode name |
| `Airing.SeasonNumber` + `EpisodeNumber` | ✅ `season_episode` | ✅ `season_episode` | Formatted "S01E05" |
| `Airing.Channel` | ✅ `channel` | ❌ stripped | Channel number |
| `Airing.Time` / `CreatedAt` | ✅ `recorded` | ✅ `recorded` | Human-readable date |
| `Airing.OriginalDate` | ✅ `original_date` | ❌ stripped | Original air date |
| `Duration` | ✅ `duration_min` | ❌ stripped | Duration in minutes |
| `Airing.FullSummary` / `Summary` | ✅ `description` | ❌ stripped | Episode synopsis |
| `Airing.Genres` | ✅ `genres` | ❌ stripped | Genre tags |
| `Airing.Image` | ✅ `image` | ❌ stripped | Thumbnail URL |
| `Airing.Cast` | ✅ `cast` | ❌ stripped | Actor list |
| `Airing.ContentRating` | ✅ `content_rating` | ❌ stripped | TV-14, TV-PG, etc. |
| `Watched` / `PlayedAt` | ✅ `watched` | ✅ `watched` | Boolean (PlayedAt fallback) |
| `Path` | ✅ `path` | ❌ stripped | File path on disk |
| `ID` | ✅ `id` | ✅ `id` | Recording ID |
| `Commercials` | ❌ | ❌ | Commercial markers (raw array) |
| `SignalStats` | ❌ | ❌ | Signal quality metrics |
| `BufferStats` | ❌ | ❌ | Buffer statistics |
| `MediaRegions` | ❌ | ❌ | Content/ad region markers |
| `Checksum` | ❌ | ❌ | File checksum |
| `FileSize` | ❌ | ❌ | File size in bytes |
| `Airing.Raw` | ❌ | ❌ | Raw EPG data blob |
| `Airing.ProgramID` | ❌ | ❌ | Schedules Direct ID |
| `Airing.SeriesID` | ❌ | ❌ | Series identifier |
| `Airing.Tags` | ❌ | ❌ | New/HD/CC tags |

**LLM sees 6 fields per recording** (~80 bytes each).
**Frontend popup sees 15 fields** (~400 bytes each).

---

## Channels DVR — Upcoming Fields

Source: `/dvr/jobs` API → `_enrich()` → `_slim_for_llm()`

| DVR API Field | MCP Returns | LLM Receives | Notes |
|---------------|:-----------:|:------------:|-------|
| `Airing.Title` / `Name` | ✅ `title` | ✅ `title` | Show name |
| `Airing.EpisodeTitle` | ✅ `episode_title` | ✅ `episode_title` | Episode name |
| `Airing.SeasonNumber` | ✅ `season` | ✅ `season` | Season number |
| `Airing.EpisodeNumber` | ✅ `episode` | ✅ `episode` | Episode number |
| `Channels[0]` | ✅ `channel` | ❌ stripped | Channel |
| `Airing.Time` | ✅ `start_time` | ✅ `start_time` | Scheduled air time |
| `Airing.Duration` | ✅ `duration_min` | ❌ stripped | Duration in minutes |
| `Airing.OriginalDate` | ✅ `original_date` | ❌ stripped | Original air date |
| `Airing.FullSummary` | ✅ `description` | ❌ stripped | Episode synopsis |
| `Airing.Image` | ✅ `image` | ❌ stripped | Thumbnail URL |
| `Airing.Genres` | ✅ `genres` | ❌ stripped | Genre tags |
| `Airing.Cast` | ✅ `cast` | ❌ stripped | Cast (max 5) |
| `Airing.ContentRating` | ✅ `content_rating` | ❌ stripped | Rating |
| `Airing.Tags` → "New" | ✅ `is_new` | ✅ `is_new` | New episode flag |
| `Airing.Tags` → "HD" | ✅ `is_hd` | ❌ stripped | HD flag |

**LLM sees 6 fields per upcoming** (~70 bytes each).

---

## Channels DVR — Channel Fields

Source: `/devices` API → `_get_channels()` (slimmed)

| DVR API Field | MCP Returns | LLM Receives | Notes |
|---------------|:-----------:|:------------:|-------|
| `Number` / `GuideNumber` | ✅ `number` | ✅ `number` | Channel number |
| `Name` / `GuideName` | ✅ `name` | ✅ `name` | Channel name |
| `Station` | ✅ `network` | ✅ `network` | Network affiliation |
| `HD` | ✅ `hd` | ❌ stripped | HD flag |
| `Logo` | ❌ | ❌ | Station logo URL |
| `Favorite` | ❌ | ❌ | Favorite flag |
| `GuideSource` | ❌ | ❌ | EPG data source |
| `DeviceID` | ❌ | ❌ | Tuner device |

---

## SageTV — Recording Fields

Source: SageX API `GetMediaFiles` → `_slim_recording()`

| SageX API Field | MCP Returns | LLM Receives | Notes |
|-----------------|:-----------:|:------------:|-------|
| `Airing.Show.ShowTitle` | ✅ `title` | ✅ `title` | Show name |
| `Airing.Show.ShowEpisode` | ✅ `episode_title` | ✅ `episode_title` | Episode name |
| `Airing.Show.ShowSeasonNumber` + `ShowEpisodeNumber` | ✅ `season_episode` | ✅ `season_episode` | Formatted "S01E05" |
| `Airing.Channel.ChannelName` | ✅ `channel` | ❌ stripped | Channel name |
| `FileStartTime` / `AiringStartTime` | ✅ `recorded` | ✅ `recorded` | Human-readable date |
| `FileDuration` | ✅ `duration_min` | ❌ stripped | Duration in minutes |
| `Airing.Show.ShowDescription` | ✅ `description` | ❌ stripped | Episode synopsis |
| `Airing.Show.ShowCategory` | ✅ `genres` | ❌ stripped | Category/genre |
| `Airing.Show.ShowImage` | ✅ `image` | ❌ stripped | Thumbnail URL |
| `Airing.Show.ShowCast` | ✅ `cast` | ❌ stripped | Actor list |
| `Airing.Show.ShowParentalRating` | ✅ `content_rating` | ❌ stripped | Rating |
| `Airing.IsWatched` | ✅ `watched` | ✅ `watched` | Boolean |
| `MediaFileID` | ✅ `id` | ✅ `id` | Recording ID |
| `FilePath` | ❌ | ❌ | Was raw, now excluded |
| `Segment` | ❌ | ❌ | Segment info (multi-part) |
| `FileSize` | ❌ | ❌ | File size |
| `FileEndTime` | ❌ | ❌ | Was in raw, now excluded |
| `Airing.AiringID` | ❌ | ❌ | Internal airing ID |
| `Airing.Show.ShowExternalID` | ❌ | ❌ | Schedules Direct ID |
| `Airing.Show.ShowID` | ❌ | ❌ | Internal show ID |

**LLM sees 6 fields per recording** (~80 bytes each) — same shape as Channels.

Previously: raw SageTV objects were 3-5 KB each → 50 recordings = 150-250 KB →
hard-truncated mid-JSON at 4 KB → LLM received broken data.

---

## SageTV — Upcoming Fields

Source: SageX API `GetScheduledRecordings` → `sagetv_get_upcoming_recordings()`

| SageX API Field | MCP Returns | Notes |
|-----------------|:-----------:|-------|
| `Airing.Show.ShowTitle` | ✅ `title` | Show name |
| `Airing.Show.ShowEpisode` | ✅ `episode_title` | Episode name |
| `Show.ShowSeasonNumber` + `ShowEpisodeNumber` | ✅ `season_episode` | Formatted "S01E05" |
| `Airing.Channel.ChannelName` | ✅ `channel` | Channel name |
| `Airing.AiringStartTime` | ✅ `start_time` | Human-readable date |

**5 fields per upcoming** — compact by default.

---

## SageTV — Channel Fields

Source: SageX API `GetAllChannels` → `sagetv_get_channels()`

| SageX API Field | MCP Returns | Notes |
|-----------------|:-----------:|-------|
| `ChannelNumber` | ✅ `number` | Channel number |
| `ChannelName` | ✅ `name` | Channel name |
| `ChannelNetwork` | ✅ `network` | Network affiliation |
| `ChannelDescription` | ❌ | Verbose description |
| `ChannelLogo` | ❌ | Logo path |
| `StationID` | ❌ | Internal station ID |

---

## Tool Filter Parameters

Shows which filters each search/query tool supports, enabling the LLM to
request precisely what it needs instead of fetching everything.

### Channels DVR

| Tool | title | channel | start_date | end_date | date | watched | status | limit |
|------|:-----:|:-------:|:----------:|:--------:|:----:|:-------:|:------:|:-----:|
| `channels_search_recordings` | ✅ | ✅ | ✅ | ✅ | | ✅ | | ✅ |
| `channels_get_recordings` | | | | | | | | ✅ |
| `channels_get_upcoming_recordings` | ✅ | ✅ | ✅ | ✅ | ✅ | | | |
| `channels_get_jobs` | | | | | | | ✅ | |
| `channels_get_channels` | | | | | | | | |
| `channels_search_epg` | query | | | | | | | |

### SageTV

| Tool | title | channel | start_date | end_date | watched | archived | recording_state | limit |
|------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:---------------:|:-----:|
| `sagetv_search_recordings` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `sagetv_get_recordings` | | | | | | | | ✅ |
| `sagetv_get_upcoming_recordings` | | | | | | | | |
| `sagetv_get_recent_recordings` | | | | | | | | ✅ |
| `sagetv_search_shows` | query | | | | | | | |

### Transcript

| Tool | query | actor | genre | channel | date_from | date_to | limit |
|------|:-----:|:-----:|:-----:|:-------:|:---------:|:-------:|:-----:|
| `transcript_search` | ✅ | | | | | | ✅ |
| `transcript_cross_search` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `transcript_actors` | | ✅ | | | | | ✅ |

---

## Orchestrator Strip Fields (`_LLM_STRIP_FIELDS`)

These fields are removed from ALL tool results before the LLM sees them.
They're preserved in the MCP response for frontend popup display.

```
cast, genres, image, content_rating, is_hd, path,
description, duration_min, original_date, channel
```

To re-enable a field for LLM visibility, remove it from `_LLM_STRIP_FIELDS`
in `backend/orchestrator/src/services/agent.py`.
