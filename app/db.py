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

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS omdb_usage (
  day TEXT PRIMARY KEY, count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rec_created  ON recommendations(created_at);
CREATE INDEX IF NOT EXISTS idx_rec_scan     ON recommendations(scan_id);
CREATE INDEX IF NOT EXISTS idx_scan_created ON scans(created_at);
CREATE INDEX IF NOT EXISTS idx_meta_fetched ON metadata_cache(fetched_at);
"""

DEFAULT_SETTINGS = {
    "default_user_ids": "",
    "recs_per_scan": "60",
    "tv_request_mode": "ask",   # ask | all | first
    "default_sort": "match",    # match | imdb | year
    "default_filter": "all",    # all | movie | tv
}

SORT_SQL = {
    "match": "match_score DESC NULLS LAST, imdb_rating DESC NULLS LAST",
    "imdb": "imdb_rating DESC NULLS LAST, match_score DESC NULLS LAST",
    "year": "year DESC NULLS LAST, match_score DESC NULLS LAST",
}


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
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)", (k, v)
            )


def purge_old(retention_days: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with _conn() as conn:
        r = conn.execute("DELETE FROM recommendations WHERE created_at < ?", (cutoff,)).rowcount
        s = conn.execute("DELETE FROM scans WHERE created_at < ?", (cutoff,)).rowcount
        m = conn.execute("DELETE FROM metadata_cache WHERE fetched_at < ?", (cutoff,)).rowcount
    # VACUUM must run outside an open transaction
    conn = get_conn()
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return {"recommendations": r, "scans": s, "metadata_cache": m}


# --- settings ----------------------------------------------------------------
def settings_get_all() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


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
            d.get("seerr_status"),
        ))
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO recommendations(id, scan_id, created_at, media_type, title, "
            "year, tmdb_id, imdb_id, imdb_rating, tmdb_rating, poster_url, "
            "backdrop_url, overview, genres, reason, match_score, because_of, "
            "is_in_library, seerr_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
) -> dict:
    """Filter (all|movie|tv) + sort (match|imdb|year) + paginate.
    Returns {items, total, page, pages, scan_id}."""
    scan_id = latest_scan_id() if scan in ("", "latest", None) else scan
    if not scan_id:
        return {"items": [], "total": 0, "page": 1, "pages": 0, "scan_id": None}

    where = "scan_id = ?"
    params: list = [scan_id]
    if media_type in ("movie", "tv"):
        where += " AND media_type = ?"
        params.append(media_type)

    order = SORT_SQL.get(sort, SORT_SQL["match"])
    page = max(1, int(page))
    per_page = max(1, min(100, int(per_page)))
    offset = (page - 1) * per_page

    with _conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM recommendations WHERE {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM recommendations WHERE {where} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, per_page, offset),
        ).fetchall()

    items = []
    for row in rows:
        d = dict(row)
        d["is_in_library"] = bool(d["is_in_library"])
        items.append(d)
    pages = (total + per_page - 1) // per_page
    return {"items": items, "total": total, "page": page, "pages": pages,
            "scan_id": scan_id}


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
