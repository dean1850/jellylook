"""Scan orchestration.

run_scan(user_ids) -> ScanRecord:
  1. watch history for the users (Jellystat w/ Jellyfin fallback, or Tautulli)
  2. recency- & play-count-weighted taste profile
  3. ONE LLM call → recs_per_scan suggestions
  4. enrich each via metadata.py (concurrent, semaphore-capped)
  5. flag is_in_library against the library ownership set (Jellyfin provider
     ids, or Plex title keys via Tautulli)
  6. drop unresolved / duplicate-of-watched / duplicate suggestions, persist

Partial success beats total failure: if some titles fail enrichment, the rest
still land. Only an empty history or an unusable LLM answer fails the scan.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from . import db, history, llm, metadata, seerr
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

    # 2. profile + exclusion signals
    profile = build_profile(watched)
    watched_titles = {w.title.strip().lower() for w in watched}

    # Feedback is per media-server user; a scan for several people unions their
    # lists, so one viewer's rejection removes the title for the group.
    hidden_keys = db.hidden_ids(user_ids)
    disliked = db.feedback_titles(user_ids, "hidden", llm.MAX_TASTE_EXAMPLES)
    liked = db.feedback_titles(user_ids, "saved", llm.MAX_TASTE_EXAMPLES)
    exclude = llm.build_exclude_list(
        hidden=disliked,
        watched=[f"{w.title} ({w.year})" if w.year else w.title for w in watched],
        suggested=db.recent_suggested_titles(s.exclude_budget),
        budget=s.exclude_budget,
    )
    if liked or disliked:
        step("history", f"{len(liked)} liked / {len(disliked)} rejected on file")

    # 3. ONE LLM call
    n = _recs_per_scan(s.recs_per_scan)
    step("llm", f"asking {s.llm_provider}/{s.llm_model} for {n} titles")
    provider = llm.get_provider(s)
    try:
        suggestions = await provider.recommend(profile, n, exclude, liked, disliked)
    except llm.NoSuggestions as exc:
        # A provider that answers with nothing is a configuration or prompt-size
        # problem, not a crash — say so rather than showing a stack-trace string.
        raise ScanError(str(exc)) from exc
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
    suppressed = 0
    for sug, meta in zip(suggestions, metas):
        if isinstance(meta, Exception):
            log.warning("enrich failed for %s: %s", sug.title, meta)
            continue
        if meta is None or not meta.tmdb_id:
            log.info("dropping unresolved suggestion: %s", sug.title)
            continue
        # Post-enrichment dedupe (two suggestions can resolve to one title).
        id_key = f"{meta.media_type}:{meta.tmdb_id}"
        # The prompt's exclude list is advisory — models return hidden titles
        # anyway. This is the enforcement point, and the only one that can be
        # correct: it runs after TMDb has resolved the title to an identity,
        # so it cannot be fooled by punctuation or a same-named remake.
        if id_key in hidden_keys:
            suppressed += 1
            continue
        if id_key in seen_ids or (meta.title.strip().lower() in watched_titles):
            continue
        seen_ids.add(id_key)
        in_library = (
            (meta.imdb_id and meta.imdb_id in owned)
            or (meta.tmdb_id and f"tmdb:{meta.tmdb_id}" in owned)
            # Plex/Tautulli ownership matches on type+title(+year) — a
            # missing year on either side still matches the bare-title key.
            or history.title_key(meta.media_type, meta.title, meta.year) in owned
            or history.title_key(meta.media_type, meta.title, None) in owned
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
            genre_ids=meta.genre_ids,
            runtime=meta.runtime,
            original_language=meta.original_language,
            popularity=meta.popularity,
            trailer_key=meta.trailer_key,
            providers=meta.providers,
        ))

    if suppressed:
        # Worth seeing: a consistently high number means the taste blocks are
        # not landing and the model keeps re-proposing rejected titles.
        log.info("suppressed %d suggestion(s) previously hidden by these users",
                 suppressed)

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

    # Auto-request runs last, on rows that are already persisted, so a Seerr
    # outage costs the scan nothing. It is still user-initiated: this whole
    # function only runs because someone pressed Scan.
    await auto_request(recs, step=step, progress=progress)

    step("done", f"{len(recs)} recommendations in {duration_ms} ms ({status})")
    return scan


# --- auto-request ----------------------------------------------------------------
async def auto_request(recs: list[Recommendation], threshold: int | None = None,
                       tv_mode: str | None = None, step=None,
                       progress: dict | None = None) -> list[Recommendation]:
    """File Seerr requests for every rec at or above the match threshold.

    Off unless the user sets a threshold in Settings, and skipped entirely when
    Seerr is unreachable. Titles already owned, already known to Seerr, or
    never resolved to a TMDb id are left alone, and a per-title failure is
    logged rather than raised — the card simply keeps its normal Add button.

    Returns the recs that were sent, mutated to carry the new state so the same
    objects can be reported on without a re-read. `progress` (the scan's status
    dict) is filled in for the UI, including when nothing could be sent: a
    feature the user switched on has to say why it stayed quiet.
    """
    if threshold is None:
        threshold = _auto_threshold()

    def report(sent: list[Recommendation], note: str = "") -> list[Recommendation]:
        if progress is not None:
            progress["auto_threshold"] = threshold
            progress["auto_requested"] = len(sent)
            progress["auto_sent"] = [rec.title for rec in sent]
            if note:
                progress["auto_note"] = note
        return sent

    if threshold <= 0:
        return report([])
    # Highest match first: if Seerr starts refusing part-way through, the
    # titles that got in are the ones the threshold was aimed at.
    candidates = sorted(
        (rec for rec in recs if rec.tmdb_id and not rec.is_in_library
         and (rec.match_score or 0) >= threshold),
        key=lambda rec: rec.match_score or 0, reverse=True,
    )
    if not candidates:
        return report([])
    if not await seerr.ping():
        log.warning("auto-request skipped for %d title(s): Seerr is unreachable",
                    len(candidates))
        return report([], "Seerr was unreachable, so nothing was auto-sent.")
    if tv_mode is None:
        tv_mode = db.settings_get_all().get("tv_request_mode", "ask")

    sent: list[Recommendation] = []
    failed = 0
    for rec in candidates:
        if step:
            step("seerr", f"auto-requesting {rec.title} ({rec.match_score}%)")
        try:
            existing = await seerr.get_status(rec.media_type, rec.tmdb_id)
            if existing:
                # Already requested by someone, or already available. Record
                # what Seerr says and move on — this is not our request.
                rec.seerr_status = existing
                db.update_seerr_status(rec.media_type, rec.tmdb_id, existing)
                continue
            seasons = None
            if rec.media_type == "tv":
                seasons = await _auto_seasons(rec.tmdb_id, tv_mode)
                if not seasons:
                    log.warning("auto-request skipped for %s: Seerr returned no "
                                "seasons", rec.title)
                    continue
            result = await seerr.request(rec.media_type, rec.tmdb_id, seasons)
        except Exception:
            log.exception("auto-request failed for %s", rec.title)
            failed += 1
            continue
        if not result.get("ok"):
            log.warning("auto-request rejected for %s: %s", rec.title,
                        result.get("error"))
            failed += 1
            continue
        status = result.get("status") or "requested"
        rec.auto_requested = True
        rec.seerr_status = status
        db.mark_auto_requested(rec.media_type, rec.tmdb_id, status)
        sent.append(rec)

    if sent:
        log.info("auto-requested %d title(s) at or above %d%%: %s",
                 len(sent), threshold, ", ".join(rec.title for rec in sent))
    note = ""
    if failed:
        note = (f"Seerr refused {failed} auto-request"
                f"{'' if failed == 1 else 's'} — check the container logs.")
    return report(sent, note)


async def _auto_seasons(tmdb_id: int, tv_mode: str) -> list[int]:
    """Which seasons an unattended TV request should ask for.

    'ask' cannot be honoured with nobody at the keyboard, so it lands on the
    whole show — the same thing the picker's "All seasons" tick does.
    """
    if tv_mode == "first":
        return [1]
    return [season["seasonNumber"] for season in await seerr.get_seasons(tmdb_id)
            if season.get("seasonNumber")]


def _auto_threshold() -> int:
    """The saved threshold, or 0 (off) when unset or unusable."""
    try:
        return db.clamp_auto_threshold(
            db.settings_get_all().get("auto_request_threshold", 0))
    except (ValueError, TypeError):
        return 0


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
    """A saved Settings-panel value wins; otherwise RECS_PER_SCAN from .env."""
    try:
        return db.clamp_recs_per_scan(
            db.settings_get_all().get("recs_per_scan", env_default))
    except (ValueError, TypeError):
        return env_default
