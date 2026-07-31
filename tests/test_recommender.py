"""Taste-profile weighting, metadata extraction and scan-level exclusion."""
import json

import pytest

from app import metadata, recommender
from app.models import WatchedItem


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
