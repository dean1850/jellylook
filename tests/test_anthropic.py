"""The Anthropic adapter: request shape per model generation, refusal handling,
the fallback model, and the one self-correcting retry on a 400.

The API is replaced by a recorder at http.request, so these tests run offline
and assert on the exact JSON the adapter would send.
"""
import json

import httpx
import pytest

from app import http, llm
from app.config import Settings

PROFILE = {"recently_watched": [{"title": "The Bear", "type": "tv", "year": 2022}]}

ROW = {"title": "Chef", "year": 2014, "type": "movie", "reason": "Kitchens",
       "match_score": 88, "because_of": "The Bear"}


def settings(**overrides) -> Settings:
    base = dict(tmdb_api_key="k", omdb_api_key="k", jellyfin_api_key="k",
                jellystat_api_key="k", anthropic_api_key="k",
                llm_provider="anthropic", llm_model="claude-haiku-5-5",
                llm_fallback_model="claude-sonnet-5-5", llm_effort="medium")
    base.update(overrides)
    return Settings(_env_file=None, **base)


def message(*blocks, stop_reason="end_turn", **extra) -> dict:
    return {"type": "message", "role": "assistant", "content": list(blocks),
            "stop_reason": stop_reason, **extra}


def text(payload) -> dict:
    return {"type": "text",
            "text": payload if isinstance(payload, str) else json.dumps(payload)}


GOOD = message({"type": "thinking", "thinking": "", "signature": "sig"},
               text({"recommendations": [ROW]}))
REFUSED = message(stop_reason="refusal",
                  stop_details={"type": "refusal", "category": "general_harms",
                                "explanation": None})


class FakeAPI:
    """Answers each request from a per-model queue and records the bodies."""

    def __init__(self, monkeypatch, **answers):
        # answers: model -> list of (status, body) or a dict (taken as 200)
        self.answers = {m.replace("_", "-"): list(v) for m, v in answers.items()}
        self.sent: list[dict] = []
        monkeypatch.setattr(http, "request", self.request)

    async def request(self, method, url, *, headers=None, json=None, **_):
        assert url == llm.AnthropicProvider.URL
        assert headers["x-api-key"] == "k"
        self.sent.append(json)
        answer = self.answers[json["model"]].pop(0)
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        return httpx.Response(status, json=body)

    def models(self) -> list[str]:
        return [body["model"] for body in self.sent]


async def recommend(s: Settings):
    provider = llm.get_provider(s)
    got = await provider.recommend(PROFILE, 1, ["The Bear"])
    return provider, got


# --- model generations ---------------------------------------------------------------
@pytest.mark.parametrize("model", [
    "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-fable-5-1",
])
def test_current_models_are_not_legacy(model):
    assert not llm.is_legacy_claude(model)


@pytest.mark.parametrize("model", [
    "claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-6",
    "claude-opus-4-6", "claude-sonnet-4-5", "claude-opus-4-1", "claude-sonnet-4-0",
    "claude-opus-4-20250514", "claude-3-5-haiku-latest", "claude-3-opus-20240229",
])
def test_older_models_are_legacy(model):
    assert llm.is_legacy_claude(model)


@pytest.mark.asyncio
async def test_haiku_5_5_request_has_no_temperature_and_uses_effort_and_schema(monkeypatch):
    api = FakeAPI(monkeypatch, claude_haiku_5_5=[GOOD])
    await recommend(settings(llm_effort="LOW"))
    body = api.sent[0]
    assert body["model"] == "claude-haiku-5-5"
    assert "temperature" not in body and "top_p" not in body and "top_k" not in body
    assert "thinking" not in body
    assert body["max_tokens"] == llm.ANTHROPIC_MAX_TOKENS
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"] == {
        "type": "json_schema", "schema": llm.RECOMMENDATION_SCHEMA}
    assert body["system"].endswith(llm.WRAP_HINT)
    assert body["messages"][-1]["role"] == "user"     # no prefill


@pytest.mark.asyncio
async def test_legacy_model_keeps_the_original_request(monkeypatch):
    api = FakeAPI(monkeypatch, claude_haiku_4_5=[message(text([ROW]))])
    await recommend(settings(llm_model="claude-haiku-4-5", llm_temperature=0.3))
    body = api.sent[0]
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == llm.MAX_TOKENS
    assert "output_config" not in body
    assert not body["system"].endswith(llm.WRAP_HINT)


def test_schema_follows_structured_output_rules():
    """Every object closed, every property required, no numeric constraints."""
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
        for banned in ("minimum", "maximum", "minLength", "maxLength", "multipleOf"):
            assert banned not in node
    walk(llm.RECOMMENDATION_SCHEMA)


# --- response handling ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_thinking_blocks_before_the_answer_are_skipped(monkeypatch):
    FakeAPI(monkeypatch, claude_haiku_5_5=[GOOD])
    provider, got = await recommend(settings())
    assert [(s.title, s.media_type, s.match_score, s.because_of) for s in got] == [
        ("Chef", "movie", 88, "The Bear")]
    assert provider.served_model == "claude-haiku-5-5"


@pytest.mark.asyncio
async def test_refusal_is_reported_not_parsed(monkeypatch):
    FakeAPI(monkeypatch, claude_haiku_5_5=[REFUSED])
    with pytest.raises(llm.ModelRefused, match=r"declined to answer \(general_harms\)"):
        await recommend(settings(llm_fallback_model=""))


@pytest.mark.asyncio
async def test_refusal_without_details_still_counts(monkeypatch):
    FakeAPI(monkeypatch, claude_haiku_5_5=[message(stop_reason="refusal",
                                                   stop_details=None)])
    with pytest.raises(llm.ModelRefused):
        await recommend(settings(llm_fallback_model=""))


def test_a_refusal_is_a_no_suggestions_error_so_the_scan_explains_it():
    assert issubclass(llm.ModelRefused, llm.NoSuggestions)


@pytest.mark.asyncio
async def test_a_reply_cut_off_at_max_tokens_keeps_its_complete_entries(monkeypatch):
    cut = json.dumps({"recommendations": [ROW, ROW | {"title": "Boiling Point"}]})
    cut = cut[:-30]                                 # mid-way through the 2nd entry
    api = FakeAPI(monkeypatch, claude_haiku_5_5=[
        message(text(cut), stop_reason="max_tokens")])
    provider, got = await recommend(settings())
    assert [s.title for s in got] == ["Chef"]
    assert api.models() == ["claude-haiku-5-5"]     # partial, not a fallback
    assert provider.served_model == "claude-haiku-5-5"


# --- fallback model --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refusal_falls_back_to_sonnet_5_5(monkeypatch):
    api = FakeAPI(monkeypatch, claude_haiku_5_5=[REFUSED], claude_sonnet_5_5=[GOOD])
    provider, got = await recommend(settings())
    # A refusal goes straight to the fallback: no stricter retry on the same model.
    assert api.models() == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert provider.served_model == "claude-sonnet-5-5"
    assert got[0].title == "Chef"
    assert "temperature" not in api.sent[1]
    assert api.sent[1]["output_config"]["effort"] == "medium"


@pytest.mark.asyncio
async def test_unusable_answers_fall_back_after_the_stricter_retry(monkeypatch):
    empty = message(text({"recommendations": []}))
    api = FakeAPI(monkeypatch, claude_haiku_5_5=[empty, empty],
                  claude_sonnet_5_5=[GOOD])
    provider, _ = await recommend(settings())
    assert api.models() == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"]
    assert provider.served_model == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_server_errors_fall_back(monkeypatch):
    api = FakeAPI(monkeypatch,
                  claude_haiku_5_5=[(529, {"type": "error",
                                           "error": {"type": "overloaded_error"}})],
                  claude_sonnet_5_5=[GOOD])
    provider, _ = await recommend(settings())
    assert api.models() == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert provider.served_model == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_timeouts_fall_back(monkeypatch):
    api = FakeAPI(monkeypatch, claude_sonnet_5_5=[GOOD])
    inner = api.request

    async def flaky(method, url, *, json=None, **kw):
        if json["model"] == "claude-haiku-5-5":
            raise httpx.ReadTimeout("slow")
        return await inner(method, url, json=json, **kw)

    monkeypatch.setattr(http, "request", flaky)
    provider, _ = await recommend(settings())
    assert provider.served_model == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_bad_key_does_not_fall_back(monkeypatch):
    api = FakeAPI(monkeypatch, claude_haiku_5_5=[(401, {"type": "error"})])
    with pytest.raises(llm.ProviderAPIError) as err:
        await recommend(settings())
    assert err.value.status == 401
    assert api.models() == ["claude-haiku-5-5"]


@pytest.mark.asyncio
async def test_both_models_failing_raises_the_last_error(monkeypatch):
    FakeAPI(monkeypatch, claude_haiku_5_5=[REFUSED], claude_sonnet_5_5=[REFUSED])
    with pytest.raises(llm.ModelRefused, match="claude-sonnet-5-5"):
        await recommend(settings())


@pytest.mark.asyncio
async def test_empty_fallback_turns_it_off(monkeypatch):
    api = FakeAPI(monkeypatch, claude_haiku_5_5=[REFUSED])
    with pytest.raises(llm.ModelRefused):
        await recommend(settings(llm_fallback_model="  "))
    assert api.models() == ["claude-haiku-5-5"]


def test_fallback_is_ignored_when_it_matches_the_primary_or_another_provider():
    assert settings(llm_fallback_model="claude-haiku-5-5").fallback_model == ""
    assert settings(llm_provider="openai", openai_api_key="k").fallback_model == ""
    assert settings().fallback_model == "claude-sonnet-5-5"


# --- 400 self-correction ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_rejected_temperature_is_dropped_and_retried(monkeypatch):
    err = {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "temperature: not supported"}}
    api = FakeAPI(monkeypatch, claude_haiku_4_5=[(400, err), message(text([ROW]))])
    _, got = await recommend(settings(llm_model="claude-haiku-4-5",
                                      llm_fallback_model=""))
    assert "temperature" in api.sent[0] and "temperature" not in api.sent[1]
    assert got[0].title == "Chef"


@pytest.mark.parametrize("message_text,kept", [
    ("output_config.effort: not supported on this model", {"format"}),
    ("output_config.format: schema too complex", {"effort"}),
    ("output_config: Extra inputs are not permitted", None),
])
def test_relax_drops_only_what_the_400_names(message_text, kept):
    payload = {"model": "m", "output_config": {"effort": "low", "format": {"x": 1}}}
    out = llm._relax_anthropic_payload(payload, message_text)
    assert payload["output_config"] == {"effort": "low", "format": {"x": 1}}  # untouched
    if kept is None:
        assert "output_config" not in out
    else:
        assert set(out["output_config"]) == kept


def test_relax_leaves_an_unrelated_400_alone():
    payload = {"model": "m", "max_tokens": 1, "output_config": {"effort": "low"}}
    assert llm._relax_anthropic_payload(payload, "messages: empty") == payload


# --- config -------------------------------------------------------------------------------
@pytest.mark.parametrize("effort,ok", [("low", True), ("Medium", True), ("xhigh", True),
                                       ("max", True), ("turbo", False), ("", False)])
def test_effort_validation(effort, ok):
    problems = [p for p in settings(llm_effort=effort).validate_runtime()
                if "LLM_EFFORT" in p]
    assert (not problems) is ok


def test_effort_is_not_checked_for_other_providers():
    s = settings(llm_provider="ollama", llm_effort="turbo")
    assert not [p for p in s.validate_runtime() if "LLM_EFFORT" in p]


def test_defaults_are_haiku_then_sonnet_5_5():
    fields = Settings.model_fields
    assert fields["llm_model"].default == "claude-haiku-5-5"
    assert fields["llm_fallback_model"].default == "claude-sonnet-5-5"


def test_overloaded_is_retried_as_transient():
    assert 529 in http.TRANSIENT_STATUS


# --- whole scan ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_scan_records_the_model_that_answered(monkeypatch, fresh_db):
    from app import recommender
    from app.models import MediaMeta, WatchedItem

    async def watch_history(user_ids):
        return [WatchedItem(title="The Bear", media_type="tv", year=2022,
                            play_count=3)]

    async def owned():
        return set()

    async def enrich(title, year, media_type):
        return MediaMeta(title=title, media_type=media_type, year=year,
                         tmdb_id=101)

    async def no_auto_request(recs, **_):
        return []

    s = settings()
    monkeypatch.setattr(recommender, "get_settings", lambda: s)
    monkeypatch.setattr(recommender.history, "get_watch_history", watch_history)
    monkeypatch.setattr(recommender.history, "get_library_provider_ids", owned)
    monkeypatch.setattr(recommender.metadata, "enrich", enrich)
    monkeypatch.setattr(recommender, "auto_request", no_auto_request)
    monkeypatch.setattr(recommender, "recs_per_scan", lambda default: 1)
    FakeAPI(monkeypatch, claude_haiku_5_5=[REFUSED], claude_sonnet_5_5=[GOOD])

    progress: dict = {}
    scan = await recommender.run_scan(["u1"], progress=progress)
    assert scan.provider == "anthropic"
    assert scan.model == "claude-sonnet-5-5"
    assert fresh_db.list_scans(1)[0]["model"] == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_a_scan_where_every_model_refuses_fails_with_the_reason(monkeypatch, fresh_db):
    from app import recommender
    from app.models import WatchedItem

    async def watch_history(user_ids):
        return [WatchedItem(title="The Bear", media_type="tv", year=2022)]

    s = settings(llm_fallback_model="")
    monkeypatch.setattr(recommender, "get_settings", lambda: s)
    monkeypatch.setattr(recommender.history, "get_watch_history", watch_history)
    monkeypatch.setattr(recommender, "recs_per_scan", lambda default: 1)
    FakeAPI(monkeypatch, claude_haiku_5_5=[REFUSED])

    with pytest.raises(recommender.ScanError, match="declined to answer"):
        await recommender.run_scan(["u1"])
