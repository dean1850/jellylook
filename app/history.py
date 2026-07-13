"""Watch-history adapters.

Two server families are supported, selected by HISTORY_SOURCE:
  - Jellyfin: Jellystat is PRIMARY (what the user is actually watching);
    Jellyfin is the fallback source and the ownership/id helper.
  - Plex: Tautulli supplies users, history AND the ownership check — user ids
    are Plex ids, so there is deliberately no Jellyfin fallback in this mode.

Jellystat's API is thin, under-documented and mid-rebuild, so this module is
deliberately defensive: several endpoint patterns are tried per call, every
response shape is unwrapped (list / {results} / {data} / ...) and every field
lookup tolerates absence. Failures degrade to the Jellyfin fallback rather
than crashing a scan. Tautulli has one stable endpoint (/api/v2?cmd=...) with
a fixed envelope, so its adapter is straightforward.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from . import http
from .config import get_settings
from .models import UserRef, WatchedItem

log = logging.getLogger("jellylook.history")


# --- helpers -------------------------------------------------------------------
def _jellystat_headers() -> dict[str, str]:
    return {"x-api-token": get_settings().jellystat_api_key}


def _jellyfin_headers() -> dict[str, str]:
    return {"Authorization": f'MediaBrowser Token="{get_settings().jellyfin_api_key}"'}


def _unwrap(data: Any) -> list[dict]:
    """Accept a bare list or common wrapper shapes and return a list of dicts."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "data", "items", "Items", "history", "response"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        # single-object response
        return [data]
    return []


def _first(d: dict, *keys: str) -> Any:
    """Return the first present, non-empty value among case-variant keys."""
    for k in keys:
        for variant in (k, k.lower(), k[0].upper() + k[1:] if k else k):
            if variant in d and d[variant] not in (None, "", "N/A"):
                return d[variant]
    return None


def _parse_dt(value: Any) -> str | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _parse_epoch(value: Any) -> str | None:
    """Tautulli timestamps are unix epoch seconds."""
    ts = _safe_int(value)
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def title_key(media_type: str, title: str, year: int | None) -> str:
    """Ownership key for servers where provider ids are impractical.

    Plex guids are only exposed by Tautulli via get_metadata (one call per
    library item), so Plex ownership matches on type+title+year instead.
    """
    return f"title:{media_type}:{title.strip().lower()}:{year or ''}"


# --- tautulli --------------------------------------------------------------------
async def _tautulli_data(cmd: str, **params: Any) -> Any:
    """Call Tautulli's single /api/v2 endpoint and return response.data
    (None on any failure). Envelope: {"response": {"result", "message", "data"}}.
    """
    s = get_settings()
    query: dict[str, Any] = {"apikey": s.tautulli_api_key, "cmd": cmd, **params}
    try:
        resp = await http.request("GET", f"{s.tautulli_url.rstrip('/')}/api/v2",
                                  params=query)
        if resp.status_code != 200:
            log.warning("Tautulli %s returned HTTP %s", cmd, resp.status_code)
            return None
        envelope = (resp.json() or {}).get("response") or {}
        if envelope.get("result") != "success":
            log.warning("Tautulli %s failed: %s", cmd, envelope.get("message"))
            return None
        return envelope.get("data")
    except Exception as exc:
        log.warning("Tautulli %s unreachable: %s", cmd, type(exc).__name__)
        return None


# --- users -----------------------------------------------------------------------
async def get_users() -> list[UserRef]:
    """User list from the active history source.

    Tautulli mode returns Plex users (no Jellyfin fallback — the ids would
    not match). Jellystat mode falls back to Jellyfin GET /Users.
    """
    s = get_settings()
    if s.history_source == "tautulli":
        return await _tautulli_users()
    if s.history_source == "jellystat" and s.jellystat_api_key:
        for path in ("/api/getAllUsers", "/api/getUsers", "/stats/getAllUsers"):
            users = await _try_jellystat_users(path)
            if users:
                return users
        log.warning("Jellystat user list unavailable — falling back to Jellyfin")
    return await _jellyfin_users()


async def _try_jellystat_users(path: str) -> list[UserRef]:
    s = get_settings()
    try:
        resp = await http.request("GET", f"{s.jellystat_url}{path}",
                                  headers=_jellystat_headers())
        if resp.status_code != 200:
            return []
        users = []
        for row in _unwrap(resp.json()):
            uid = _first(row, "UserId", "Id", "userid", "user_id")
            name = _first(row, "UserName", "Name", "username", "FriendlyName")
            if uid and name:
                users.append(UserRef(id=str(uid), name=str(name)))
        return users
    except Exception as exc:
        log.debug("jellystat users via %s failed: %s", path, type(exc).__name__)
        return []


async def _tautulli_users() -> list[UserRef]:
    users = []
    for row in _unwrap(await _tautulli_data("get_users")):
        uid = row.get("user_id")
        name = row.get("friendly_name") or row.get("username")
        # user_id 0 is Tautulli's "Local" pseudo-user, not a real viewer.
        if uid in (None, "") or str(uid) == "0" or not name:
            continue
        if row.get("deleted_user") or row.get("is_active") in (0, False):
            continue
        users.append(UserRef(id=str(uid), name=str(name)))
    if not users:
        log.warning("Tautulli returned no users — check TAUTULLI_URL/API key")
    return users


async def _jellyfin_users() -> list[UserRef]:
    s = get_settings()
    try:
        resp = await http.request("GET", f"{s.jellyfin_url}/Users",
                                  headers=_jellyfin_headers())
        if resp.status_code != 200:
            log.error("Jellyfin /Users returned %s", resp.status_code)
            return []
        return [
            UserRef(id=str(u["Id"]), name=str(u.get("Name", "unknown")))
            for u in _unwrap(resp.json()) if u.get("Id")
        ]
    except Exception:
        log.exception("Jellyfin /Users unreachable")
        return []


# --- watch history -----------------------------------------------------------------
async def get_watch_history(user_ids: list[str], limit: int = 200) -> list[WatchedItem]:
    """Recent plays for the selected users, normalised to WatchedItem,
    deduped/aggregated by (title, media_type)."""
    s = get_settings()
    items: list[WatchedItem] = []
    if s.history_source == "tautulli":
        # Plex user ids — a Jellyfin fallback would never match, so none.
        for uid in user_ids:
            items.extend(_normalise_tautulli(
                await _tautulli_user_history(uid, limit)))
        return _aggregate(items)[:limit]
    if s.history_source == "jellystat" and s.jellystat_api_key:
        for uid in user_ids:
            rows = await _jellystat_user_history(uid, limit)
            items.extend(_normalise_jellystat(rows))
        if not items:
            log.warning("Jellystat returned no history — trying Jellyfin fallback")
    if not items:
        for uid in user_ids:
            items.extend(await _jellyfin_user_history(uid, limit))
    return _aggregate(items)[:limit]


async def _tautulli_user_history(user_id: str, limit: int) -> list[dict]:
    data = await _tautulli_data("get_history", user_id=user_id, length=limit,
                                order_column="date", order_dir="desc")
    rows = _unwrap(data)  # data is {"recordsTotal": n, ..., "data": [rows]}
    if rows:
        log.info("tautulli history for %s: %d rows", user_id, len(rows))
    else:
        log.warning("Tautulli returned no history for user %s", user_id)
    return rows


def _normalise_tautulli(rows: list[dict]) -> list[WatchedItem]:
    """Map Tautulli get_history rows to WatchedItem. Each row is one play;
    episodes fold into their series via grandparent_title."""
    items = []
    for row in rows:
        kind = str(row.get("media_type") or "").lower()
        if kind == "movie":
            title, media_type, year = row.get("title"), "movie", \
                _safe_int(row.get("year"))
        elif kind == "episode":
            # year on an episode row is the episode's air year, not the
            # series premiere — leave it unset for TV.
            title, media_type, year = \
                row.get("grandparent_title") or row.get("title"), "tv", None
        else:
            continue  # track / photo / clip / live TV
        if not title:
            continue
        items.append(WatchedItem(
            title=str(title),
            media_type=media_type,
            year=year,
            imdb_id=None,
            tmdb_id=None,
            play_count=1,
            last_played=_parse_epoch(row.get("date") or row.get("stopped")),
        ))
    return items


async def _jellystat_user_history(user_id: str, limit: int) -> list[dict]:
    """Try several known/likely Jellystat endpoint patterns until one answers."""
    s = get_settings()
    attempts: list[tuple[str, str, dict | None, dict | None]] = [
        ("GET", f"{s.jellystat_url}/api/getUserHistory",
         {"userid": user_id, "size": limit}, None),
        ("POST", f"{s.jellystat_url}/api/getUserHistory", None,
         {"userid": user_id, "size": limit}),
        ("GET", f"{s.jellystat_url}/api/getHistory",
         {"userid": user_id, "size": limit}, None),
        ("POST", f"{s.jellystat_url}/api/getHistory", None,
         {"userid": user_id, "size": limit, "page": 1}),
        ("GET", f"{s.jellystat_url}/stats/getUserActivity",
         {"userid": user_id}, None),
    ]
    for method, url, params, body in attempts:
        try:
            resp = await http.request(method, url, headers=_jellystat_headers(),
                                      params=params, json=body)
            if resp.status_code != 200:
                continue
            rows = _unwrap(resp.json())
            if rows:
                log.info("jellystat history for %s via %s %s (%d rows)",
                         user_id, method, url.rsplit("/", 1)[-1], len(rows))
                return rows
        except Exception as exc:
            log.debug("jellystat %s %s failed: %s", method, url, type(exc).__name__)
    log.warning("no Jellystat history endpoint answered for user %s", user_id)
    return []


def _normalise_jellystat(rows: list[dict]) -> list[WatchedItem]:
    """Map Jellystat activity rows to WatchedItem, folding episodes into
    their series."""
    items = []
    for row in rows:
        series = _first(row, "SeriesName", "seriesName", "series_name")
        name = _first(row, "NowPlayingItemName", "ItemName", "Name", "title",
                      "FullName")
        if series:
            title, media_type = str(series), "tv"
        elif name:
            title = str(name)
            item_type = str(_first(row, "ItemType", "Type", "MediaType") or "").lower()
            media_type = "tv" if item_type in ("episode", "series", "season") else "movie"
        else:
            continue
        items.append(WatchedItem(
            title=title,
            media_type=media_type,
            year=_safe_int(_first(row, "ProductionYear", "Year")),
            imdb_id=None,
            tmdb_id=_safe_int(_first(row, "TmdbId", "tmdb_id")),
            play_count=_safe_int(_first(row, "PlayCount", "Plays", "TotalPlays")) or 1,
            last_played=_parse_dt(_first(row, "ActivityDateInserted", "DateCreated",
                                         "LastPlayedDate", "date", "LastWatched")),
        ))
    return items


async def _jellyfin_user_history(user_id: str, limit: int) -> list[WatchedItem]:
    s = get_settings()
    try:
        resp = await http.request(
            "GET", f"{s.jellyfin_url}/Users/{user_id}/Items",
            headers=_jellyfin_headers(),
            params={
                "Recursive": "true", "IsPlayed": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "Genres,ProviderIds,ProductionYear,UserData",
                "SortBy": "DatePlayed", "SortOrder": "Descending",
                "Limit": limit,
            },
        )
        if resp.status_code != 200:
            log.error("Jellyfin history for %s returned %s", user_id, resp.status_code)
            return []
    except Exception:
        log.exception("Jellyfin history unreachable for %s", user_id)
        return []

    items = []
    for row in _unwrap(resp.json()):
        title = row.get("Name")
        if not title:
            continue
        provider_ids = row.get("ProviderIds") or {}
        user_data = row.get("UserData") or {}
        items.append(WatchedItem(
            title=str(title),
            media_type="tv" if row.get("Type") == "Series" else "movie",
            year=_safe_int(row.get("ProductionYear")),
            imdb_id=provider_ids.get("Imdb"),
            tmdb_id=_safe_int(provider_ids.get("Tmdb")),
            play_count=_safe_int(user_data.get("PlayCount")) or 1,
            last_played=_parse_dt(user_data.get("LastPlayedDate")),
        ))
    return items


def _aggregate(items: list[WatchedItem]) -> list[WatchedItem]:
    """Dedupe by (title, media_type): sum plays, keep newest date + best ids."""
    merged: dict[tuple[str, str], WatchedItem] = {}
    for item in items:
        key = (item.title.strip().lower(), item.media_type)
        existing = merged.get(key)
        if existing is None:
            merged[key] = item.model_copy()
            continue
        existing.play_count += item.play_count
        if item.last_played and (not existing.last_played
                                 or item.last_played > existing.last_played):
            existing.last_played = item.last_played
        existing.imdb_id = existing.imdb_id or item.imdb_id
        existing.tmdb_id = existing.tmdb_id or item.tmdb_id
        existing.year = existing.year or item.year
    out = list(merged.values())
    out.sort(key=lambda w: w.last_played or "", reverse=True)
    return out


# --- ownership -----------------------------------------------------------------
async def get_library_provider_ids() -> set[str]:
    """Owned-media key set, fetched once per scan.

    Jellyfin mode: provider ids — 'tt1234567' (IMDb) and 'tmdb:1396' (TMDb).
    Tautulli mode: title_key() entries from the Plex library sections.
    """
    s = get_settings()
    if s.history_source == "tautulli":
        return await _tautulli_library_keys()
    try:
        resp = await http.request(
            "GET", f"{s.jellyfin_url}/Items",
            headers=_jellyfin_headers(),
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series",
                "Fields": "ProviderIds",
            },
        )
        if resp.status_code != 200:
            log.error("Jellyfin library fetch returned %s", resp.status_code)
            return set()
    except Exception:
        log.exception("Jellyfin library unreachable — ownership flags disabled")
        return set()

    owned: set[str] = set()
    for row in _unwrap(resp.json()):
        provider_ids = row.get("ProviderIds") or {}
        for key, value in provider_ids.items():
            if not value:
                continue
            k = key.lower()
            if k == "imdb":
                owned.add(str(value))
            elif k == "tmdb":
                owned.add(f"tmdb:{value}")
    log.info("library ownership set: %d ids", len(owned))
    return owned


async def _tautulli_library_keys() -> set[str]:
    """Owned title keys from every Plex movie/show section via Tautulli."""
    owned: set[str] = set()
    for section in _unwrap(await _tautulli_data("get_libraries")):
        section_type = str(section.get("section_type") or "").lower()
        if section_type not in ("movie", "show"):
            continue
        media_type = "movie" if section_type == "movie" else "tv"
        data = await _tautulli_data("get_library_media_info",
                                    section_id=section.get("section_id"),
                                    length=10000)
        for row in _unwrap(data):
            title = row.get("title")
            if not title:
                continue
            owned.add(title_key(media_type, str(title),
                                _safe_int(row.get("year"))))
    log.info("plex library ownership set: %d keys", len(owned))
    return owned


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (ValueError, TypeError):
        return None
