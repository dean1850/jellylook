# jellylook — implementation plan: feedback, filters, history, richer cards

Status: **proposal, not implemented.** No application code changes accompany this
document.

This plan covers three phases agreed during planning:

| Phase | Theme | Ships |
|---|---|---|
| 1 | Smarter picks | 👍/👎 feedback, permanent exclusion, prompt teaching, cross-scan dedupe |
| 2 | Filters + history | Genre/decade/runtime/rating filters, "all time" pool, scan picker |
| 3 | Richer cards | Where-to-watch (TMDb/JustWatch) and trailers |

Constraints fixed up front:

- **No new API keys.** Everything below runs on the `TMDB_API_KEY` and
  `OMDB_API_KEY` jellylook already requires. Trakt and MDBList were considered
  and deliberately dropped.
- **No new runtime Python dependencies.** `requirements.txt` stays at five lines.
  (`pytest` is proposed as a *dev* dependency only — see §5.)
- **Feedback is household-wide**, not per media-server user.
- Everything must remain free and legal for self-hosted, non-commercial use.

---

## 0. Where the current design blocks this

Three facts about today's code drive most of the design:

1. **The only exclusion signal is watched titles.** `recommender.py:79` builds
   `watched_titles` and that set is the entire memory of the system. Scan #5 can
   and does re-suggest what scan #1 already offered and you already declined.
   There is nowhere to record "no".

2. **Exclusion is matched on lowercased title strings**
   (`recommender.py:89`, `recommender.py:112`). That is fine for a fuzzy prompt
   hint, but it is not sound enforcement — `The Office` (US) and `The Office`
   (UK) collide, and `WALL·E` vs `WALL-E` does not. Any *hard* rule must key on
   `(media_type, tmdb_id)` after TMDb resolution.

3. **`purge_old()` deletes recommendations unconditionally**
   (`db.py:154-166`). The moment a user can save a title, that function becomes
   destructive. Fixing it is a prerequisite for Phase 1, not a follow-up.

---

## 1. Phase 1 — feedback and exclusion

### 1.1 Schema

One new table. Keyed on the resolved TMDb identity, not the title string.

```sql
CREATE TABLE IF NOT EXISTS feedback (
  media_type TEXT    NOT NULL,          -- movie | tv
  tmdb_id    INTEGER NOT NULL,
  verdict    TEXT    NOT NULL,          -- saved | hidden
  title      TEXT    NOT NULL,          -- denormalised: survives purge, feeds the prompt
  year       INTEGER,
  created_at TEXT    NOT NULL,
  PRIMARY KEY (media_type, tmdb_id)
);

CREATE INDEX IF NOT EXISTS idx_feedback_verdict ON feedback(verdict);
CREATE INDEX IF NOT EXISTS idx_rec_tmdb ON recommendations(media_type, tmdb_id);
```

`title` and `year` are denormalised on purpose. Feedback must outlive the
`recommendations` row it came from — otherwise a 👎 stops working after 60 days
and the title comes straight back.

`idx_rec_tmdb` is needed by the Phase 2 dedupe query and the feedback join. It
also speeds up `update_seerr_status()` (`db.py:351`), which currently does a full
table scan on exactly that predicate.

### 1.2 Two verdicts, not three

The feature was sketched as 👍/👎/🚫. With the two decisions taken during
planning — *household-wide* scope and *👎 means never suggest this again* — the
🚫 button has no behaviour left that 👎 does not already have. Shipping two
controls that do the same thing is worse than shipping one.

Recommendation: **`saved` and `hidden` only.** The `verdict` column is a free-text
enum, so adding a third value later costs one migration-free line. If a genuinely
distinct third action is wanted, the useful one is not a stronger title ban but a
*rule*: "never suggest horror", "nothing before 1980". That is a different table
and belongs in a later phase.

→ *Open question 1 in §7.*

### 1.3 Behaviour

| Action | Effect on views | Effect on next scan |
|---|---|---|
| 👍 `saved` | Appears in a new "Saved" view; card gets a filled marker | Listed to the model as a liked example |
| 👎 `hidden` | Removed from every result view; recoverable from a "Hidden" view | Hard-excluded, **and** listed as a rejected example |
| cleared | Back to normal | No longer referenced |

### 1.4 Prompt changes — and the budget problem

`llm.py:25` currently truncates the exclude list at a flat 200 titles:

```python
exclude_list = sorted(exclude)[:200]
```

Adding hidden titles and previously-suggested titles to that set pushes it past
500 entries. At roughly 5 tokens per title that is ~2,500 tokens of pure
exclusion — tolerable on a hosted frontier model, actively harmful on the local
Ollama setups jellylook advertises, where it crowds out the taste profile that
actually does the work.

**Proposed policy** — a fixed budget filled by priority, replacing the flat slice:

| Priority | Source | Cap |
|---|---|---|
| 1 | `hidden` titles | 100 |
| 2 | Watched titles, most recent first | 150 |
| 3 | Previously suggested, not acted on, newest first | fills remainder |

Total default **300**, exposed as `EXCLUDE_BUDGET` in `.env` so small-context
local models can shrink it. `sorted()` is replaced by recency ordering — dropping
titles alphabetically from `T` onward is arbitrary; dropping the oldest is not.

Liked titles go in a **separate** block, capped at 30, with a different
instruction. Sketch of the new prompt section in `_build_prompts()`:

```
They explicitly LIKED these earlier suggestions — lean toward what these share:
["Paddington 2 (2017)", ...]

They explicitly REJECTED these earlier suggestions — avoid these and close neighbours:
["The Kissing Booth (2018)", ...]

Never suggest anything in this exclude list:
[...]
```

**Over-fitting risk.** At `LLM_TEMPERATURE=0.7`, three rejections can swing a
model hard — reject two horror films and it may stop suggesting horror entirely.
Mitigations: only emit the liked/rejected blocks once there are **≥3** entries in
that verdict; cap each at 30; phrase them as "lean toward"/"avoid" rather than
absolute rules. Absolute language is reserved for the exclude list.

### 1.5 Enforcement is separate from the prompt

The prompt is advisory. Models will return hidden titles anyway. The hard filter
belongs in `recommender.py`, in the existing post-enrichment dedupe block
(`recommender.py:110-114`), where a real `tmdb_id` exists:

```python
if id_key in seen_ids or id_key in excluded_ids:
    continue
```

`excluded_ids` is loaded once per scan alongside the ownership set. This is the
only place exclusion is guaranteed correct, because it is the only place the
title has been resolved to an identity.

Scans should also log how many suggestions were dropped for being hidden — if
that number is consistently high, the prompt teaching is not landing, and that is
worth being able to see.

### 1.6 Purge safety (prerequisite)

`purge_old()` must not delete saved rows:

```sql
DELETE FROM recommendations
 WHERE created_at < ?
   AND NOT EXISTS (SELECT 1 FROM feedback f
                    WHERE f.media_type = recommendations.media_type
                      AND f.tmdb_id    = recommendations.tmdb_id
                      AND f.verdict    = 'saved');
```

The `feedback` table itself is **never** purged. It is small (one short row per
decision), it is the only user-authored data in the database, and expiring it
would silently resurrect rejected titles.

### 1.7 API

```
POST /api/feedback      {media_type, tmdb_id, verdict}   verdict: saved|hidden|null (null clears)
GET  /api/feedback      ?verdict=saved|hidden            → list for the Saved/Hidden views
```

`GET /api/recommendations` gains a `verdict` field per item so cards render in
the right state on load. Validation follows the existing `_SETTING_CHOICES`
pattern in `main.py:104-123`.

### 1.8 UI

Two icon buttons in `.card-actions` beside the Seerr button. Optimistic update;
on 👎 the card fades and is removed from the grid without a refetch. "Saved" and
"Hidden" become entries in the existing filter segment, reusing the current
`state.filter` plumbing in `app.js:435-450`.

### 1.9 Files touched

`app/db.py` · `app/models.py` · `app/recommender.py` · `app/llm.py` ·
`app/main.py` · `app/static/app.js` · `app/static/index.html` ·
`app/static/style.css` · `.env.example` · `README.md`

---

## 2. Phase 2 — filters and scan history

### 2.1 New columns on `recommendations`

Every one of these is already present in the TMDb detail response that
`metadata.py:77-81` fetches. **Zero additional network calls.**

| Column | Source | Why |
|---|---|---|
| `genre_ids TEXT` | `detail.genres[].id`, comma-joined | Stable, language-independent. The existing `genres` string is display text and a poor filter key |
| `runtime INTEGER` | movie `runtime`, TV `episode_run_time[0]` | "Under 2 hours tonight" |
| `original_language TEXT` | `original_language` | Foreign-film filter |
| `popularity REAL` | `popularity` | Optional extra sort |

### 2.2 Migration helper

`db.py` currently has one bespoke, marker-guarded migration
(`_migrate_unseed_recs_per_scan`). Adding four columns warrants a reusable,
idempotent helper rather than four more markers:

```python
def _add_column(conn, table, column, ddl):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
```

Safe to run on every boot; no marker row needed.

### 2.3 The NULL problem — call this out in the UI

Rows written before the upgrade have `NULL` in all four columns. Proposed
semantics:

- **Numeric filters** (runtime, year, min rating): `NULL` **passes**. Unknown is
  not the same as failing.
- **Genre filter**: `NULL` **fails**. We cannot claim a row is a Thriller when we
  have no genre data for it.

Consequence: switching on a genre filter in the "all time" view hides older
recommendations. That will look like a bug unless it is surfaced, so the grid
should show a one-line note — *"12 older titles hidden: no genre data (scanned
before v1.1)"* — whenever a genre filter excludes rows on NULL.

A backfill is possible but not free: `metadata_cache` stores the old `MediaMeta`
shape, which lacks these fields, so backfilling means re-querying TMDb for every
historical title. Recommendation: **do not backfill.** Filters are clean from the
first scan after upgrade, and the 60-day retention ages the gap out on its own.

### 2.4 Query surface

`GET /api/recommendations` extends to:

```
scan=latest | all | <scan_id>
type=all|movie|tv
sort=match|imdb|year|runtime|added
genre=<id>[,<id>]          any-of
year_min=, year_max=
runtime_max=
imdb_min=
in_library=any|hide|only
requested=any|hide
page=, per_page=
```

Keeping the existing `Query(pattern=...)` validation style from `main.py:186-198`.

### 2.5 The "all time" pool

`scan=all` returns every recommendation in the retention window, deduped by
`(media_type, tmdb_id)` keeping the newest, minus anything hidden:

```sql
WITH ranked AS (
  SELECT r.*, ROW_NUMBER() OVER (
           PARTITION BY r.media_type, r.tmdb_id
           ORDER BY r.created_at DESC, r.id DESC
         ) AS rn
    FROM recommendations r
)
SELECT ranked.* FROM ranked
LEFT JOIN feedback f
       ON f.media_type = ranked.media_type AND f.tmdb_id = ranked.tmdb_id
 WHERE ranked.rn = 1
   AND COALESCE(f.verdict, '') != 'hidden'
```

A window function is used rather than `GROUP BY … MAX(created_at)` because every
row in a scan shares one `created_at` (`recommender.py:63` reuses a single
timestamp for the whole batch). Within a scan the existing post-enrichment dedupe
guarantees `(media_type, tmdb_id)` is unique, so ties across scans are the only
case — `id DESC` settles them deterministically. SQLite has supported window
functions since 3.25; Python 3.12 ships far newer.

**The pool is bounded by `RETENTION_DAYS`.** "All time" means "all 60 days" by
default. Raising it grows the metadata cache too. Worth one sentence in the
README so nobody expects a permanent archive.

### 2.6 Scan picker

New `GET /api/scans` (the existing `/api/status` already returns
`last_scans(5)`; this promotes it to a first-class, paginated endpoint). Header
dropdown:

```
Latest scan · 2h ago · Dean            (default)
All time · 312 titles
─────────────
29 Jul · Dean, Sam · 58 titles
27 Jul · Dean · 60 titles
```

### 2.7 Files touched

`app/db.py` · `app/metadata.py` · `app/models.py` · `app/recommender.py` ·
`app/main.py` · `app/static/*` · `README.md`

---

## 3. Phase 3 — richer cards

### 3.1 One-line enrichment change

`metadata.py:79` becomes:

```python
"append_to_response": "external_ids,videos,watch/providers"
```

TMDb maps `append_to_response` keys 1:1 into the response, so this returns
external ids, trailers and streaming availability in the **same** request that
already runs. Enrichment stays at 1 TMDb search + 1 TMDb detail + 1 OMDb per
title. No new key, no new call, no change to the OMDb budget.

### 3.2 New fields

| Column | Content |
|---|---|
| `trailer_key TEXT` | YouTube video key. Selection order: `site == "YouTube"` and `type == "Trailer"` and `official` → any Trailer → any Teaser → `NULL` |
| `providers TEXT` | JSON, filtered to the configured region: `{"flatrate":[{"name":"Netflix","logo":"/x.jpg"}],"rent":[…],"link":"https://…"}` |

Providers are stored region-filtered to keep rows small. Changing `TMDB_REGION`
therefore affects new scans only; existing rows keep the region they were scanned
under. Documented, not worked around.

### 3.3 New config

```
TMDB_REGION=AU     # ISO 3166-1 country code for streaming availability
```

`AU` proposed to match the repo's existing `TZ=Australia/Sydney` default.
→ *Open question 3 in §7.*

### 3.4 Attribution — a hard requirement

TMDb's watch-provider data is licensed from JustWatch and carries a mandatory
attribution condition: the data source must be credited as JustWatch, and a
reference is expected **on each media item**, not only in an About box.

Non-negotiable deliverables for this phase:

1. A "JustWatch" label on the provider row of every card that shows one.
2. A line in the Help panel: *"Streaming availability data provided by JustWatch."*
3. A line in the README alongside the existing TMDb notice.

If any of the three is dropped, the feature ships out of compliance and should not
ship at all. This is the only part of the plan with an external legal obligation
attached.

### 3.5 Known TMDb quirk

The appended `watch/providers` block is reported to diverge from the dedicated
endpoint for some TV titles. Mitigation: when the appended block is absent or
empty **and** `media_type == "tv"`, fall back to
`GET /tv/{id}/watch/providers` — one extra call, only on TV misses, cached like
everything else.

### 3.6 Trailer UI — privacy note

The README states jellylook has *no telemetry*. An embedded YouTube iframe loads
Google scripts into the page and quietly breaks that claim.

Recommendation: **link out to YouTube by default** (a ▶ badge on the poster
opening a new tab), with an opt-in `Embed trailers inline` setting in the
Settings panel that uses `youtube-nocookie.com` when enabled. The privacy-safe
behaviour should be the one you get without reading the docs.

### 3.7 Free filter

Once providers are stored, `provider=streaming|not_streaming` costs almost
nothing and delivers the SuggestArr-style *"don't request what I can already
stream"* behaviour — arguably the most practically useful filter in the whole
plan.

---

## 4. Sequencing

Phases are independently shippable and land in this order because each depends on
the one before:

```
Phase 1  ──▶  Phase 2  ──▶  Phase 3
feedback      needs `feedback`      needs Phase 2's
+ purge fix   for the all-time      column-migration
              pool's hidden-filter  helper
```

Rough sizes: Phase 1 ≈ 400 lines across 8 files; Phase 2 ≈ 350 lines, mostly SQL
and UI; Phase 3 ≈ 200 lines, the smallest despite being the most visible.

---

## 5. Testing — the main risk

**There are currently no tests.** `app/selftest.py` is a live-API smoke check, not
a test suite, and CI only builds the image and curls `/health`.

This plan rewrites the scan pipeline's filtering, adds four migrations, and
introduces non-trivial SQL (a window function, a `LEFT JOIN` on an outer table, a
`NOT EXISTS` purge). Shipping that against zero tests is the largest risk here —
larger than any individual feature.

Proposed, and cheap because the interesting logic is already pure:

- `requirements-dev.txt` with `pytest` + `pytest-asyncio`. Runtime deps unchanged.
- `tests/test_db.py` — migrations are idempotent; all-time dedupe keeps the newest
  row; hidden rows are excluded; purge spares saved rows.
- `tests/test_llm.py` — `_extract_json` against real malformed model output; the
  exclude-budget allocator respects priority and cap.
- `tests/test_recommender.py` — `build_profile` weighting; hard exclusion by
  `tmdb_id`.
- A `pytest` step in `.github/workflows/docker-publish.yml` before the build.

None of it needs network access or an API key.

---

## 6. Documentation

- **README**: Features list; a new *Filters* section; `TMDB_REGION` and
  `EXCLUDE_BUDGET` in the Configuration tables; the JustWatch notice; a note that
  "all time" is bounded by `RETENTION_DAYS`.
- **Help panel** (`index.html:120-156`): what 👍/👎 do and that they steer the next
  scan; where streaming data comes from.
- **`.env.example`**: two new keys with comments.

---

## 7. Open questions

1. **Two verdicts or three?** §1.2 argues 🚫 is redundant with 👎 under
   household-wide scope and recommends dropping it. Confirm, or specify what 🚫
   should do that 👎 does not.
2. **Should 👍 imply a Seerr request?** A saved title you don't own is a strong
   signal you want it. Auto-requesting on 👍 is convenient but spends
   Seerr/download capacity without a confirmation step. Default proposed: **no**,
   with the Saved view offering a bulk "Request all" action instead.
3. **`TMDB_REGION` default** — `AU` (matching the existing `TZ` default) or the
   more conventional `US`?
4. **Does the "all time" view need its own retention?** Currently one
   `RETENTION_DAYS` governs scans, recommendations and the metadata cache. A
   longer horizon for the pool would mean a second knob.
5. **Ordering of phase 3.** It is the smallest and most visible change. Worth
   pulling ahead of phase 2 for a quicker visible win?
