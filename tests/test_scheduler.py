"""Unattended-scan scheduling: when a scan is due, and when it must not run.

Every test drives real SQLite through the fresh_db fixture — the due maths
reads the scans table, so stubbing it out would test nothing worth testing.
Time is controlled by writing scan rows at chosen timestamps and passing an
explicit `now`, never by sleeping.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import db, main, scheduler
from tests.conftest import make_scan

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def at(**delta) -> str:
    """An ISO timestamp relative to NOW, e.g. at(hours=-6)."""
    return (NOW + timedelta(**delta)).isoformat()


def arm(fresh_db, interval="1d", users="u1"):
    """Turn the schedule on the way the Settings panel would."""
    fresh_db.settings_set("autoscan_interval", interval)
    fresh_db.settings_set("default_user_ids", users)


def runner(result=scheduler.OK):
    """A stand-in for main._autoscan_runner that records what it was asked."""
    calls = []

    async def run(user_ids):
        calls.append(user_ids)
        return result

    run.calls = calls
    return run


# --- interval parsing -------------------------------------------------------------
def test_off_is_the_default(fresh_db):
    assert fresh_db.settings_get_all()["autoscan_interval"] == "off"
    assert scheduler.interval_seconds() == 0
    assert scheduler.next_due_at(NOW) is None


def test_every_offered_interval_has_a_length():
    assert db.autoscan_seconds("5h") == 5 * 3600
    assert db.autoscan_seconds("1d") == 86400
    assert db.autoscan_seconds("1w") == 7 * 86400
    assert db.autoscan_seconds("1m") == 30 * 86400


def test_an_unrecognised_interval_reads_as_off(fresh_db):
    """A misspelt or future-version value must not run scans by accident."""
    fresh_db.settings_set("autoscan_interval", "1y")
    assert scheduler.interval_seconds() == 0
    assert scheduler.next_due_at(NOW) is None


# --- due maths --------------------------------------------------------------------
def test_a_new_schedule_with_no_scans_is_due_immediately(fresh_db):
    arm(fresh_db, "1w")
    assert scheduler.next_due_at(NOW) == NOW


def test_not_due_until_the_interval_has_elapsed(fresh_db):
    arm(fresh_db, "5h")
    fresh_db.insert_scan(make_scan("s1", created_at=at(hours=-1)))
    assert scheduler.next_due_at(NOW) > NOW


def test_due_once_the_interval_has_elapsed(fresh_db):
    arm(fresh_db, "5h")
    fresh_db.insert_scan(make_scan("s1", created_at=at(hours=-6)))
    assert scheduler.next_due_at(NOW) < NOW


def test_a_manual_scan_resets_the_clock(fresh_db):
    """Pressing Scan means fresh recommendations; the schedule has nothing to
    add for another full interval."""
    arm(fresh_db, "5h")
    fresh_db.autoscan_record_attempt(at(hours=-10))
    fresh_db.autoscan_record_result(scheduler.OK)
    fresh_db.insert_scan(make_scan("s1", created_at=at(hours=-1)))
    assert scheduler.next_due_at(NOW) == NOW + timedelta(hours=4)


def test_the_longest_interval_still_fires_after_a_restart(fresh_db):
    """The anchor is a stored timestamp, not a countdown, so uptime is
    irrelevant — a monthly schedule survives nightly reboots."""
    arm(fresh_db, "1m")
    fresh_db.insert_scan(make_scan("s1", created_at=at(days=-31)))
    assert scheduler.next_due_at(NOW) < NOW


# --- failure backoff --------------------------------------------------------------
def test_a_failed_attempt_retries_within_the_hour_not_the_month(fresh_db):
    """One transient LLM error must not cost a monthly schedule a month."""
    arm(fresh_db, "1m")
    fresh_db.autoscan_record_attempt(at(hours=-2))
    fresh_db.autoscan_record_result("error: provider timed out")
    assert scheduler.next_due_at(NOW) == NOW - timedelta(hours=1)


def test_a_failed_attempt_still_waits_out_the_backoff(fresh_db):
    arm(fresh_db, "1m")
    fresh_db.autoscan_record_attempt(at(minutes=-30))
    fresh_db.autoscan_record_result("error: provider timed out")
    assert scheduler.next_due_at(NOW) > NOW


def test_the_backoff_only_ever_shortens_the_wait(fresh_db):
    """Even on the shortest schedule the retry lands an hour after the
    failure — sooner than the interval, never later than it."""
    arm(fresh_db, "5h")
    failed_at = NOW - timedelta(hours=2)
    fresh_db.autoscan_record_attempt(failed_at.isoformat())
    fresh_db.autoscan_record_result("error: nope")
    due = scheduler.next_due_at(NOW)
    assert due == failed_at + timedelta(hours=1)
    assert due <= failed_at + timedelta(hours=5)


def test_a_scan_that_killed_the_process_counts_as_failed(fresh_db):
    """'running' is what a crash leaves behind — retry, don't wait a month."""
    arm(fresh_db, "1m")
    fresh_db.autoscan_record_attempt(at(hours=-2))
    assert fresh_db.autoscan_state()["autoscan_last_result"] == "running"
    assert scheduler.next_due_at(NOW) < NOW


def test_a_later_successful_scan_clears_the_backoff(fresh_db):
    """A stale error must not keep shortening the interval forever."""
    arm(fresh_db, "1w")
    fresh_db.autoscan_record_attempt(at(days=-3))
    fresh_db.autoscan_record_result("error: nope")
    fresh_db.insert_scan(make_scan("s1", created_at=at(days=-2)))
    assert scheduler.next_due_at(NOW) == NOW + timedelta(days=5)


# --- guards -----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tick_does_nothing_while_the_schedule_is_off(fresh_db):
    run = runner()
    assert await scheduler.tick(run, NOW) == "off"
    assert run.calls == []


@pytest.mark.asyncio
async def test_tick_waits_when_the_scan_is_not_due(fresh_db):
    arm(fresh_db, "5h")
    fresh_db.insert_scan(make_scan("s1", created_at=at(hours=-1)))
    run = runner()
    assert await scheduler.tick(run, NOW) == "waiting"
    assert run.calls == []


@pytest.mark.asyncio
async def test_tick_refuses_to_run_with_no_default_users(fresh_db):
    fresh_db.settings_set("autoscan_interval", "1d")
    fresh_db.settings_set("default_user_ids", "")
    run = runner()
    assert await scheduler.tick(run, NOW) == "no-users"
    assert run.calls == []
    # Nothing ran, so nothing may claim to have run.
    assert fresh_db.autoscan_state()["autoscan_last_attempt_at"] == ""


@pytest.mark.asyncio
async def test_tick_holds_off_when_omdb_has_no_room_for_a_scan(fresh_db):
    arm(fresh_db)
    fresh_db.settings_set("recs_per_scan", "60")
    fresh_db.omdb_increment(db.OMDB_DAILY_LIMIT - 10)
    run = runner()
    assert await scheduler.tick(run, NOW) == "quota"
    assert run.calls == []
    # Still due, so it goes as soon as the quota resets rather than skipping
    # the cycle outright.
    assert scheduler.next_due_at(NOW) <= NOW


@pytest.mark.asyncio
async def test_tick_runs_when_omdb_has_room(fresh_db):
    arm(fresh_db)
    fresh_db.settings_set("recs_per_scan", "10")
    fresh_db.omdb_increment(db.OMDB_DAILY_LIMIT - 200)
    run = runner()
    assert await scheduler.tick(run, NOW) == scheduler.OK
    assert run.calls == [["u1"]]


# --- running ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_due_scan_runs_for_the_default_users(fresh_db):
    arm(fresh_db, "1d", users="u1,u2")
    run = runner()
    assert await scheduler.tick(run, NOW) == scheduler.OK
    assert run.calls == [["u1", "u2"]]
    state = fresh_db.autoscan_state()
    assert state["autoscan_last_attempt_at"] == NOW.isoformat()
    assert state["autoscan_last_result"] == scheduler.OK


@pytest.mark.asyncio
async def test_a_failed_scan_records_why(fresh_db):
    arm(fresh_db)
    assert await scheduler.tick(runner("error: no watch history"), NOW) \
        == "error: no watch history"
    assert fresh_db.autoscan_state()["autoscan_last_result"] \
        == "error: no watch history"


@pytest.mark.asyncio
async def test_a_busy_runner_leaves_the_clock_exactly_as_it_was(fresh_db):
    """A manual scan won the lock. Nothing ran, so the recorded attempt is
    rolled back — Settings → Status must not report a scan that never was."""
    arm(fresh_db, "5h")
    fresh_db.autoscan_record_attempt(at(hours=-6))
    fresh_db.autoscan_record_result(scheduler.OK)
    before = fresh_db.autoscan_state()
    assert await scheduler.tick(runner(scheduler.BUSY), NOW) == scheduler.BUSY
    assert fresh_db.autoscan_state() == before


@pytest.mark.asyncio
async def test_a_busy_runner_on_a_first_ever_run_records_nothing(fresh_db):
    arm(fresh_db)
    assert await scheduler.tick(runner(scheduler.BUSY), NOW) == scheduler.BUSY
    assert fresh_db.autoscan_state() == {"autoscan_last_attempt_at": "",
                                         "autoscan_last_result": ""}


# --- status -----------------------------------------------------------------------
def test_status_reports_off_by_default(fresh_db):
    status = scheduler.status()
    assert status["interval"] == "off"
    assert status["enabled"] is False
    assert status["next_due_at"] is None


def test_status_reports_an_armed_schedule(fresh_db):
    arm(fresh_db, "1w", users="u1")
    fresh_db.insert_scan(make_scan("s1", created_at=at(days=-1)))
    status = scheduler.status()
    assert status["interval"] == "1w"
    assert status["interval_seconds"] == 7 * 86400
    assert status["enabled"] is True
    assert status["users"] == ["u1"]
    assert status["next_due_at"] is not None


def test_status_reports_a_schedule_that_cannot_run(fresh_db):
    """On, but with nobody to scan for — neither off nor working, and the
    panel has to be able to say so."""
    fresh_db.settings_set("autoscan_interval", "1d")
    fresh_db.settings_set("default_user_ids", "")
    status = scheduler.status()
    assert status["interval"] == "1d"
    assert status["enabled"] is False


# --- the settings guard -----------------------------------------------------------
def test_arming_a_schedule_without_users_is_rejected(fresh_db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        main._check_autoscan_has_users({"autoscan_interval": "1d",
                                        "default_user_ids": ""})
    assert exc.value.status_code == 400
    assert "default user" in exc.value.detail


def test_clearing_the_users_of_a_live_schedule_is_rejected(fresh_db):
    from fastapi import HTTPException

    arm(fresh_db, "1d", users="u1")
    with pytest.raises(HTTPException):
        main._check_autoscan_has_users({"default_user_ids": ""})


def test_turning_the_schedule_off_never_needs_users(fresh_db):
    main._check_autoscan_has_users({"autoscan_interval": "off",
                                    "default_user_ids": ""})


def test_arming_a_schedule_alongside_its_users_is_allowed(fresh_db):
    main._check_autoscan_has_users({"autoscan_interval": "5h",
                                    "default_user_ids": "u1,u2"})


def test_users_already_stored_satisfy_a_later_arming(fresh_db):
    fresh_db.settings_set("default_user_ids", "u1")
    main._check_autoscan_has_users({"autoscan_interval": "1m"})


# --- scan provenance ---------------------------------------------------------------
def test_a_scan_records_whether_it_was_automatic(fresh_db):
    fresh_db.insert_scan(make_scan("s1", created_at=at(hours=-2)))
    fresh_db.insert_scan(dict(make_scan("s2", created_at=at(hours=-1)),
                              trigger="auto"))
    by_id = {scan["id"]: scan for scan in fresh_db.list_scans(10)}
    assert by_id["s2"]["trigger"] == "auto"
    # make_scan() omits it entirely, exactly as a pre-upgrade row would.
    assert by_id["s1"]["trigger"] == "manual"
