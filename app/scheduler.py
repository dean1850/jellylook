"""Unattended scans on a fixed interval.

Off by default. When Settings → Automatic scans is set to 5h/1d/1w/1m, a loop
started in main.py's lifespan wakes every few minutes and asks one question:
is a scan overdue? Everything else falls out of that.

Due-ness is *derived*, never counted down, which is what makes the schedule
survive restarts: the anchor is the newest scan in the database, so the answer
is the same whether the container has been up for a month or ninety seconds.
It also means pressing Scan yourself resets the clock — you asked for fresh
recommendations, so the scheduler has nothing to add for another interval.

Guards, in order: the schedule must be on, the scan must be due, there must be
default user(s) to scan for, and OMDb must have a scan's worth of quota left.
Failing a guard is never an error — it logs once and waits for the next tick.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import db, recommender

log = logging.getLogger("jellylook.scheduler")

# How often the loop asks whether a scan is due. Five minutes is fine grained
# next to a five-hour minimum interval and costs one SELECT.
CHECK_INTERVAL_SECONDS = 300
# A scan that came due while the container was down runs after this — long
# enough that startup, the purge and any upstream containers have settled.
STARTUP_GRACE_SECONDS = 120
# A failed attempt retries this soon rather than a full interval later. On a
# monthly schedule the alternative is one transient LLM error costing a month.
RETRY_AFTER_SECONDS = 3600

# Runner outcomes. "busy" is the only one that does not move the clock.
BUSY = "busy"
OK = "ok"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    """Read a stored ISO timestamp, tolerating junk and naive values."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # Rows written before timezone-aware timestamps, or by another tool, would
    # otherwise raise on every comparison below and wedge the schedule.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def interval_seconds() -> int:
    """The configured interval in seconds; 0 when the schedule is off."""
    return db.autoscan_seconds(db.settings_get_all().get("autoscan_interval"))


def configured_users() -> list[str]:
    """Who an unattended scan runs for — the Settings panel's default user(s).

    There is no session behind a scheduled scan, so this is the only honest
    source. An empty list disables the schedule rather than guessing.
    """
    raw = db.settings_get_all().get("default_user_ids", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _anchor() -> tuple[datetime | None, bool]:
    """When the clock last reset, and whether it reset on a failed attempt.

    The newest scan row and the last unattended attempt are both resets; the
    later one wins. They differ only when an attempt produced no scan — a
    failure — and that is exactly the case the retry backoff exists for.
    """
    last_scan = _parse(db.last_scan_at())
    state = db.autoscan_state()
    last_attempt = _parse(state.get("autoscan_last_attempt_at"))
    if last_attempt and (last_scan is None or last_attempt >= last_scan):
        # "running" means the process died mid-scan; that is a failure too.
        return last_attempt, state.get("autoscan_last_result") != OK
    return last_scan, False


def next_due_at(now: datetime | None = None) -> datetime | None:
    """When the next unattended scan may run, or None when the schedule is off.

    A time in the past means overdue. A schedule switched on before anything
    has ever been scanned is due immediately — the user just asked for
    automatic recommendations and has none.
    """
    interval = interval_seconds()
    if not interval:
        return None
    now = now or _utcnow()
    anchor, failed = _anchor()
    if anchor is None:
        return now
    delay = min(interval, RETRY_AFTER_SECONDS) if failed else interval
    return anchor + timedelta(seconds=delay)


def _quota_headroom() -> tuple[bool, int, int]:
    """Whether OMDb has room for a whole scan. Returns (ok, used, needed).

    A scheduled scan yields to the quota so the one you press yourself still
    has ratings to work with. Cached titles cost nothing, so this is a
    worst-case reservation and usually pessimistic — which is the right way to
    be wrong here.
    """
    from .config import get_settings

    needed = recommender.recs_per_scan(get_settings().recs_per_scan)
    used = db.omdb_today()
    return used + needed <= db.OMDB_DAILY_LIMIT, used, needed


# Guard outcomes are logged only when they change, so a schedule that is off —
# or waiting, which is most of the time — does not fill the log with one line
# every five minutes.
_last_outcome: str | None = None


def _log_once(outcome: str, message: str) -> None:
    global _last_outcome
    if outcome != _last_outcome:
        log.info("autoscan: %s", message)
        _last_outcome = outcome


async def tick(run_scan, now: datetime | None = None) -> str:
    """Run one due-check, starting a scan if everything is in order.

    `run_scan(user_ids)` is injected by main.py rather than called directly,
    so a scheduled scan goes through the same lock and the same status dict as
    the button — the two can never overlap. It returns BUSY if a scan was
    already running, or a result string once its own scan has finished.

    Returns the outcome as a short string, which is what the tests assert on.
    """
    global _last_outcome
    now = now or _utcnow()
    if not interval_seconds():
        _log_once("off", "schedule is off")
        return "off"

    due = next_due_at(now)
    if due is None or now < due:
        _log_once("waiting", f"next scan due {due.isoformat()}" if due else "waiting")
        return "waiting"

    users = configured_users()
    if not users:
        _log_once("no-users", "on, but no default user(s) are set in Settings — "
                              "tick at least one and an unattended scan can run")
        return "no-users"

    room, used, needed = _quota_headroom()
    if not room:
        _log_once("quota", f"holding off: OMDb is at {used}/{db.OMDB_DAILY_LIMIT} "
                           f"today and a scan wants up to {needed} more. "
                           f"The quota resets at midnight UTC.")
        return "quota"

    # Written before the scan, so a scan that kills the process still moves the
    # clock — otherwise a reproducible crash becomes a restart loop.
    before = db.autoscan_state()
    db.autoscan_record_attempt(now.isoformat())
    _log_once("running", f"scan due, running it for {len(users)} user(s)")
    result = await run_scan(users)
    if result == BUSY:
        # A manual scan beat us to the lock between the check and the start.
        # Nothing ran, so nothing should claim to have run.
        db.autoscan_restore_state(before)
        _last_outcome = None
        return BUSY
    db.autoscan_record_result(result)
    _last_outcome = None  # the next tick's outcome is worth a line either way
    if result != OK:
        log.warning("autoscan: scan failed — %s", result)
    return result


async def run_loop(run_scan) -> None:
    """Tick forever. Cancelled by the lifespan handler on shutdown."""
    await asyncio.sleep(STARTUP_GRACE_SECONDS)
    while True:
        try:
            await tick(run_scan)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A broken tick must not take the loop down with it — the next one
            # may well succeed, and a dead loop is a silently dead feature.
            log.exception("autoscan tick failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def status() -> dict:
    """Everything the Settings panel needs to describe the schedule."""
    interval = db.settings_get_all().get("autoscan_interval", "off")
    seconds = db.autoscan_seconds(interval)
    users = configured_users()
    state = db.autoscan_state()
    due = next_due_at()
    return {
        "interval": interval if seconds else "off",
        "interval_seconds": seconds,
        # On *and* able to run. A schedule with no default users is neither
        # off nor working, and the panel has to be able to say so.
        "enabled": bool(seconds and users),
        "users": users,
        "next_due_at": due.isoformat() if due else None,
        "last_attempt_at": state.get("autoscan_last_attempt_at") or None,
        "last_result": state.get("autoscan_last_result") or None,
    }
