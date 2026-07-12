"""jellylook FastAPI entrypoint.

Scans run as a background task; POST /api/scan returns immediately with a
scan_id and the UI polls GET /api/scan/status. Only one scan runs at a time.
Config problems raise RuntimeError at startup (fail-fast).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from . import db, history, http, recommender, seerr
from .config import get_settings
from .recommender import ScanError

logging.basicConfig(
    level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("jellylook")

STATIC_DIR = Path(__file__).parent / "static"

# In-memory scan state: {"state": idle|running|done|failed, ...}
_scan_state: dict = {"state": "idle"}
_scan_lock = asyncio.Lock()


async def _purge_loop(retention_days: int) -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            log.info("daily purge: %s", db.purge_old(retention_days))
        except Exception:
            log.exception("daily purge failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    problems = s.validate_runtime()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        raise RuntimeError(
            "jellylook cannot start — fix these .env problems: "
            + "; ".join(problems)
        )
    db.init_db()
    await http.startup()
    try:
        log.info("startup purge: %s", db.purge_old(s.retention_days))
    except Exception:
        log.exception("startup purge failed")
    task = asyncio.create_task(_purge_loop(s.retention_days))
    try:
        yield
    finally:
        task.cancel()
        await http.shutdown()


app = FastAPI(title="jellylook", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    s = get_settings()
    return {
        "provider": s.llm_provider,
        "model": s.llm_model,
        "history_source": s.history_source,
        "omdb_today": db.omdb_today(),
        "omdb_limit": 1000,
        "seerr_reachable": await seerr.ping(),
        "last_scans": db.list_scans(5),
    }


@app.get("/api/users")
async def users():
    return await history.get_users()


@app.get("/api/settings")
async def settings_get():
    return db.settings_get_all()


@app.post("/api/settings")
async def settings_post(payload: dict):
    allowed = set(db.DEFAULT_SETTINGS)
    for key, value in payload.items():
        if key in allowed:
            db.settings_set(str(key), str(value))
    return db.settings_get_all()


# --- scan -----------------------------------------------------------------------
async def _run_scan_task(user_ids: list[str]) -> None:
    try:
        scan = await recommender.run_scan(user_ids, progress=_scan_state)
        _scan_state.update({"state": "done", "scan_id": scan.id,
                            "item_count": scan.item_count,
                            "status": scan.status})
    except ScanError as exc:
        log.warning("scan failed: %s", exc)
        _scan_state.update({"state": "failed", "error": str(exc)})
    except Exception as exc:
        log.exception("scan crashed")
        _scan_state.update({"state": "failed",
                            "error": f"Unexpected error: {exc}"})


@app.post("/api/scan")
async def scan(payload: dict):
    user_ids = payload.get("user_ids") or []
    if not user_ids or not isinstance(user_ids, list):
        raise HTTPException(400, "user_ids (non-empty list) is required")
    async with _scan_lock:
        # Check-and-set inside the lock so two simultaneous POSTs can't both
        # pass the "running" test and start overlapping scans.
        if _scan_state.get("state") == "running":
            raise HTTPException(409, "A scan is already running")
        _scan_state.clear()
        _scan_state.update({"state": "running", "step": "starting"})
        asyncio.create_task(_run_scan_task([str(u) for u in user_ids]))
    return {"started": True}


@app.get("/api/scan/status")
async def scan_status():
    return dict(_scan_state)


# --- recommendations --------------------------------------------------------------
@app.get("/api/recommendations")
async def recommendations(
    scan: str = "latest",
    type: str = Query("all", pattern="^(all|movie|tv|film)$"),
    sort: str = Query("match", pattern="^(match|imdb|year)$"),
    page: int = Query(1, ge=1),
    per_page: int | None = Query(None, ge=1, le=100),
):
    media_type = "movie" if type == "film" else type
    return db.get_recommendations(
        scan=scan, media_type=media_type, sort=sort, page=page,
        per_page=per_page or get_settings().per_page,
    )


# --- seerr ------------------------------------------------------------------------
@app.get("/api/seerr/status")
async def seerr_status(type: str, tmdb: int):
    state = await seerr.get_status(type, tmdb)
    if state:
        db.update_seerr_status("tv" if type == "tv" else "movie", tmdb, state)
    return {"status": state}


@app.get("/api/seerr/seasons")
async def seerr_seasons(tmdb: int):
    return {"seasons": await seerr.get_seasons(tmdb)}


@app.post("/api/seerr/request")
async def seerr_request(payload: dict):
    media_type = payload.get("type")
    tmdb = payload.get("tmdb")
    if media_type not in ("movie", "tv") or not tmdb:
        raise HTTPException(400, "type (movie|tv) and tmdb are required")
    seasons = payload.get("seasons")
    if media_type == "tv" and not seasons:
        raise HTTPException(400, "seasons required for tv requests")
    result = await seerr.request(media_type, int(tmdb), seasons)
    if result.get("ok"):
        db.update_seerr_status(media_type, int(tmdb), result.get("status"))
        return result
    raise HTTPException(502, result.get("error", "Seerr request failed"))


# Static UI last so API routes take precedence.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
