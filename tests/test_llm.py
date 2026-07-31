"""Prompt construction, the exclusion budget, and JSON extraction."""
import json

import pytest

from app import llm


# --- exclusion budget --------------------------------------------------------------
def test_priority_order_is_hidden_then_watched_then_suggested():
    out = llm.build_exclude_list(hidden=["H"], watched=["W"], suggested=["S"],
                                 budget=10)
    assert out == ["H", "W", "S"]


def test_hidden_is_never_starved_by_a_long_watch_history():
    """The old flat slice could spend the whole budget before reaching a rejection."""
    out = llm.build_exclude_list(
        hidden=[f"H{i}" for i in range(20)],
        watched=[f"W{i}" for i in range(500)],
        suggested=[],
        budget=100,
    )
    assert all(f"H{i}" in out for i in range(20))
    assert len(out) == 100


def test_each_source_is_capped_so_none_can_dominate():
    out = llm.build_exclude_list(
        hidden=[f"H{i}" for i in range(500)],
        watched=[f"W{i}" for i in range(500)],
        suggested=[f"S{i}" for i in range(500)],
        budget=300,
    )
    assert len([t for t in out if t.startswith("H")]) == llm.HIDDEN_CAP
    assert len([t for t in out if t.startswith("W")]) == llm.WATCHED_CAP
    assert len(out) == 300


def test_budget_is_never_exceeded():
    out = llm.build_exclude_list(hidden=["a"] * 5, watched=["b"] * 5,
                                 suggested=["c"] * 5, budget=3)
    assert len(out) == 3


def test_deduplicates_case_insensitively_keeping_the_first_seen():
    out = llm.build_exclude_list(hidden=["Dune"], watched=["dune", "Arrival"],
                                 suggested=["DUNE"], budget=10)
    assert out == ["Dune", "Arrival"]


def test_blank_entries_are_dropped():
    assert llm.build_exclude_list(["", "  ", "Real"], [], [], 10) == ["Real"]


def test_zero_budget_yields_nothing():
    assert llm.build_exclude_list(["H"], ["W"], ["S"], 0) == []


# --- prompt ------------------------------------------------------------------------
PROFILE = {"recently_watched": [{"title": "Seed", "type": "tv"}]}


def test_taste_blocks_are_omitted_below_the_threshold():
    """One or two rejections are noise, not a preference."""
    _, user = llm._build_prompts(PROFILE, 10, ["X"], liked=["A"], disliked=["B", "C"])
    assert "explicitly LIKED" not in user
    assert "explicitly REJECTED" not in user


def test_taste_blocks_appear_once_there_is_enough_signal():
    liked = ["A", "B", "C"]
    disliked = ["D", "E", "F"]
    _, user = llm._build_prompts(PROFILE, 10, ["X"], liked=liked, disliked=disliked)
    assert "explicitly LIKED" in user and "explicitly REJECTED" in user
    for title in liked + disliked:
        assert f'"{title}"' in user


def test_taste_blocks_are_capped():
    many = [f"T{i}" for i in range(200)]
    _, user = llm._build_prompts(PROFILE, 10, [], liked=many, disliked=many)
    assert f'"T{llm.MAX_TASTE_EXAMPLES}"' not in user
    assert '"T0"' in user


def test_exclude_list_is_rendered_verbatim():
    _, user = llm._build_prompts(PROFILE, 10, ["Alpha (2001)", "Beta"])
    assert json.dumps(["Alpha (2001)", "Beta"]) in user


def test_requested_count_is_stated_unambiguously():
    _, user = llm._build_prompts(PROFILE, 42, [])
    assert "Recommend exactly 42 titles" in user


# --- response parsing ----------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    '[{"title":"A","year":2020,"type":"movie"}]',
    '```json\n[{"title":"A","year":2020,"type":"movie"}]\n```',
    'Sure! Here you go:\n[{"title":"A","year":2020,"type":"movie"}]\nEnjoy.',
    '{"recommendations":[{"title":"A","year":2020,"type":"movie"}]}',
])
def test_extract_json_tolerates_real_model_output(raw):
    assert llm._extract_json(raw) == [{"title": "A", "year": 2020, "type": "movie"}]


def test_extract_json_handles_brackets_inside_strings():
    raw = '[{"title":"Se7en [remastered]","year":1995,"type":"movie"}]'
    assert llm._extract_json(raw)[0]["title"] == "Se7en [remastered]"


@pytest.mark.parametrize("raw", ["", "no json here", "{", "[{unclosed"])
def test_extract_json_gives_up_cleanly(raw):
    assert llm._extract_json(raw) == []


def test_to_suggestions_normalises_and_clamps():
    got = llm._to_suggestions([
        {"title": "A", "year": "2020", "type": "Series", "match_score": 250},
        {"title": "B", "year": "bad", "type": "tv show", "match_score": -5},
        {"title": "C", "type": "movie", "match_score": None},
    ], 10)
    assert [s.media_type for s in got] == ["tv", "tv", "movie"]
    assert got[0].match_score == 100 and got[0].year == 2020
    assert got[1].match_score == 0 and got[1].year is None
    assert got[2].match_score is None


def test_to_suggestions_drops_unusable_rows():
    got = llm._to_suggestions([
        {"title": "", "type": "movie"},
        {"type": "movie"},
        {"title": "NoType"},
        {"title": "Bad", "type": "album"},
        {"title": "Good", "type": "movie"},
    ], 10)
    assert [s.title for s in got] == ["Good"]


def test_to_suggestions_respects_the_requested_count():
    rows = [{"title": f"T{i}", "type": "movie"} for i in range(50)]
    assert len(llm._to_suggestions(rows, 5)) == 5
