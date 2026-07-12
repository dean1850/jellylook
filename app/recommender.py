"""Scan orchestration.

run_scan(user_ids) -> ScanRecord:
  1. Jellystat history (Jellyfin fallback) for the users
  2. recency- & play-count-weighted taste profile
  3. ONE LLM call → recs_per_scan suggestions
  4. enrich each via metadata.py (concurrent, semaphore-capped)
  5. flag is_in_library against the Jellyfin ownership set
  6. drop unresolved / duplicate-of-watched / duplicate suggestions, persist

Partial success beats total failure: if some titles fail enrichment, the rest
still land. Only an empty history or an unusable LLM answer fails the scan.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from . import db, history, llm, metadata
from .config import get_settings
from .models import Recommendation, ScanRecord, Suggestion, WatchedItem

log = logging.getLogger("jellylook.recommender")

PROFILE_SEEDS = 30  # how many weighted titles the LLM sees


class ScanError(RuntimeError):
    """A scan-level failure with a user-facing message."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_profile(watched: list[WatchedItem]) -> dict:
    """Recency- & play-count-weighted seeds. watched arrives newest-first."""
    total = len(watched)
    seeds = []
    for idx, item in enumerate(watched[:PROFILE_SEEDS * 2]):
        recency = 1.0 - (idx / max(total, 1)) * 0.7   # newest ≈ 1.0
        weight = round(recency * (1 + min(item.play_count, 10) * 0.2), 2)
        seeds.append({
            "title": item.title,
            "type": item.media_type,
            "year": item.year,
            "plays": item.play_count,
            "weight": weight,
        })
    seeds.sort(key=lambda s: s["weight"], reverse=True)
    return {"recently_watched": seeds[:PROFILE_SEEDS]}


async def run_scan(user_ids: list[str],
                   progress: dict | None = None) -> ScanRecord:
    """Run a full scan. `progress` (optional dict) is mutated for polling."""
    s = get_settings()
    started = time.monotonic()
    scan_id = uuid.uuid4().hex
    created_at = _now()

    def step(name: str, detail: str = "") -> None:
        if progress is not None:
            progress.update({"step": name, "detail": detail})
        log.info("scan %s: %s %s", scan_id[:8], name, detail)

    # 1. history
    step("history", "pulling watch history")
    watched = await history.get_watch_history(user_ids)
    if not watched:
        raise ScanError("No watch history found for the selected user(s). "
                        "Check the history source connection and API key.")
    step("history", f"{len(watched)} watched titles")

    # 2. profile + exclude set
    profile = build_profile(watched)
    watched_titles = {w.title.strip().lower() for w in watched}

    # 3. ONE LLM call
    n = _recs_per_scan(s.recs_per_scan)
    step("llm", f"asking {s.llm_provider}/{s.llm_model} for {n} titles")
    provider = llm.get_provider(s)
    suggestions = await provider.recommend(profile, n, watched_titles)
    step("llm", f"{len(suggestions)} suggestions returned")

    # Drop duplicates and anything matching a watched title.
    suggestions = _dedupe(suggestions, watched_titles)

    # Ownership set (once per scan) + enrichment run concurrently.
    step("enrich", f"resolving {len(suggestions)} titles via TMDb/OMDb")
    owned_task = asyncio.create_task(history.get_library_provider_ids())
    metas = await asyncio.gather(
        *(metadata.enrich(sug.title, sug.year, sug.media_type)
          for sug in suggestions),
        return_exceptions=True,
    )
    owned = await owned_task

    recs: list[Recommendation] = []
    seen_ids: set[str] = set()
    for sug, meta in zip(suggestions, metas):
        if isinstance(meta, Exception):
            log.warning("enrich failed for %s: %s", sug.title, meta)
            continue
        if meta is None or not meta.tmdb_id:
            log.info("dropping unresolved suggestion: %s", sug.title)
            continue
        # Post-enrichment dedupe (two suggestions can resolve to one title).
        id_key = f"{meta.media_type}:{meta.tmdb_id}"
        if id_key in seen_ids or (meta.title.strip().lower() in watched_titles):
            continue
        seen_ids.add(id_key)
        in_library = (
            (meta.imdb_id and meta.imdb_id in owned)
            or (meta.tmdb_id and f"tmdb:{meta.tmdb_id}" in owned)
        )
        recs.append(Recommendation(
            id=uuid.uuid4().hex,
            scan_id=scan_id,
            created_at=created_at,
            media_type=meta.media_type,
            title=meta.title,
            year=meta.year,
            tmdb_id=meta.tmdb_id,
            imdb_id=meta.imdb_id,
            imdb_rating=meta.imdb_rating,
            tmdb_rating=meta.tmdb_rating,
            poster_url=meta.poster_url,
            backdrop_url=meta.backdrop_url,
            overview=meta.overview,
            genres=meta.genres,
            reason=sug.reason,
            match_score=sug.match_score,
            because_of=sug.because_of,
            is_in_library=bool(in_library),
        ))

    if not recs:
        raise ScanError("The scan produced no usable recommendations. "
                        "Check TMDB_API_KEY and the LLM provider settings.")

    duration_ms = int((time.monotonic() - started) * 1000)
    status = "ok" if len(recs) >= n * 0.8 else "partial"
    scan = ScanRecord(
        id=scan_id, created_at=created_at, source_users=",".join(user_ids),
        provider=s.llm_provider, model=s.llm_model,
        item_count=len(recs), duration_ms=duration_ms, status=status,
    )
    db.insert_scan(scan)
    db.insert_recommendations(scan_id, recs)
    step("done", f"{len(recs)} recommendations in {duration_ms} ms ({status})")
    return scan


def _dedupe(suggestions: list[Suggestion],
            watched_titles: set[str]) -> list[Suggestion]:
    seen: set[tuple[str, str]] = set()
    out = []
    for sug in suggestions:
        key = (sug.title.strip().lower(), sug.media_type)
        if key in seen or key[0] in watched_titles:
            continue
        seen.add(key)
        out.append(sug)
    return out


def _recs_per_scan(env_default: int) -> int:
    """Runtime pref (Settings menu) wins over the .env default."""
    try:
        value = int(db.settings_get_all().get("recs_per_scan", env_default))
        return max(5, min(100, value))
    except (ValueError, TypeError):
        return env_default
