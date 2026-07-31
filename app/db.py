"""SQLite layer — schema, settings kv, OMDb quota, metadata cache, purge,
and full scan/recommendation CRUD. Parameterised queries only.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DATA_DIR = os.getenv("DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "jellylook.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scans (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source_users TEXT NOT NULL,
  provider TEXT, model TEXT, item_count INTEGER, duration_ms INTEGER, status TEXT
);

CREATE TABLE IF NOT EXISTS recommendations (
  id TEXT PRIMARY KEY, scan_id TEXT NOT NULL, created_at TEXT NOT NULL,
  media_type TEXT NOT NULL,
  title TEXT NOT NULL, year INTEGER,
  tmdb_id INTEGER, imdb_id TEXT,
  imdb_rating REAL, tmdb_rating REAL,
  poster_url TEXT, backdrop_url TEXT, overview TEXT, genres TEXT,
  reason TEXT,
  match_score INTEGER,
  because_of TEXT,
  is_in_library INTEGER DEFAULT 0,
  seerr_status TEXT,
  FOREIGN KEY (scan_id) REFERENCES scans(id)
);

CREATE TABLE IF NOT EXISTS metadata_cache (
  cache_key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL
);

-- One viewer's verdict on one title. Keyed on the RESOLVED TMDb identity, not
-- the title string: 'The Office' (US) and 'The Office' (UK) are different rows.
-- title/year/poster_url are denormalised so a verdict outlives the
-- recommendation it came from — otherwise a hide would expire with the row.
CREATE TABLE IF NOT EXISTS feedback (
  user_id TEXT NOT NULL,
  media_type TEXT NOT NULL,
  tmdb_id INTEGER NOT NULL,
  verdict TEXT NOT NULL,          -- saved | hidden
  title TEXT NOT NULL,
  year INTEGER,
  poster_url TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, media_type, tmdb_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS omdb_usage (
  day TEXT PRIMARY KEY, count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY, value TEXT
);

CREATE INDEX IF NOT EXISTS idx_rec_created  ON recommendations(created_at);
CREATE INDEX IF NOT EXISTS idx_rec_scan     ON recommendations(scan_id);
CREATE INDEX IF NOT EXISTS idx_scan_created ON scans(created_at);
CREATE INDEX IF NOT EXISTS idx_meta_fetched ON metadata_cache(fetched_at);
-- Serves the all-time dedupe, the feedback join, and update_seerr_status(),
-- which previously scanned the whole table on exactly this predicate.
CREATE INDEX IF NOT EXISTS idx_rec_tmdb     ON recommendations(media_type, tmdb_id);
CREATE INDEX IF NOT EXISTS idx_fb_verdict   ON feedback(user_id, verdict);
CREATE INDEX IF NOT EXISTS idx_fb_title     ON feedback(media_type, tmdb_id);
"""

# Columns added after 1.0. ALTER TABLE ADD COLUMN is applied idempotently at
# startup by _add_column(), so upgrades need no marker rows and no downtime.
# Every one of these is already present in the TMDb detail response that
# metadata.py fetches — they cost no extra network calls.
ADDED_COLUMNS = [
    ("recommendations", "genre_ids", "TEXT"),
    ("recommendations", "runtime", "INTEGER"),
    ("recommendations", "original_language", "TEXT"),
    ("recommendations", "popularity", "REAL"),
    ("recommendations", "trailer_key", "TEXT"),
    ("recommendations", "providers", "TEXT"),
]

DEFAULT_SETTINGS = {
    "default_user_ids": "",
    "tv_request_mode": "ask",   # ask | all | first
    "default_sort": "match",    # match | imdb | year
    "default_filter": "all",    # all | movie | tv
}

# Settings whose default comes from .env rather than from DEFAULT_SETTINGS.
# They are deliberately NOT seeded: a missing row means "use the .env value",
# and a row exists only once the user saves one in the Settings panel.
ENV_BACKED_SETTINGS = {"recs_per_scan"}

ALLOWED_SETTINGS = set(DEFAULT_SETTINGS) | ENV_BACKED_SETTINGS

# recs_per_scan used to be seeded at this value, which made RECS_PER_SCAN in
# .env dead config. Dropping that row once lets .env through again.
_LEGACY_SEEDED_RECS_PER_SCAN = "60"

RECS_PER_SCAN_MIN = 5
RECS_PER_SCAN_MAX = 100


def clamp_recs_per_scan(value: object) -> int:
    """Coerce a batch size into the supported range.

    Raises ValueError/TypeError for anything non-numeric so callers can decide
    between rejecting the input and falling back to a default.
    """
    return max(RECS_PER_SCAN_MIN, min(RECS_PER_SCAN_MAX, int(value)))


SORT_SQL = {
    "match": "match_score DESC NULLS LAST, imdb_rating DESC NULLS LAST",
    "imdb": "imdb_rating DESC NULLS LAST, match_score DESC NULLS LAST",
    "year": "year DESC NULLS LAST, match_score DESC NULLS LAST",
    "runtime": "runtime ASC NULLS LAST, match_score DESC NULLS LAST",
    "added": "created_at DESC, match_score DESC NULLS LAST",
    "popularity": "popularity DESC NULLS LAST, match_score DESC NULLS LAST",
}

VERDICTS = ("saved", "hidden")

# TMDb genre ids are stable and few, so the id→name map is embedded rather than
# fetched. Movie and TV lists are merged; where TMDb splits a genre across the
# two vocabularies (Action vs Action & Adventure) both ids are kept so a filter
# on either still matches what the API returned.
TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy",
    36: "History", 27: "Horror", 10402: "Music", 9648: "Mystery",
    10749: "Romance", 878: "Science Fiction", 10770: "TV Movie",
    53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News",
    10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap",
    10767: "Talk", 10768: "War & Politics",
}


def genre_key(ids: list[int] | None) -> str | None:
    """Sentinel-wrap genre ids as ',28,12,' so LIKE '%,12,%' cannot match 120."""
    if not ids:
        return None
    return "," + ",".join(str(i) for i in ids) + ","


def parse_genre_key(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(p) for p in value.strip(",").split(",") if p.strip().isdigit()]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def _conn():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with _conn() as conn:
        conn.executescript(SCHEMA_SQL)
        for table, column, ddl in ADDED_COLUMNS:
            _add_column(conn, table, column, ddl)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)", (k, v)
            )
        _migrate_unseed_recs_per_scan(conn)


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column if it isn't there. Idempotent, so it can run every startup.

    SQLite has no ADD COLUMN IF NOT EXISTS, and no way to add one inside a
    CREATE TABLE IF NOT EXISTS, so existing databases need this to pick up
    post-1.0 columns. Rows written before the upgrade keep NULL; the query
    layer treats NULL as "unknown" rather than "excluded" for numeric filters.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_unseed_recs_per_scan(conn: sqlite3.Connection) -> None:
    """Drop the auto-seeded recs_per_scan row so RECS_PER_SCAN applies again.

    Runs exactly once (guarded by schema_meta), and only removes the value the
    old code seeded — a value the user chose themselves is left alone unless it
    happens to equal that seed, in which case .env takes over.
    """
    marker = "unseed_recs_per_scan"
    done = conn.execute(
        "SELECT 1 FROM schema_meta WHERE key = ?", (marker,)
    ).fetchone()
    if done:
        return
    conn.execute(
        "DELETE FROM app_settings WHERE key = 'recs_per_scan' AND value = ?",
        (_LEGACY_SEEDED_RECS_PER_SCAN,),
    )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?)", (marker, _utcnow())
    )


def purge_old(retention_days: int, max_retention_days: int = 365) -> dict:
    """Two-stage purge.

    Stage 1 (retention_days) clears ordinary rows but SPARES anything a viewer
    saved — without that exemption this function would quietly delete the one
    kind of row the user asked to keep.

    Stage 2 (max_retention_days, default one year) is an absolute ceiling and
    spares nothing, so saved titles cannot pin rows in the database forever and
    an instance left running for years still bounds its own disk use.

    The `feedback` table itself is never purged. It is a few dozen bytes per
    decision and it is the only user-authored data here — expiring it would
    silently resurrect titles someone had already rejected.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    hard_cutoff = (now - timedelta(days=max(retention_days, max_retention_days))).isoformat()

    with _conn() as conn:
        # Stage 1 — ordinary retention, saved rows exempt.
        removed = conn.execute(
            "DELETE FROM recommendations WHERE created_at < ? AND NOT EXISTS ("
            "  SELECT 1 FROM feedback f WHERE f.media_type = recommendations.media_type"
            "    AND f.tmdb_id = recommendations.tmdb_id AND f.verdict = 'saved')",
            (cutoff,),
        ).rowcount
        # Stage 2 — hard ceiling, nothing exempt.
        expired = conn.execute(
            "DELETE FROM recommendations WHERE created_at < ?", (hard_cutoff,)
        ).rowcount
        m = conn.execute(
            "DELETE FROM metadata_cache WHERE fetched_at < ?", (cutoff,)
        ).rowcount
        # Scans are the FK parent, so they can only go once no row references
        # them — a saved recommendation keeps its scan alive deliberately.
        s = conn.execute(
            "DELETE FROM scans WHERE created_at < ? AND NOT EXISTS ("
            "  SELECT 1 FROM recommendations r WHERE r.scan_id = scans.id)",
            (cutoff,),
        ).rowcount
        q = conn.execute("DELETE FROM omdb_usage WHERE day < ?",
                         (hard_cutoff[:10],)).rowcount
    # VACUUM must run outside an open transaction
    conn = get_conn()
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return {"recommendations": removed + expired, "expired_at_ceiling": expired,
            "scans": s, "metadata_cache": m, "omdb_usage": q}


# --- settings ----------------------------------------------------------------
def settings_get_all() -> dict:
    """Stored settings only — an env-backed key is absent until it is saved."""
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def settings_effective(recs_per_scan: int) -> dict:
    """Stored settings with .env filling in the env-backed keys.

    This is what the UI shows, so the Settings panel displays the value a scan
    would actually use rather than a hard-coded placeholder.
    """
    values = settings_get_all()
    values.setdefault("recs_per_scan", str(clamp_recs_per_scan(recs_per_scan)))
    return values


def settings_clear(key: str) -> None:
    """Remove a stored override so its .env default applies again."""
    with _conn() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))


def settings_set(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- OMDb quota ---------------------------------------------------------------
def omdb_increment(n: int = 1) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO omdb_usage(day, count) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET count = count + ?",
            (_today(), n, n),
        )


def omdb_today() -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT count FROM omdb_usage WHERE day = ?", (_today(),)
        ).fetchone()
    return row["count"] if row else 0


# --- metadata cache -----------------------------------------------------------
def cache_get(key: str, ttl_days: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT fetched_at, payload FROM metadata_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"])
    if datetime.now(timezone.utc) - fetched > timedelta(days=ttl_days):
        return None
    return json.loads(row["payload"])


def cache_set(key: str, payload: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO metadata_cache(cache_key, fetched_at, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "fetched_at=excluded.fetched_at, payload=excluded.payload",
            (key, _utcnow(), json.dumps(payload)),
        )


# --- feedback -------------------------------------------------------------------
# Feedback is per media-server user. There is deliberately no "who is voting"
# setting: the verdict is attributed to whoever is ticked in the user chips,
# which is already the selection a scan runs for. One person ticked means one
# person's opinion; a household watching together ticks both and speaks for
# both. Nothing extra to configure, and the attribution matches what the UI
# already shows.
def feedback_set(user_ids: list[str], media_type: str, tmdb_id: int,
                 verdict: str, title: str, year: int | None = None,
                 poster_url: str | None = None) -> int:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    rows = [(uid, media_type, tmdb_id, verdict, title, year, poster_url, _utcnow())
            for uid in user_ids]
    with _conn() as conn:
        cur = conn.executemany(
            "INSERT INTO feedback(user_id, media_type, tmdb_id, verdict, title, "
            "year, poster_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, media_type, tmdb_id) DO UPDATE SET "
            "verdict=excluded.verdict, title=excluded.title, year=excluded.year, "
            "poster_url=excluded.poster_url, created_at=excluded.created_at",
            rows,
        )
        return cur.rowcount


def feedback_clear(user_ids: list[str], media_type: str, tmdb_id: int) -> int:
    if not user_ids:
        return 0
    marks = ",".join("?" for _ in user_ids)
    with _conn() as conn:
        cur = conn.execute(
            f"DELETE FROM feedback WHERE media_type = ? AND tmdb_id = ? "
            f"AND user_id IN ({marks})",
            (media_type, tmdb_id, *user_ids),
        )
        return cur.rowcount


def hidden_ids(user_ids: list[str]) -> set[str]:
    """'{media_type}:{tmdb_id}' keys hidden by ANY of these users.

    A scan for several people unions their hidden lists: if one person in the
    room has rejected a title, the group does not get it offered again.
    """
    if not user_ids:
        return set()
    marks = ",".join("?" for _ in user_ids)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT media_type, tmdb_id FROM feedback "
            f"WHERE verdict = 'hidden' AND user_id IN ({marks})",
            tuple(user_ids),
        ).fetchall()
    return {f"{row['media_type']}:{row['tmdb_id']}" for row in rows}


def feedback_titles(user_ids: list[str], verdict: str, limit: int) -> list[str]:
    """Newest-first 'Title (Year)' strings for the LLM prompt's taste blocks."""
    if not user_ids or limit <= 0:
        return []
    marks = ",".join("?" for _ in user_ids)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT title, MAX(year) AS year, MAX(created_at) AS created_at "
            f"FROM feedback WHERE verdict = ? AND user_id IN ({marks}) "
            f"GROUP BY media_type, tmdb_id ORDER BY created_at DESC LIMIT ?",
            (verdict, *user_ids, limit),
        ).fetchall()
    return [f"{r['title']} ({r['year']})" if r["year"] else r["title"] for r in rows]


def feedback_counts(user_ids: list[str]) -> dict:
    if not user_ids:
        return {"saved": 0, "hidden": 0}
    marks = ",".join("?" for _ in user_ids)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT verdict, COUNT(DISTINCT media_type || ':' || tmdb_id) AS c "
            f"FROM feedback WHERE user_id IN ({marks}) GROUP BY verdict",
            tuple(user_ids),
        ).fetchall()
    counts = {"saved": 0, "hidden": 0}
    for row in rows:
        counts[row["verdict"]] = row["c"]
    return counts


def _fb_cte(user_ids: list[str]) -> tuple[str, list]:
    """CTE ranking each title's verdict for these users: 2 hidden, 1 saved.

    hidden outranks saved so that in a multi-user selection a single rejection
    wins — the safe direction, since showing something one viewer has banned is
    worse than omitting something another merely bookmarked.
    """
    if not user_ids:
        return ("fb AS (SELECT NULL AS media_type, NULL AS tmdb_id, "
                "NULL AS rank WHERE 0)", [])
    marks = ",".join("?" for _ in user_ids)
    return (
        f"fb AS (SELECT media_type, tmdb_id, "
        f"MAX(CASE WHEN verdict = 'hidden' THEN 2 ELSE 1 END) AS rank, "
        f"MAX(created_at) AS marked_at, MAX(title) AS fb_title, "
        f"MAX(year) AS fb_year, MAX(poster_url) AS fb_poster "
        f"FROM feedback WHERE user_id IN ({marks}) GROUP BY media_type, tmdb_id)",
        list(user_ids),
    )


def get_feedback_items(user_ids: list[str], verdict: str, page: int = 1,
                       per_page: int = 20) -> dict:
    """Saved / Hidden views.

    Driven by the `feedback` table and enriched from the newest surviving
    recommendation, so an entry still lists after its recommendation row has
    been purged — just with fewer details.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    if not user_ids:
        return {"items": [], "total": 0, "page": 1, "pages": 0, "scan_id": None}

    fb_sql, fb_params = _fb_cte(user_ids)
    rank = 2 if verdict == "hidden" else 1
    page = max(1, int(page))
    per_page = max(1, min(100, int(per_page)))

    sql = (
        f"WITH {fb_sql}, ranked AS (SELECT r.*, ROW_NUMBER() OVER ("
        f"PARTITION BY r.media_type, r.tmdb_id "
        f"ORDER BY r.created_at DESC, r.id DESC) AS rn FROM recommendations r) "
        f"SELECT fb.media_type, fb.tmdb_id, fb.marked_at, "
        f"COALESCE(r.title, fb.fb_title) AS title, "
        f"COALESCE(r.year, fb.fb_year) AS year, "
        f"COALESCE(r.poster_url, fb.fb_poster) AS poster_url, "
        f"r.imdb_id, r.imdb_rating, r.tmdb_rating, r.backdrop_url, r.overview, "
        f"r.genres, r.genre_ids, r.runtime, r.original_language, r.popularity, "
        f"r.trailer_key, r.providers, r.reason, r.match_score, r.because_of, "
        f"r.is_in_library, r.seerr_status "
        f"FROM fb LEFT JOIN ranked r ON r.media_type = fb.media_type "
        f"AND r.tmdb_id = fb.tmdb_id AND r.rn = 1 "
        f"WHERE fb.rank = ? "
    )
    with _conn() as conn:
        total = conn.execute(
            f"WITH {fb_sql} SELECT COUNT(*) AS c FROM fb WHERE fb.rank = ?",
            (*fb_params, rank),
        ).fetchone()["c"]
        rows = conn.execute(
            sql + "ORDER BY fb.marked_at DESC LIMIT ? OFFSET ?",
            (*fb_params, rank, per_page, (page - 1) * per_page),
        ).fetchall()

    items = []
    for row in rows:
        d = dict(row)
        d["is_in_library"] = bool(d.get("is_in_library"))
        d["verdict"] = verdict
        items.append(d)
    return {"items": items, "total": total, "page": page,
            "pages": (total + per_page - 1) // per_page, "scan_id": None}


# --- scans + recommendations ---------------------------------------------------
def insert_scan(scan) -> None:
    """Persist a ScanRecord (pydantic model or dict)."""
    d = scan.model_dump() if hasattr(scan, "model_dump") else dict(scan)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO scans(id, created_at, source_users, provider, model, "
            "item_count, duration_ms, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (d["id"], d["created_at"], d["source_users"], d.get("provider"),
             d.get("model"), d.get("item_count", 0), d.get("duration_ms", 0),
             d.get("status", "ok")),
        )


def insert_recommendations(scan_id: str, recs: list) -> None:
    """Bulk insert Recommendation rows (pydantic models or dicts)."""
    rows = []
    for rec in recs:
        d = rec.model_dump() if hasattr(rec, "model_dump") else dict(rec)
        rows.append((
            d["id"], scan_id, d["created_at"], d["media_type"], d["title"],
            d.get("year"), d.get("tmdb_id"), d.get("imdb_id"),
            d.get("imdb_rating"), d.get("tmdb_rating"),
            d.get("poster_url"), d.get("backdrop_url"), d.get("overview"),
            d.get("genres"), d.get("reason"), d.get("match_score"),
            d.get("because_of"), 1 if d.get("is_in_library") else 0,
            d.get("seerr_status"), d.get("genre_ids"), d.get("runtime"),
            d.get("original_language"), d.get("popularity"),
            d.get("trailer_key"), d.get("providers"),
        ))
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO recommendations(id, scan_id, created_at, media_type, title, "
            "year, tmdb_id, imdb_id, imdb_rating, tmdb_rating, poster_url, "
            "backdrop_url, overview, genres, reason, match_score, because_of, "
            "is_in_library, seerr_status, genre_ids, runtime, original_language, "
            "popularity, trailer_key, providers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            rows,
        )


def latest_scan_id() -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM scans WHERE status != 'failed' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def get_scan(scan_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def get_recommendations(
    scan: str = "latest",
    media_type: str = "all",
    sort: str = "match",
    page: int = 1,
    per_page: int = 20,
    user_ids: list[str] | None = None,
    genres: list[int] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    runtime_max: int | None = None,
    imdb_min: float | None = None,
    in_library: str = "any",
    requested: str = "any",
    streaming: str = "any",
) -> dict:
    """Browse recommendations for one scan, or the whole retention window.

    scan="all" collapses every scan into one pool, deduped by (media_type,
    tmdb_id) keeping the newest sighting. Titles hidden by any of `user_ids`
    are excluded throughout.

    Returns {items, total, page, pages, scan_id, facets, dropped_no_genre}.
    """
    user_ids = user_ids or []
    all_time = scan == "all"
    scan_id = None
    if not all_time:
        scan_id = latest_scan_id() if scan in ("", "latest", None) else scan
        if not scan_id:
            return {"items": [], "total": 0, "page": 1, "pages": 0,
                    "scan_id": None, "facets": {"genres": []},
                    "dropped_no_genre": 0}

    if all_time:
        base_sql = ("base AS (SELECT * FROM (SELECT r.*, ROW_NUMBER() OVER ("
                    "PARTITION BY r.media_type, r.tmdb_id "
                    "ORDER BY r.created_at DESC, r.id DESC) AS rn "
                    "FROM recommendations r) WHERE rn = 1)")
        base_params: list = []
    else:
        base_sql = "base AS (SELECT * FROM recommendations WHERE scan_id = ?)"
        base_params = [scan_id]

    fb_sql, fb_params = _fb_cte(user_ids)
    head = f"WITH {base_sql}, {fb_sql} "
    join = ("FROM base LEFT JOIN fb ON fb.media_type = base.media_type "
            "AND fb.tmdb_id = base.tmdb_id ")

    # Hidden titles never appear in a browsing view; that is the whole point of
    # hiding one. Saved titles stay visible, flagged.
    clauses = ["COALESCE(fb.rank, 0) != 2"]
    params: list = []

    if media_type in ("movie", "tv"):
        clauses.append("base.media_type = ?")
        params.append(media_type)
    # NULL is "unknown", not "fails" — rows written before these columns
    # existed must not silently vanish from every numeric filter.
    if year_min is not None:
        clauses.append("(base.year IS NULL OR base.year >= ?)")
        params.append(int(year_min))
    if year_max is not None:
        clauses.append("(base.year IS NULL OR base.year <= ?)")
        params.append(int(year_max))
    if runtime_max is not None:
        clauses.append("(base.runtime IS NULL OR base.runtime <= ?)")
        params.append(int(runtime_max))
    if imdb_min is not None:
        clauses.append("(base.imdb_rating IS NULL OR base.imdb_rating >= ?)")
        params.append(float(imdb_min))
    if in_library == "hide":
        clauses.append("base.is_in_library = 0")
    elif in_library == "only":
        clauses.append("base.is_in_library = 1")
    if requested == "hide":
        clauses.append("base.seerr_status IS NULL")
    # providers is stored region-filtered with empty categories omitted, so the
    # presence of a "flatrate" key is a reliable "you can already stream this".
    if streaming == "only":
        clauses.append("base.providers LIKE '%\"flatrate\"%'")
    elif streaming == "hide":
        clauses.append("(base.providers IS NULL OR "
                       "base.providers NOT LIKE '%\"flatrate\"%')")

    # Genre is applied last and kept separate: facet counts are computed
    # WITHOUT it, so choosing a genre never empties the genre picker itself.
    pre_genre = list(clauses)
    pre_genre_params = list(params)
    if genres:
        # Unlike the numeric filters, an unknown genre fails: we cannot claim a
        # row is a Thriller when we have no genre data for it.
        ors = " OR ".join("base.genre_ids LIKE ?" for _ in genres)
        clauses.append(f"({ors})")
        params.extend(f"%,{int(g)},%" for g in genres)

    where = " WHERE " + " AND ".join(clauses)
    pre_where = " WHERE " + " AND ".join(pre_genre)
    order = SORT_SQL.get(sort, SORT_SQL["match"])
    order = ", ".join("base." + part.lstrip() for part in order.split(","))
    page = max(1, int(page))
    per_page = max(1, min(100, int(per_page)))

    cte_params = (*base_params, *fb_params)
    with _conn() as conn:
        total = conn.execute(
            f"{head}SELECT COUNT(*) AS c {join}{where}",
            (*cte_params, *params),
        ).fetchone()["c"]
        rows = conn.execute(
            f"{head}SELECT base.*, CASE fb.rank WHEN 2 THEN 'hidden' "
            f"WHEN 1 THEN 'saved' END AS verdict {join}{where} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*cte_params, *params, per_page, (page - 1) * per_page),
        ).fetchall()
        facet_rows = conn.execute(
            f"{head}SELECT base.genre_ids {join}{pre_where}",
            (*cte_params, *pre_genre_params),
        ).fetchall()

    items = []
    for row in rows:
        d = dict(row)
        d.pop("rn", None)  # window-function bookkeeping, not part of the API
        d["is_in_library"] = bool(d["is_in_library"])
        items.append(d)

    counts: dict[int, int] = {}
    missing = 0
    for row in facet_rows:
        ids = parse_genre_key(row["genre_ids"])
        if not ids:
            missing += 1
        for gid in ids:
            counts[gid] = counts.get(gid, 0) + 1
    facets = {"genres": sorted(
        ({"id": gid, "name": TMDB_GENRES.get(gid, str(gid)), "count": c}
         for gid, c in counts.items()),
        key=lambda g: (-g["count"], g["name"]),
    )}

    return {"items": items, "total": total, "page": page,
            "pages": (total + per_page - 1) // per_page,
            "scan_id": scan_id, "facets": facets,
            # Surfaced so the UI can explain a genre filter hiding pre-upgrade
            # rows, instead of it looking like results silently went missing.
            "dropped_no_genre": missing if genres else 0}


def recent_suggested_titles(limit: int = 300) -> list[str]:
    """Newest-first titles already offered in the retention window.

    Fed to the prompt so a scan spends its slots on titles the viewer has not
    already been shown and passed over, rather than re-offering the same pool.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT title, MAX(year) AS year, MAX(created_at) AS created_at "
            "FROM recommendations GROUP BY media_type, tmdb_id "
            "ORDER BY created_at DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
    return [f"{r['title']} ({r['year']})" if r["year"] else r["title"] for r in rows]


def all_time_total(user_ids: list[str] | None = None) -> int:
    """Distinct, non-hidden titles across the whole retention window."""
    fb_sql, fb_params = _fb_cte(user_ids or [])
    with _conn() as conn:
        row = conn.execute(
            f"WITH {fb_sql} SELECT COUNT(*) AS c FROM ("
            f"SELECT DISTINCT r.media_type, r.tmdb_id FROM recommendations r "
            f"LEFT JOIN fb ON fb.media_type = r.media_type "
            f"AND fb.tmdb_id = r.tmdb_id WHERE COALESCE(fb.rank, 0) != 2)",
            tuple(fb_params),
        ).fetchone()
    return row["c"] if row else 0


def list_scans(limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scans ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def update_seerr_status(media_type: str, tmdb_id: int, status: str | None) -> int:
    """Persist a Seerr state on every rec matching this title. Returns rowcount."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE recommendations SET seerr_status = ? "
            "WHERE media_type = ? AND tmdb_id = ?",
            (status, media_type, tmdb_id),
        )
        return cur.rowcount
