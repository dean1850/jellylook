"""Taste-profile weighting, metadata extraction, exclusion and auto-request."""
import json

import pytest

from app import metadata, recommender
from app.models import Recommendation, WatchedItem
from tests.conftest import make_scan


# --- profile ---------------------------------------------------------------------
def watched(title, plays=1):
    return WatchedItem(title=title, media_type="movie", year=2020, play_count=plays)


def test_recent_titles_outweigh_older_ones():
    items = [watched(f"T{i}") for i in range(20)]
    seeds = recommender.build_profile(items)["recently_watched"]
    assert seeds[0]["title"] == "T0"
    assert seeds[0]["weight"] > seeds[-1]["weight"]


def test_replays_raise_a_title_up_the_order():
    items = [watched("Once", 1), watched("Rewatched", 10)]
    seeds = recommender.build_profile(items)["recently_watched"]
    assert seeds[0]["title"] == "Rewatched"


def test_play_count_influence_is_capped():
    """A title played 500 times must not drown out everything else."""
    a = recommender.build_profile([watched("A", 10)])["recently_watched"][0]
    b = recommender.build_profile([watched("B", 500)])["recently_watched"][0]
    assert a["weight"] == b["weight"]


def test_profile_is_limited_to_the_seed_budget():
    items = [watched(f"T{i}") for i in range(200)]
    seeds = recommender.build_profile(items)["recently_watched"]
    assert len(seeds) == recommender.PROFILE_SEEDS


def test_empty_history_produces_an_empty_profile():
    assert recommender.build_profile([])["recently_watched"] == []


# --- suggestion dedupe -------------------------------------------------------------
def suggestion(title, media_type="movie"):
    from app.models import Suggestion
    return Suggestion(title=title, media_type=media_type, year=2020)


def test_dedupe_drops_repeats_and_watched_titles():
    got = recommender._dedupe(
        [suggestion("Dune"), suggestion("dune"), suggestion("Arrival"),
         suggestion("Seen It")],
        watched_titles={"seen it"},
    )
    assert [s.title for s in got] == ["Dune", "Arrival"]


def test_dedupe_keeps_a_film_and_a_series_of_the_same_name():
    got = recommender._dedupe(
        [suggestion("Fargo", "movie"), suggestion("Fargo", "tv")], set())
    assert len(got) == 2


# --- auto-request ------------------------------------------------------------------
def rec(title, tmdb_id, *, match_score=95, media_type="movie", in_library=False):
    return Recommendation(
        id=f"r{tmdb_id}", scan_id="s1", created_at="2026-01-01T00:00:00+00:00",
        media_type=media_type, title=title, year=2024, tmdb_id=tmdb_id,
        match_score=match_score, is_in_library=in_library,
    )


class FakeSeerr:
    """Stand-in for app.seerr with the same shape and no network."""

    def __init__(self, *, reachable=True, existing=None, fails=(), seasons=(1, 2, 3)):
        self.reachable = reachable
        self.existing = existing or {}      # "movie:1" -> status already in Seerr
        self.fails = set(fails)             # tmdb ids Seerr refuses
        self.seasons = list(seasons)
        self.requests: list[tuple] = []

    async def ping(self):
        return self.reachable

    async def get_status(self, media_type, tmdb_id):
        return self.existing.get(f"{media_type}:{tmdb_id}")

    async def get_seasons(self, tmdb_id):
        return [{"seasonNumber": n, "episodeCount": 8, "name": f"Season {n}"}
                for n in self.seasons]

    async def request(self, media_type, tmdb_id, seasons=None):
        self.requests.append((media_type, tmdb_id, seasons))
        if tmdb_id in self.fails:
            return {"ok": False, "error": "Seerr said no"}
        return {"ok": True, "status": "requested"}


@pytest.fixture
def fake_seerr(monkeypatch, fresh_db):
    """A fake Seerr plus a real (empty) database for the status writes."""
    fake = FakeSeerr()
    monkeypatch.setattr(recommender, "seerr", fake)
    return fake


@pytest.mark.asyncio
async def test_auto_request_is_off_by_default(fake_seerr):
    """Nothing may be requested until the user sets a threshold."""
    sent = await recommender.auto_request([rec("Great", 1)], threshold=0)
    assert sent == []
    assert fake_seerr.requests == []


@pytest.mark.asyncio
async def test_auto_request_sends_only_titles_over_the_threshold(fake_seerr):
    recs = [rec("Perfect", 1, match_score=96), rec("Close", 2, match_score=90),
            rec("Middling", 3, match_score=71), rec("Unscored", 4, match_score=None)]
    sent = await recommender.auto_request(recs, threshold=90)
    assert [r.title for r in sent] == ["Perfect", "Close"]
    assert [tmdb for _type, tmdb, _seasons in fake_seerr.requests] == [1, 2]
    assert recs[0].auto_requested and recs[0].seerr_status == "requested"
    assert not recs[2].auto_requested


@pytest.mark.asyncio
async def test_auto_request_sends_highest_match_first(fake_seerr):
    sent = await recommender.auto_request(
        [rec("Third", 1, match_score=91), rec("First", 2, match_score=99),
         rec("Second", 3, match_score=95)], threshold=90)
    assert [r.title for r in sent] == ["First", "Second", "Third"]


@pytest.mark.asyncio
async def test_auto_request_skips_owned_unresolved_and_known_titles(fake_seerr):
    fake_seerr.existing = {"movie:2": "available"}
    recs = [rec("Owned", 1, in_library=True), rec("Already there", 2),
            rec("Unresolved", None), rec("Fresh", 4)]
    sent = await recommender.auto_request(recs, threshold=90)
    assert [r.title for r in sent] == ["Fresh"]
    # The title Seerr already knew about keeps the state Seerr reported, and is
    # not badged as ours.
    assert recs[1].seerr_status == "available"
    assert not recs[1].auto_requested


@pytest.mark.asyncio
async def test_auto_request_skipped_entirely_when_seerr_is_down(fake_seerr):
    fake_seerr.reachable = False
    progress = {}
    sent = await recommender.auto_request([rec("Great", 1)], threshold=90,
                                          progress=progress)
    assert sent == []
    assert fake_seerr.requests == []
    # The user switched this on; silence would read as a broken setting.
    assert "unreachable" in progress["auto_note"]
    assert progress["auto_requested"] == 0


@pytest.mark.asyncio
async def test_progress_carries_what_was_sent(fake_seerr):
    progress = {}
    await recommender.auto_request(
        [rec("Perfect", 1, match_score=96), rec("Ignored", 2, match_score=40)],
        threshold=90, progress=progress)
    assert progress["auto_requested"] == 1
    assert progress["auto_sent"] == ["Perfect"]
    assert progress["auto_threshold"] == 90
    assert "auto_note" not in progress


@pytest.mark.asyncio
async def test_progress_reports_a_refusal(fake_seerr):
    fake_seerr.fails = {1}
    progress = {}
    await recommender.auto_request([rec("Refused", 1)], threshold=90,
                                   progress=progress)
    assert progress["auto_requested"] == 0
    assert "refused 1" in progress["auto_note"].lower()


@pytest.mark.asyncio
async def test_one_rejected_title_does_not_stop_the_rest(fake_seerr):
    fake_seerr.fails = {1}
    sent = await recommender.auto_request(
        [rec("Refused", 1, match_score=99), rec("Accepted", 2, match_score=95)],
        threshold=90)
    assert [r.title for r in sent] == ["Accepted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tv_mode,expected", [
    ("first", [1]),
    ("all", [1, 2, 3]),
    # Nobody is at the keyboard during a scan, so "ask" cannot be honoured.
    ("ask", [1, 2, 3]),
])
async def test_tv_seasons_follow_the_request_mode(fake_seerr, tv_mode, expected):
    await recommender.auto_request([rec("Series", 1, media_type="tv")],
                                   threshold=90, tv_mode=tv_mode)
    assert fake_seerr.requests == [("tv", 1, expected)]


@pytest.mark.asyncio
async def test_tv_is_skipped_when_seerr_returns_no_seasons(fake_seerr):
    """Better to leave the card's picker than to guess at the season list."""
    fake_seerr.seasons = []
    sent = await recommender.auto_request([rec("Series", 1, media_type="tv")],
                                          threshold=90, tv_mode="all")
    assert sent == [] and fake_seerr.requests == []


@pytest.mark.asyncio
async def test_auto_request_persists_the_badge(fake_seerr, fresh_db):
    fresh_db.insert_scan(make_scan("s1"))
    fresh_db.insert_recommendations("s1", [rec("Perfect", 1).model_dump()])
    await recommender.auto_request([rec("Perfect", 1)], threshold=90)
    item = fresh_db.get_recommendations(scan="s1")["items"][0]
    assert item["auto_requested"] is True
    assert item["seerr_status"] == "requested"


@pytest.mark.asyncio
async def test_threshold_comes_from_settings_when_not_passed(fake_seerr, fresh_db):
    fresh_db.settings_set("auto_request_threshold", "90")
    sent = await recommender.auto_request([rec("Perfect", 1, match_score=92)])
    assert [r.title for r in sent] == ["Perfect"]


# --- metadata extraction -----------------------------------------------------------
def test_trailer_preference_order():
    videos = {"results": [
        {"site": "YouTube", "type": "Teaser", "key": "teaser"},
        {"site": "YouTube", "type": "Trailer", "key": "unofficial", "official": False},
        {"site": "YouTube", "type": "Trailer", "key": "official", "official": True},
    ]}
    assert metadata._pick_trailer(videos) == "official"


def test_trailer_falls_back_through_the_chain():
    assert metadata._pick_trailer(
        {"results": [{"site": "YouTube", "type": "Teaser", "key": "t"}]}) == "t"
    assert metadata._pick_trailer({"results": []}) is None
    assert metadata._pick_trailer(None) is None


def test_non_youtube_videos_are_ignored():
    """The UI builds a youtube.com URL, so another host would produce a dead link."""
    videos = {"results": [{"site": "Vimeo", "type": "Trailer", "key": "v"}]}
    assert metadata._pick_trailer(videos) is None


def test_providers_are_sliced_to_the_configured_region():
    block = {"results": {
        "AU": {"flatrate": [{"provider_name": "Aussie", "logo_path": "/a.png"}],
               "link": "https://example/watch"},
        "US": {"flatrate": [{"provider_name": "Yank", "logo_path": "/y.png"}]},
    }}
    got = json.loads(metadata._pick_providers(block, "AU"))
    assert got["flatrate"] == [{"name": "Aussie", "logo": "/a.png"}]
    assert got["link"] == "https://example/watch"
    assert "Yank" not in json.dumps(got)


def test_providers_are_none_when_the_region_is_absent():
    block = {"results": {"US": {"flatrate": [{"provider_name": "Yank"}]}}}
    assert metadata._pick_providers(block, "AU") is None
    assert metadata._pick_providers({"results": {}}, "AU") is None
    assert metadata._pick_providers(None, "AU") is None


def test_empty_provider_categories_are_omitted():
    """The streaming filter tests for a "flatrate" key, so it must mean something."""
    block = {"results": {"AU": {"flatrate": [], "rent": [
        {"provider_name": "RentCo", "logo_path": "/r.png"}]}}}
    got = json.loads(metadata._pick_providers(block, "AU"))
    assert "flatrate" not in got
    assert got["rent"] == [{"name": "RentCo", "logo": "/r.png"}]


def test_provider_entries_without_a_name_are_skipped():
    block = {"results": {"AU": {"flatrate": [{"logo_path": "/x.png"}]}}}
    assert metadata._pick_providers(block, "AU") is None


# --- config -------------------------------------------------------------------------
@pytest.mark.parametrize("region,ok", [("AU", True), ("us", True), ("USA", False),
                                       ("", False), ("1A", False)])
def test_region_validation(region, ok):
    from app.config import Settings
    s = Settings(tmdb_api_key="k", omdb_api_key="k", jellyfin_api_key="k",
                 jellystat_api_key="k", anthropic_api_key="k", tmdb_region=region)
    problems = [p for p in s.validate_runtime() if "TMDB_REGION" in p]
    assert (not problems) is ok
    if ok:
        assert s.region == region.upper()
