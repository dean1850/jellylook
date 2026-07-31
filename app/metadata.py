"""TMDb + OMDb enrichment with metadata_cache.

enrich(title, year, media_type) -> MediaMeta | None
  1. TMDb search by title(+year) → best hit → details with external_ids,
     videos and watch/providers appended
  2. OMDb by imdb_id → IMDB rating (+ plot/genre/poster fallback)
Every lookup is cached (TTL = RETENTION_DAYS). OMDb calls that actually hit
the network increment omdb_usage; cache hits do not.

TMDb maps append_to_response keys 1:1 into the detail response, so trailers
and streaming availability arrive in the request that was already being made
— enrichment stays at 1 search + 1 detail + 1 OMDb per title.
"""
import json
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
APPEND = "external_ids,videos,watch/providers"

# Streaming categories kept, in the order they are shown. "flatrate" is the
# one the streaming filter keys on — it means included with a subscription.
PROVIDER_KINDS = ("flatrate", "free", "ads", "rent", "buy")


async def enrich(title: str, year: int | None, media_type: str) -> MediaMeta | None:
    """Resolve a title to metadata + ratings + artwork. None if unresolvable."""
    s = get_settings()
    ttl = s.retention_days
    # meta2 + region: the payload gained fields and became region-dependent, so
    # entries cached by an older build would silently serve trailer/provider
    # nulls forever. A new prefix retires them in one step.
    cache_key = f"meta2:{s.region}:{media_type}:{title.strip().lower()}:{year or ''}"
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
        genre_ids=tmdb.get("genre_ids"),
        runtime=tmdb.get("runtime"),
        original_language=tmdb.get("original_language"),
        popularity=tmdb.get("popularity"),
        trailer_key=tmdb.get("trailer_key"),
        providers=tmdb.get("providers"),
    )
    db.cache_set(cache_key, meta.model_dump())
    return meta


# --- append_to_response extraction ------------------------------------------------
def _pick_trailer(videos: dict | None) -> str | None:
    """Best YouTube key: official trailer → any trailer → any teaser."""
    results = (videos or {}).get("results") or []
    youtube = [v for v in results
               if isinstance(v, dict) and v.get("site") == "YouTube" and v.get("key")]
    for predicate in (
        lambda v: v.get("type") == "Trailer" and v.get("official"),
        lambda v: v.get("type") == "Trailer",
        lambda v: v.get("type") == "Teaser",
    ):
        for video in youtube:
            if predicate(video):
                return str(video["key"])
    return None


def _pick_providers(block: dict | None, region: str) -> str | None:
    """Region slice of TMDb watch/providers as compact JSON, or None.

    Stored region-filtered to keep rows small; empty categories are dropped so
    the streaming filter can test for a "flatrate" key directly. The data is
    licensed from JustWatch — anything rendering it must credit them.
    """
    regions = (block or {}).get("results") or {}
    entry = regions.get(region) or {}
    out: dict[str, Any] = {}
    for kind in PROVIDER_KINDS:
        names = [
            {"name": p.get("provider_name"), "logo": p.get("logo_path")}
            for p in (entry.get(kind) or [])
            if isinstance(p, dict) and p.get("provider_name")
        ]
        if names:
            out[kind] = names
    if not out:
        return None
    if entry.get("link"):
        out["link"] = entry["link"]
    return json.dumps(out, separators=(",", ":"))


async def _tv_providers_fallback(tmdb_id: int, region: str) -> str | None:
    """TMDb's appended watch/providers block is unreliable for some TV titles.

    Only called when the appended block came back empty for a series, and the
    result is cached, so this stays a rare extra request rather than a second
    call per title.
    """
    s = get_settings()
    cache_key = f"wp:tv:{region}:{tmdb_id}"
    cached = db.cache_get(cache_key, s.retention_days)
    if cached is not None:
        return cached.get("providers")
    try:
        resp = await http.request(
            "GET", f"{TMDB_BASE}/tv/{tmdb_id}/watch/providers",
            params={"api_key": s.tmdb_api_key},
        )
        block = resp.json() if resp.status_code == 200 else None
    except Exception:
        log.debug("watch/providers fallback failed for tv/%s", tmdb_id)
        return None
    providers = _pick_providers(block, region)
    db.cache_set(cache_key, {"providers": providers})
    return providers


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
            params={"api_key": s.tmdb_api_key, "append_to_response": APPEND},
        )
        detail = detail_resp.json() if detail_resp.status_code == 200 else {}
    except Exception:
        log.exception("TMDb lookup failed for %s", title)
        return None

    date = detail.get("release_date") or detail.get("first_air_date") or ""
    genre_objs = [g for g in detail.get("genres", []) if isinstance(g, dict)]
    genres = ", ".join(g.get("name", "") for g in genre_objs if g.get("name"))
    genre_ids = db.genre_key([g["id"] for g in genre_objs if g.get("id")])
    poster = detail.get("poster_path") or best.get("poster_path")
    backdrop = detail.get("backdrop_path") or best.get("backdrop_path")
    tmdb_id = detail.get("id") or best.get("id")

    # Movies carry a single runtime; series report per-episode lengths.
    runtime = _safe_int(detail.get("runtime"))
    if runtime is None:
        episode_runtimes = [
            _safe_int(v) for v in (detail.get("episode_run_time") or [])
        ]
        runtime = next((v for v in episode_runtimes if v), None)

    providers = _pick_providers(detail.get("watch/providers"), s.region)
    if providers is None and kind == "tv" and tmdb_id:
        providers = await _tv_providers_fallback(int(tmdb_id), s.region)

    return {
        "title": detail.get("title") or detail.get("name") or title,
        "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else year,
        "tmdb_id": tmdb_id,
        "imdb_id": (detail.get("external_ids") or {}).get("imdb_id"),
        "tmdb_rating": _round1(detail.get("vote_average")),
        "poster_url": f"{IMG_POSTER}{poster}" if poster else None,
        "backdrop_url": f"{IMG_BACKDROP}{backdrop}" if backdrop else None,
        "overview": detail.get("overview"),
        "genres": genres or None,
        "genre_ids": genre_ids,
        "runtime": runtime,
        "original_language": detail.get("original_language"),
        "popularity": _round1(detail.get("popularity")),
        "trailer_key": _pick_trailer(detail.get("videos")),
        "providers": providers,
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


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", "N/A") else None
    except (ValueError, TypeError):
        return None


def _round1(value: Any) -> float | None:
    f = _safe_float(value)
    return round(f, 1) if f is not None else None
