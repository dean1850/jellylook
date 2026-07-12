"""Watch-history adapters. Jellystat is PRIMARY (what the user is actually
watching); Jellyfin is the fallback source and the ownership/id helper.

Jellystat's API is thin, under-documented and mid-rebuild, so this module is
deliberately defensive: several endpoint patterns are tried per call, every
response shape is unwrapped (list / {results} / {data} / ...) and every field
lookup tolerates absence. Failures degrade to the Jellyfin fallback rather
than crashing a scan.
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


# --- users -----------------------------------------------------------------------
async def get_users() -> list[UserRef]:
    """User list from Jellystat if available, else Jellyfin GET /Users."""
    s = get_settings()
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
    """Owned ids from the Jellyfin library, as a flat set:
    'tt1234567' (IMDb) and 'tmdb:1396' (TMDb). Fetch once per scan."""
    s = get_settings()
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


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (ValueError, TypeError):
        return None
