"""TMDb + OMDb enrichment with metadata_cache.

enrich(title, year, media_type) -> MediaMeta | None
  1. TMDb search by title(+year) → best hit → details w/ external_ids
  2. OMDb by imdb_id → IMDB rating (+ plot/genre/poster fallback)
Every lookup is cached (TTL = RETENTION_DAYS). OMDb calls that actually hit
the network increment omdb_usage; cache hits do not.
"""
import logging
from typing import Any

from . import db, http
from .config import get_settings
from .models import MediaMeta

log = logging.getLogger("jellylook.metadata")

TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "https://www.omdbapi.com/"
IMG_POSTER = "https://image.tmdb.org/t/p/w500"
IMG_BACKDROP = "https://image.tmdb.org/t/p/w1280"


async def enrich(title: str, year: int | None, media_type: str) -> MediaMeta | None:
    """Resolve a title to metadata + ratings + artwork. None if unresolvable."""
    ttl = get_settings().retention_days
    cache_key = f"meta:{media_type}:{title.strip().lower()}:{year or ''}"
    cached = db.cache_get(cache_key, ttl)
    if cached is not None:
        return MediaMeta(**cached) if cached.get("title") else None

    tmdb = await _tmdb_lookup(title, year, media_type)
    if tmdb is None:
        # Negative-cache so a bad title doesn't re-query every scan.
        db.cache_set(cache_key, {})
        return None

    omdb = await _omdb_lookup(tmdb.get("imdb_id"), ttl)

    meta = MediaMeta(
        title=tmdb.get("title") or title,
        media_type=media_type,
        year=tmdb.get("year") or year,
        tmdb_id=tmdb.get("tmdb_id"),
        imdb_id=tmdb.get("imdb_id"),
        imdb_rating=omdb.get("imdb_rating"),
        tmdb_rating=tmdb.get("tmdb_rating"),
        poster_url=tmdb.get("poster_url") or omdb.get("poster_url"),
        backdrop_url=tmdb.get("backdrop_url"),
        overview=tmdb.get("overview") or omdb.get("plot"),
        genres=tmdb.get("genres") or omdb.get("genres"),
    )
    db.cache_set(cache_key, meta.model_dump())
    return meta


# --- TMDb -----------------------------------------------------------------------
async def _tmdb_lookup(title: str, year: int | None, media_type: str) -> dict | None:
    s = get_settings()
    kind = "tv" if media_type == "tv" else "movie"
    params: dict[str, Any] = {"api_key": s.tmdb_api_key, "query": title}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = year
    try:
        resp = await http.request("GET", f"{TMDB_BASE}/search/{kind}", params=params)
        results = (resp.json() or {}).get("results") or [] if resp.status_code == 200 else []
        if not results and year:
            # Retry without the year — LLM years are sometimes off by one.
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            resp = await http.request("GET", f"{TMDB_BASE}/search/{kind}", params=params)
            results = (resp.json() or {}).get("results") or [] if resp.status_code == 200 else []
        if not results:
            log.info("TMDb: no result for %s (%s)", title, kind)
            return None
        best = results[0]
        detail_resp = await http.request(
            "GET", f"{TMDB_BASE}/{kind}/{best['id']}",
            params={"api_key": s.tmdb_api_key, "append_to_response": "external_ids"},
        )
        detail = detail_resp.json() if detail_resp.status_code == 200 else {}
    except Exception:
        log.exception("TMDb lookup failed for %s", title)
        return None

    date = detail.get("release_date") or detail.get("first_air_date") or ""
    genres = ", ".join(g.get("name", "") for g in detail.get("genres", []) if g.get("name"))
    poster = detail.get("poster_path") or best.get("poster_path")
    backdrop = detail.get("backdrop_path") or best.get("backdrop_path")
    return {
        "title": detail.get("title") or detail.get("name") or title,
        "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else year,
        "tmdb_id": detail.get("id") or best.get("id"),
        "imdb_id": (detail.get("external_ids") or {}).get("imdb_id"),
        "tmdb_rating": _round1(detail.get("vote_average")),
        "poster_url": f"{IMG_POSTER}{poster}" if poster else None,
        "backdrop_url": f"{IMG_BACKDROP}{backdrop}" if backdrop else None,
        "overview": detail.get("overview"),
        "genres": genres or None,
    }


# --- OMDb -----------------------------------------------------------------------
async def _omdb_lookup(imdb_id: str | None, ttl_days: int) -> dict:
    if not imdb_id:
        return {}
    cache_key = f"omdb:{imdb_id}"
    cached = db.cache_get(cache_key, ttl_days)
    if cached is not None:
        return cached
    s = get_settings()
    try:
        resp = await http.request(
            "GET", OMDB_BASE, params={"apikey": s.omdb_api_key, "i": imdb_id}
        )
        db.omdb_increment()  # every real network call counts, success or not
        if resp.status_code != 200:
            log.warning("OMDb returned %s for %s", resp.status_code, imdb_id)
            return {}
        data = resp.json() or {}
    except Exception:
        log.exception("OMDb lookup failed for %s", imdb_id)
        return {}

    if str(data.get("Response", "")).lower() == "false":
        result: dict = {}
    else:
        result = {
            "imdb_rating": _safe_float(data.get("imdbRating")),
            "poster_url": data.get("Poster") if data.get("Poster") not in (None, "N/A") else None,
            "plot": data.get("Plot") if data.get("Plot") not in (None, "N/A") else None,
            "genres": data.get("Genre") if data.get("Genre") not in (None, "N/A") else None,
        }
    db.cache_set(cache_key, result)
    return result


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", "N/A") else None
    except (ValueError, TypeError):
        return None


def _round1(value: Any) -> float | None:
    f = _safe_float(value)
    return round(f, 1) if f is not None else None
