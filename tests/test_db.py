"""Schema, migrations, per-user feedback, the all-time pool, filters and purge."""
import sqlite3

import pytest

from tests.conftest import iso, make_rec, make_scan


# --- migrations ------------------------------------------------------------------
def test_init_db_is_idempotent(fresh_db):
    fresh_db.init_db()
    fresh_db.init_db()
    with fresh_db._conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
    assert {"genre_ids", "runtime", "original_language", "popularity",
            "trailer_key", "providers", "auto_requested"} <= cols


def test_add_column_upgrades_a_pre_existing_table(fresh_db):
    """A 1.0 database must gain the new columns without losing its rows."""
    with fresh_db._conn() as conn:
        conn.execute("DROP TABLE recommendations")
        conn.execute("CREATE TABLE recommendations ("
                     "id TEXT PRIMARY KEY, scan_id TEXT, created_at TEXT, "
                     "media_type TEXT, title TEXT, year INTEGER, tmdb_id INTEGER, "
                     "imdb_id TEXT, imdb_rating REAL, tmdb_rating REAL, "
                     "poster_url TEXT, backdrop_url TEXT, overview TEXT, "
                     "genres TEXT, reason TEXT, match_score INTEGER, "
                     "because_of TEXT, is_in_library INTEGER, seerr_status TEXT)")
        conn.execute("INSERT INTO recommendations(id, scan_id, title, media_type, "
                     "created_at) VALUES ('x', 's1', 'Legacy', 'movie', ?)", (iso(),))
    fresh_db.init_db()
    with fresh_db._conn() as conn:
        row = conn.execute("SELECT title, runtime FROM recommendations").fetchone()
    assert row["title"] == "Legacy"
    assert row["runtime"] is None  # unknown, not zero


def test_genre_key_roundtrip(fresh_db):
    key = fresh_db.genre_key([28, 12])
    assert key == ",28,12,"
    assert fresh_db.parse_genre_key(key) == [28, 12]
    assert fresh_db.genre_key([]) is None
    assert fresh_db.parse_genre_key(None) == []


def test_genre_key_sentinels_prevent_substring_matches(fresh_db):
    """',12,' must not match inside ',120,' — the reason for the sentinels."""
    fresh_db.insert_scan(make_scan("s1"))
    fresh_db.insert_recommendations("s1", [
        make_rec("Wide", 1, genre_ids=fresh_db.genre_key([120])),
        make_rec("Narrow", 2, genre_ids=fresh_db.genre_key([12])),
    ])
    got = fresh_db.get_recommendations(scan="s1", genres=[12])
    assert [i["title"] for i in got["items"]] == ["Narrow"]


# --- feedback ----------------------------------------------------------------------
def test_feedback_is_scoped_to_each_user(fresh_db):
    fresh_db.feedback_set(["u1"], "movie", 10, "hidden", "Nope", 2020)
    assert fresh_db.hidden_ids(["u1"]) == {"movie:10"}
    assert fresh_db.hidden_ids(["u2"]) == set()
    # A shared session unions the two, so one person's veto covers the room.
    assert fresh_db.hidden_ids(["u1", "u2"]) == {"movie:10"}


def test_feedback_set_applies_to_every_selected_user(fresh_db):
    fresh_db.feedback_set(["u1", "u2"], "tv", 11, "saved", "Yes", 2021)
    assert fresh_db.feedback_counts(["u1"])["saved"] == 1
    assert fresh_db.feedback_counts(["u2"])["saved"] == 1
    # Counted once for the pair, not twice.
    assert fresh_db.feedback_counts(["u1", "u2"])["saved"] == 1


def test_feedback_verdict_can_be_changed_and_cleared(fresh_db):
    fresh_db.feedback_set(["u1"], "movie", 12, "saved", "Maybe")
    fresh_db.feedback_set(["u1"], "movie", 12, "hidden", "Maybe")
    assert fresh_db.hidden_ids(["u1"]) == {"movie:12"}
    assert fresh_db.feedback_counts(["u1"]) == {"saved": 0, "hidden": 1}
    fresh_db.feedback_clear(["u1"], "movie", 12)
    assert fresh_db.hidden_ids(["u1"]) == set()


def test_feedback_rejects_unknown_verdict(fresh_db):
    with pytest.raises(ValueError):
        fresh_db.feedback_set(["u1"], "movie", 1, "maybe", "T")


def test_hidden_wins_over_saved_in_a_shared_view(fresh_db):
    """If one viewer banned it, the group view omits it even if another saved it."""
    fresh_db.insert_scan(make_scan("s1"))
    fresh_db.insert_recommendations("s1", [make_rec("Contested", 20)])
    fresh_db.feedback_set(["u1"], "movie", 20, "saved", "Contested")
    fresh_db.feedback_set(["u2"], "movie", 20, "hidden", "Contested")
    assert fresh_db.get_recommendations(scan="s1", user_ids=["u1"])["total"] == 1
    assert fresh_db.get_recommendations(scan="s1", user_ids=["u1", "u2"])["total"] == 0


def test_feedback_titles_formats_for_the_prompt(fresh_db):
    fresh_db.feedback_set(["u1"], "movie", 1, "saved", "With Year", 1999)
    fresh_db.feedback_set(["u1"], "tv", 2, "saved", "No Year", None)
    titles = fresh_db.feedback_titles(["u1"], "saved", 10)
    assert "With Year (1999)" in titles
    assert "No Year" in titles


def test_saved_view_survives_the_recommendation_being_purged(fresh_db):
    """The denormalised title/poster is what keeps a saved entry listable."""
    fresh_db.feedback_set(["u1"], "movie", 30, "saved", "Orphan", 2011, "/o.jpg")
    items = fresh_db.get_feedback_items(["u1"], "saved")["items"]
    assert items[0]["title"] == "Orphan"
    assert items[0]["poster_url"] == "/o.jpg"
    assert items[0]["verdict"] == "saved"


# --- settings ------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("0", 0), (0, 0), (-5, 0),          # off, however it is spelled
    ("90", 90), (100, 100), (70, 70),
    (49, 50), (140, 100),               # clamped into the supported band
])
def test_auto_threshold_is_clamped(fresh_db, raw, expected):
    assert fresh_db.clamp_auto_threshold(raw) == expected


def test_auto_threshold_rejects_nonsense(fresh_db):
    """Callers decide what to do with a bad value; they need it to raise."""
    for bad in ("", "ninety", None, "9o"):
        with pytest.raises((ValueError, TypeError)):
            fresh_db.clamp_auto_threshold(bad)


def test_auto_request_is_off_in_a_fresh_database(fresh_db):
    assert fresh_db.settings_effective(60)["auto_request_threshold"] == "0"


def test_mark_auto_requested_flags_every_row_for_the_title(fresh_db):
    """The badge has to survive into earlier scans and the all-time pool."""
    fresh_db.insert_scan(make_scan("s1", created_at=iso(2)))
    fresh_db.insert_scan(make_scan("s2"))
    fresh_db.insert_recommendations("s1", [make_rec("Twice Seen", 7, scan_id="s1",
                                                    created_at=iso(2))])
    fresh_db.insert_recommendations("s2", [make_rec("Twice Seen", 7, scan_id="s2")])
    assert fresh_db.mark_auto_requested("movie", 7) == 2
    for scan in ("s1", "s2"):
        item = fresh_db.get_recommendations(scan=scan)["items"][0]
        assert item["auto_requested"] is True
        assert item["seerr_status"] == "requested"


def test_rows_are_not_auto_requested_by_default(fresh_db):
    fresh_db.insert_scan(make_scan("s1"))
    fresh_db.insert_recommendations("s1", [make_rec("Plain", 8)])
    assert fresh_db.get_recommendations(scan="s1")["items"][0]["auto_requested"] is False


def test_saved_view_reports_the_auto_badge(fresh_db):
    """Saved/Hidden select their columns by hand, so the flag needs adding there."""
    fresh_db.insert_scan(make_scan("s1"))
    fresh_db.insert_recommendations("s1", [make_rec("Kept", 9)])
    fresh_db.mark_auto_requested("movie", 9)
    fresh_db.feedback_set(["u1"], "movie", 9, "saved", "Kept")
    item = fresh_db.get_feedback_items(["u1"], "saved")["items"][0]
    assert item["auto_requested"] is True


# --- browsing ------------------------------------------------------------------------
@pytest.fixture
def populated(fresh_db):
    fresh_db.insert_scan(make_scan("s1", created_at=iso(2)))
    fresh_db.insert_scan(make_scan("s2", created_at=iso(0)))
    fresh_db.insert_recommendations("s1", [
        make_rec("Alpha", 1, scan_id="s1", created_at=iso(2), match_score=50),
        make_rec("Beta", 2, scan_id="s1", created_at=iso(2), media_type="tv",
                 genre_ids=",18,", runtime=45, providers=None, imdb_rating=9.1),
        make_rec("Gamma", 3, scan_id="s1", created_at=iso(2), genre_ids=None),
    ])
    fresh_db.insert_recommendations("s2", [
        make_rec("Alpha", 1, scan_id="s2", created_at=iso(0), match_score=99),
        make_rec("Delta", 4, scan_id="s2", created_at=iso(0),
                 is_in_library=True, seerr_status="requested"),
    ])
    return fresh_db


def test_latest_scan_only_shows_that_scan(populated):
    got = populated.get_recommendations(scan="latest")
    assert sorted(i["title"] for i in got["items"]) == ["Alpha", "Delta"]


def test_all_time_dedupes_keeping_the_newest_sighting(populated):
    got = populated.get_recommendations(scan="all")
    assert got["total"] == 4
    alpha = next(i for i in got["items"] if i["title"] == "Alpha")
    assert alpha["match_score"] == 99, "the newer of the two Alpha rows must win"
    assert "rn" not in alpha, "window-function bookkeeping must not leak into the API"


def test_all_time_excludes_hidden(populated):
    populated.feedback_set(["u1"], "movie", 1, "hidden", "Alpha")
    got = populated.get_recommendations(scan="all", user_ids=["u1"])
    assert "Alpha" not in [i["title"] for i in got["items"]]
    assert populated.all_time_total(["u1"]) == 3
    assert populated.all_time_total([]) == 4


def test_unknown_scan_id_returns_empty(populated):
    assert populated.get_recommendations(scan="nope")["total"] == 0


@pytest.mark.parametrize("kwargs,expected", [
    ({"media_type": "tv"}, ["Beta"]),
    ({"genres": [53]}, ["Alpha", "Delta"]),      # Beta is Drama, Gamma has none
    ({"genres": [18]}, ["Beta"]),
    ({"genres": [18, 53]}, ["Alpha", "Beta", "Delta"]),   # any-of
    ({"runtime_max": 60}, ["Beta"]),
    ({"imdb_min": 9.0}, ["Beta"]),
    ({"in_library": "only"}, ["Delta"]),
    ({"in_library": "hide"}, ["Alpha", "Beta", "Gamma"]),
    ({"requested": "hide"}, ["Alpha", "Beta", "Gamma"]),
    ({"streaming": "hide"}, ["Beta"]),
    ({"streaming": "only"}, ["Alpha", "Delta", "Gamma"]),
])
def test_filters(populated, kwargs, expected):
    got = populated.get_recommendations(scan="all", **kwargs)
    assert sorted(i["title"] for i in got["items"]) == expected


def test_genre_filter_excludes_rows_with_no_genre_data(populated):
    """Gamma has NULL genre_ids: unknown cannot be claimed as a match."""
    got = populated.get_recommendations(scan="all", genres=[53])
    assert "Gamma" not in [i["title"] for i in got["items"]]
    assert got["dropped_no_genre"] == 1


def test_numeric_filters_keep_rows_with_unknown_values(fresh_db):
    """The mirror case: NULL runtime is unknown, so it must not be filtered out."""
    fresh_db.insert_scan(make_scan("s1"))
    fresh_db.insert_recommendations("s1", [make_rec("NoRuntime", 1, runtime=None)])
    got = fresh_db.get_recommendations(scan="s1", runtime_max=90)
    assert [i["title"] for i in got["items"]] == ["NoRuntime"]


def test_facets_are_computed_before_the_genre_filter(populated):
    """Picking a genre must not empty the genre picker."""
    unfiltered = populated.get_recommendations(scan="all")["facets"]["genres"]
    filtered = populated.get_recommendations(scan="all", genres=[53])["facets"]["genres"]
    assert filtered == unfiltered
    counts = {g["name"]: g["count"] for g in unfiltered}
    assert counts == {"Thriller": 2, "Drama": 1}   # Gamma contributes to neither


def test_pagination(populated):
    first = populated.get_recommendations(scan="all", per_page=2, page=1)
    second = populated.get_recommendations(scan="all", per_page=2, page=2)
    assert first["pages"] == 2 and first["total"] == 4
    assert len(first["items"]) == 2 and len(second["items"]) == 2
    assert not ({i["title"] for i in first["items"]}
                & {i["title"] for i in second["items"]})


@pytest.mark.parametrize("sort", ["match", "imdb", "year", "runtime", "added",
                                  "popularity"])
def test_every_sort_is_valid_sql(populated, sort):
    assert populated.get_recommendations(scan="all", sort=sort)["total"] == 4


def test_recent_suggested_titles_dedupes_and_orders_newest_first(populated):
    titles = populated.recent_suggested_titles(10)
    assert titles[0] in ("Alpha (2020)", "Delta (2020)")
    assert len(titles) == len(set(titles)) == 4


# --- purge ---------------------------------------------------------------------------
def test_purge_spares_saved_but_not_past_the_ceiling(fresh_db):
    fresh_db.insert_scan(make_scan("fresh", created_at=iso(1)))
    fresh_db.insert_scan(make_scan("old", created_at=iso(100)))
    fresh_db.insert_scan(make_scan("ancient", created_at=iso(400)))
    fresh_db.insert_recommendations("fresh", [make_rec("Fresh", 1, scan_id="fresh",
                                                       created_at=iso(1))])
    fresh_db.insert_recommendations("old", [
        make_rec("OldPlain", 2, scan_id="old", created_at=iso(100)),
        make_rec("OldSaved", 3, scan_id="old", created_at=iso(100)),
    ])
    fresh_db.insert_recommendations("ancient", [make_rec("AncientSaved", 4,
                                                         scan_id="ancient",
                                                         created_at=iso(400))])
    fresh_db.feedback_set(["u1"], "movie", 3, "saved", "OldSaved")
    fresh_db.feedback_set(["u1"], "movie", 4, "saved", "AncientSaved")

    fresh_db.purge_old(retention_days=60, max_retention_days=365)

    with fresh_db._conn() as conn:
        left = {r["title"] for r in conn.execute("SELECT title FROM recommendations")}
        scans = {r["id"] for r in conn.execute("SELECT id FROM scans")}
    assert left == {"Fresh", "OldSaved"}
    # The saved row keeps its parent scan alive; the expired one lets it go.
    assert scans == {"fresh", "old"}


def test_purge_never_deletes_feedback(fresh_db):
    fresh_db.feedback_set(["u1"], "movie", 9, "hidden", "Ancient Grudge", 1999)
    fresh_db.purge_old(retention_days=1, max_retention_days=1)
    assert fresh_db.hidden_ids(["u1"]) == {"movie:9"}


def test_purge_respects_a_retention_longer_than_the_ceiling(fresh_db):
    """RETENTION_DAYS above the ceiling must widen it, not silently truncate."""
    fresh_db.insert_scan(make_scan("s1", created_at=iso(500)))
    fresh_db.insert_recommendations("s1", [make_rec("Kept", 1, created_at=iso(500))])
    fresh_db.purge_old(retention_days=900, max_retention_days=365)
    with fresh_db._conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM recommendations").fetchone()["c"] == 1


def test_foreign_keys_are_enforced(fresh_db):
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.insert_recommendations("ghost-scan", [make_rec("Orphan", 1,
                                                                scan_id="ghost-scan")])
