"""Seerr (Overseerr/Jellyseerr) client.

If Seerr is unreachable every call degrades to a harmless value; the UI
disables the button instead of crashing.
"""
import logging

from . import http
from .config import get_settings

log = logging.getLogger("jellylook.seerr")

# mediaInfo.status → label. 1 unknown, 2 pending, 3 processing, 4 partial, 5 available
STATUS_MAP = {1: "requested", 2: "pending", 3: "pending", 4: "available", 5: "available"}


def _headers() -> dict[str, str]:
    return {"X-Api-Key": get_settings().seerr_api_key}


async def ping() -> bool:
    """True if the Seerr instance answers."""
    s = get_settings()
    try:
        resp = await http.request("GET", f"{s.seerr_url}/api/v1/status",
                                  headers=_headers(), retries=0)
        return resp.status_code == 200
    except Exception:
        return False


async def get_status(media_type: str, tmdb_id: int) -> str | None:
    """None (not added) | 'requested' | 'pending' | 'available'.
    Also None when Seerr is unreachable (UI treats identically to not-added
    but /api/status exposes reachability)."""
    s = get_settings()
    kind = "tv" if media_type == "tv" else "movie"
    try:
        resp = await http.request("GET", f"{s.seerr_url}/api/v1/{kind}/{tmdb_id}",
                                  headers=_headers())
        if resp.status_code != 200:
            return None
        media_info = (resp.json() or {}).get("mediaInfo")
        if not media_info:
            return None
        return STATUS_MAP.get(media_info.get("status"))
    except Exception:
        log.warning("seerr status check failed for %s/%s", kind, tmdb_id)
        return None


async def get_seasons(tmdb_id: int) -> list[dict]:
    """Season list for the TV picker. Empty list if unreachable."""
    s = get_settings()
    try:
        resp = await http.request("GET", f"{s.seerr_url}/api/v1/tv/{tmdb_id}",
                                  headers=_headers())
        if resp.status_code != 200:
            return []
        seasons = (resp.json() or {}).get("seasons") or []
        return [
            {
                "seasonNumber": season.get("seasonNumber"),
                "episodeCount": season.get("episodeCount"),
                "name": season.get("name"),
            }
            for season in seasons
            if isinstance(season, dict) and season.get("seasonNumber", 0) > 0
        ]
    except Exception:
        log.warning("seerr seasons fetch failed for tv/%s", tmdb_id)
        return []


async def request(media_type: str, tmdb_id: int,
                  seasons: list[int] | None = None) -> dict:
    """Create a Seerr request. Returns {ok, status?, error?}."""
    s = get_settings()
    kind = "tv" if media_type == "tv" else "movie"
    body: dict = {"mediaType": kind, "mediaId": tmdb_id}
    if kind == "tv":
        body["seasons"] = seasons or []
    try:
        resp = await http.request("POST", f"{s.seerr_url}/api/v1/request",
                                  headers=_headers(), json=body)
    except Exception:
        log.exception("seerr request failed for %s/%s", kind, tmdb_id)
        return {"ok": False, "error": "Seerr is unreachable"}
    if resp.status_code in (200, 201):
        return {"ok": True, "status": "requested"}
    if resp.status_code == 409:
        return {"ok": True, "status": "requested",
                "note": "already requested in Seerr"}
    detail = ""
    try:
        detail = (resp.json() or {}).get("message", "")
    except Exception:
        pass
    log.warning("seerr request %s/%s → %s %s", kind, tmdb_id,
                resp.status_code, detail)
    return {"ok": False, "error": detail or f"Seerr returned {resp.status_code}"}
