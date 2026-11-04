"""LLM provider abstraction: anthropic | openai | google | openwebui | ollama.

ONE call per scan returns all `n` suggestions. Every adapter implements
_complete(system, user) -> raw text; the shared recommend() handles the
prompt, robust JSON extraction and one stricter retry. The Anthropic adapter
adds a second model (LLM_FALLBACK_MODEL) tried when the first one fails.
"""
import copy
import json
import logging
import re

import httpx

from . import http
from .config import Settings
from .models import Suggestion

log = logging.getLogger("jellylook.llm")

MAX_TOKENS = 8000
# Current Claude models think before they answer, and thinking counts toward
# max_tokens. 16K leaves room for that plus a 60-title array, and stays inside
# what a non-streaming request can return before an HTTP timeout.
ANTHROPIC_MAX_TOKENS = 16000

# Appended to the system prompt wherever the provider's JSON mode wants an
# object at the root rather than a bare array.
WRAP_HINT = ' Wrap the array as {"recommendations": [...]}.'


class NoSuggestions(RuntimeError):
    """The provider answered, but with nothing usable.

    Distinct from a transport or auth failure so the scan can report something
    the user can act on rather than "unexpected error".
    """


class ModelRefused(NoSuggestions):
    """The model declined the request (stop_reason "refusal")."""


class ProviderAPIError(RuntimeError):
    """A non-200 answer from a provider's API."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status

# A handful of rejections should nudge the model, not redefine the viewer. Below
# MIN_TASTE_EXAMPLES the taste blocks are omitted entirely — two rejected horror
# films are not evidence that someone dislikes horror — and the cap stops a long
# history from crowding out the profile that does the real work.
MIN_TASTE_EXAMPLES = 3
MAX_TASTE_EXAMPLES = 30

# Exclusions are spent by priority, not first-come. An explicit rejection is
# worth more prompt space than the 150th watched title.
HIDDEN_CAP = 100
WATCHED_CAP = 150


def build_exclude_list(hidden: list[str], watched: list[str],
                       suggested: list[str], budget: int) -> list[str]:
    """Fill a fixed exclusion budget by priority: hidden, watched, then already
    suggested. Each source arrives newest-first and is capped so no one source
    can starve the others; de-duplication is case-insensitive.

    The old code sliced a sorted set at 200, which dropped titles alphabetically
    from T onward — arbitrary, and it had no room for rejections at all.
    """
    out: list[str] = []
    seen: set[str] = set()
    for titles, cap in ((hidden, HIDDEN_CAP), (watched, WATCHED_CAP),
                        (suggested, None)):
        room = budget - len(out)
        if room <= 0:
            break
        allowance = room if cap is None else min(cap, room)
        for title in titles:
            if allowance <= 0:
                break
            key = (title or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(title.strip())
            allowance -= 1
    return out


def _build_prompts(profile: dict, n: int, exclude: list[str],
                   liked: list[str] | None = None,
                   disliked: list[str] | None = None) -> tuple[str, str]:
    system = (
        "You are a recommendation engine for TV shows and movies. You respond "
        "with ONLY a JSON array — no prose, no markdown fences, no commentary."
    )
    taste = ""
    if liked and len(liked) >= MIN_TASTE_EXAMPLES:
        taste += (
            "\n\nThey explicitly LIKED these earlier suggestions — lean toward "
            "what these have in common:\n"
            f"{json.dumps(liked[:MAX_TASTE_EXAMPLES])}"
        )
    if disliked and len(disliked) >= MIN_TASTE_EXAMPLES:
        taste += (
            "\n\nThey explicitly REJECTED these earlier suggestions — avoid "
            "these and close neighbours:\n"
            f"{json.dumps(disliked[:MAX_TASTE_EXAMPLES])}"
        )
    user = (
        f"Here is a viewer's RECENT viewing profile (most recent and most "
        f"replayed titles carry the most weight):\n\n"
        f"{json.dumps(profile, indent=1)}"
        f"{taste}\n\n"
        f"Recommend exactly {n} titles they have NOT seen. Never suggest "
        f"anything in this exclude list (or any trivial variant of it):\n"
        f"{json.dumps(exclude)}\n\n"
        "Rules:\n"
        f"- Return a JSON array of exactly {n} objects, nothing else.\n"
        '- Each object: {"title": str, "year": int, "type": "movie"|"tv", '
        '"reason": one-line str, "match_score": int 0-100 (estimated similarity '
        'to the recent viewing), "because_of": str (one specific watched seed '
        "title from the profile that motivates this pick)}.\n"
        "- Aim for a good mix of movies and TV.\n"
        "- because_of MUST be a title that appears in the profile.\n"
        "- Output ONLY the JSON array."
    )
    return system, user


def _extract_json(text: str) -> list[dict]:
    """Strip fences, find the first JSON array/object, tolerate wrappers."""
    if not text:
        return []
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    # direct parse first
    for candidate in (cleaned, _slice_json(cleaned)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
    return _salvage_truncated(cleaned)


def _salvage_truncated(text: str) -> list[dict]:
    """The complete objects of an array that was cut off part-way.

    A reply that hits max_tokens ends mid-object; keeping the entries before
    the cut turns a failed scan into a partial one.
    """
    start = text.find("[")
    if start == -1:
        return []
    decoder = json.JSONDecoder()
    out: list[dict] = []
    i = start + 1
    while i < len(text):
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text) or text[i] != "{":
            break
        try:
            obj, i = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _slice_json(text: str) -> str | None:
    """Return the substring from the first '[' or '{' to its matching close."""
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    opener = text[start]
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _to_suggestions(raw: list[dict], n: int) -> list[Suggestion]:
    out: list[Suggestion] = []
    for item in raw:
        title = item.get("title")
        if not title:
            continue
        media_type = str(item.get("type") or item.get("media_type") or "").lower()
        if media_type in ("show", "series", "tv show"):
            media_type = "tv"
        if media_type not in ("movie", "tv"):
            continue
        try:
            year = int(item["year"]) if item.get("year") else None
        except (ValueError, TypeError):
            year = None
        score = item.get("match_score")
        try:
            score = max(0, min(100, int(score))) if score is not None else None
        except (ValueError, TypeError):
            score = None
        out.append(Suggestion(
            title=str(title).strip(),
            media_type=media_type,
            year=year,
            reason=str(item["reason"]).strip() if item.get("reason") else None,
            match_score=score,
            because_of=str(item["because_of"]).strip() if item.get("because_of") else None,
        ))
        if len(out) >= n:
            break
    return out


class BaseProvider:
    def __init__(self, settings: Settings):
        self.s = settings
        # The model behind the last successful recommend(). Only the Anthropic
        # adapter can differ from LLM_MODEL, when its fallback answered.
        self.served_model = settings.llm_model

    async def _complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    async def recommend(
        self, profile: dict, n: int, exclude: list[str],
        liked: list[str] | None = None, disliked: list[str] | None = None,
    ) -> list[Suggestion]:
        system, user = _build_prompts(profile, n, exclude, liked, disliked)
        return await self._recommend_once(system, user, n, self.s.llm_model)

    async def _recommend_once(self, system: str, user: str, n: int,
                              model: str) -> list[Suggestion]:
        """One answer from the current model, plus one stricter retry if it
        was not parseable. Raises NoSuggestions when both come back empty."""
        text = await self._complete(system, user)
        suggestions = _to_suggestions(_extract_json(text), n)
        if suggestions:
            return suggestions
        log.warning("%s returned unparseable output — one stricter retry",
                    type(self).__name__)
        strict = user + ("\n\nIMPORTANT: your previous answer was not valid "
                         "JSON. Respond with ONLY the raw JSON array. No text "
                         "before or after it.")
        text = await self._complete(system, strict)
        suggestions = _to_suggestions(_extract_json(text), n)
        if not suggestions:
            raise NoSuggestions(
                f"{self.s.llm_provider}/{model} returned no usable "
                f"recommendations. Either the model isn't following the JSON "
                f"format, or the exclude list left it nothing to suggest — try "
                f"a larger model, or lower EXCLUDE_BUDGET."
            )
        return suggestions


# Claude models from before Opus 4.7 / Sonnet 5 / Haiku 5.5. They accept
# `temperature`, and predate effort levels, so they get the original request.
# Everything newer - Haiku 5.5, Sonnet 5.5, and whatever comes next - rejects a
# non-default temperature and gets effort plus structured outputs instead.
_LEGACY_CLAUDE = re.compile(
    r"^claude-(?:instant|2|3)"
    r"|^claude-(?:opus|sonnet|haiku)-4(?:-[0156])?(?:-\d{8})?$"
)


def is_legacy_claude(model: str) -> bool:
    return bool(_LEGACY_CLAUDE.match((model or "").strip().lower()))


_SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "year": {"type": "integer"},
        "type": {"type": "string", "enum": ["movie", "tv"]},
        "reason": {"type": "string"},
        "match_score": {"type": "integer"},
        "because_of": {"type": "string"},
    },
    "required": ["title", "year", "type", "reason", "match_score", "because_of"],
    "additionalProperties": False,
}

# Structured outputs guarantee the answer parses. The root has to be an object,
# which _extract_json already unwraps.
RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {"type": "array", "items": _SUGGESTION_SCHEMA},
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}

# Same key, same answer: trying another model would only fail the same way.
_NO_FALLBACK_STATUS = {401, 403}


def _worth_falling_back(exc: Exception) -> bool:
    if isinstance(exc, NoSuggestions):          # includes ModelRefused
        return True
    if isinstance(exc, ProviderAPIError):
        return exc.status not in _NO_FALLBACK_STATUS
    return isinstance(exc, httpx.HTTPError)     # timeouts, connection errors


def _relax_anthropic_payload(payload: dict, body: str) -> dict:
    """Drop the field a 400 complains about, so one retry can succeed.

    The capability check in is_legacy_claude() is a guess for model names it
    has not seen; this keeps a wrong guess from failing the scan.
    """
    retry = copy.deepcopy(payload)
    body = body.lower()
    if "temperature" in body:
        retry.pop("temperature", None)
    config = retry.get("output_config")
    if config is not None:
        if "effort" in body:
            config.pop("effort", None)
        elif "format" in body or "schema" in body:
            config.pop("format", None)
        elif "output_config" in body:
            config.clear()
        if not config:
            retry.pop("output_config")
    return retry


class AnthropicProvider(BaseProvider):
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._model = settings.llm_model

    async def recommend(
        self, profile: dict, n: int, exclude: list[str],
        liked: list[str] | None = None, disliked: list[str] | None = None,
    ) -> list[Suggestion]:
        system, user = _build_prompts(profile, n, exclude, liked, disliked)
        models = [self.s.llm_model]
        if self.s.fallback_model:
            models.append(self.s.fallback_model)
        for i, model in enumerate(models):
            self._model = model
            try:
                suggestions = await self._recommend_once(system, user, n, model)
            except (NoSuggestions, ProviderAPIError, httpx.HTTPError) as exc:
                if i + 1 == len(models) or not _worth_falling_back(exc):
                    raise
                log.warning("%s failed (%s: %s) — trying fallback model %s",
                            model, type(exc).__name__, exc, models[i + 1])
                continue
            self.served_model = model
            return suggestions
        raise AssertionError("unreachable: the model list is never empty")

    def _payload(self, system: str, user: str) -> dict:
        model = self._model
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": user}],
        }
        if is_legacy_claude(model):
            payload.update(max_tokens=MAX_TOKENS, system=system,
                           temperature=self.s.llm_temperature)
        else:
            # No `thinking` field: adaptive thinking is the default, and
            # effort is the control for how much of it happens.
            payload.update(
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=system + WRAP_HINT,
                output_config={
                    "effort": self.s.effort,
                    "format": {"type": "json_schema",
                               "schema": RECOMMENDATION_SCHEMA},
                },
            )
        return payload

    async def _post(self, payload: dict):
        return await http.request(
            "POST", self.URL,
            headers={
                "x-api-key": self.s.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=http.LLM_TIMEOUT,
        )

    async def _complete(self, system: str, user: str) -> str:
        payload = self._payload(system, user)
        resp = await self._post(payload)
        if resp.status_code == 400:
            retry = _relax_anthropic_payload(payload, resp.text[:1000])
            if retry != payload:
                log.info("Anthropic rejected the request for %s (400) — "
                         "retrying with adjusted parameters", self._model)
                resp = await self._post(retry)
        data = _json_or_error(resp, "Anthropic")
        stop = data.get("stop_reason")
        if stop == "refusal":
            # Branch on stop_reason; stop_details is informational and may be null.
            details = data.get("stop_details")
            category = details.get("category") if isinstance(details, dict) else None
            raise ModelRefused(
                f"anthropic/{self._model} declined to answer"
                f"{f' ({category})' if category else ''}. Scan again, or set "
                f"LLM_FALLBACK_MODEL to a model to try next."
            )
        if stop == "max_tokens":
            log.warning("%s stopped at max_tokens — the answer may be cut short; "
                        "lower RECS_PER_SCAN or LLM_EFFORT", self._model)
        # Read by type: current models can open with `thinking` blocks.
        blocks = data.get("content") or []
        return "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")


class _OpenAICompatible(BaseProvider):
    """Shared implementation for OpenAI and Open WebUI (OpenAI-compatible)."""
    label = "OpenAI"

    def _endpoint(self) -> tuple[str, str]:
        raise NotImplementedError

    async def _complete(self, system: str, user: str) -> str:
        url, key = self._endpoint()
        payload = {
            "model": self.s.llm_model,
            "temperature": self.s.llm_temperature,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system",
                 "content": system + WRAP_HINT},
                {"role": "user", "content": user},
            ],
        }
        resp = await self._post(url, key, payload)
        if resp.status_code == 400:
            # Compatibility fallbacks: newer OpenAI models want
            # max_completion_tokens; some Open WebUI / proxy backends reject
            # response_format. Adjust once and retry.
            body = resp.text[:500]
            retry = dict(payload)
            if "max_tokens" in body and "max_completion_tokens" not in retry:
                retry.pop("max_tokens", None)
                retry["max_completion_tokens"] = MAX_TOKENS
            if "response_format" in body:
                retry.pop("response_format", None)
            if retry != payload:
                log.info("%s rejected the request (400) — retrying with "
                         "adjusted parameters", self.label)
                resp = await self._post(url, key, retry)
        data = _json_or_error(resp, self.label)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"{self.label} returned no choices")
        return (choices[0].get("message") or {}).get("content") or ""

    async def _post(self, url: str, key: str, payload: dict):
        return await http.request(
            "POST", url,
            headers={"Authorization": f"Bearer {key}",
                     "content-type": "application/json"},
            json=payload,
            timeout=http.LLM_TIMEOUT,
        )


class OpenAIProvider(_OpenAICompatible):
    label = "OpenAI"

    def _endpoint(self) -> tuple[str, str]:
        return (f"{self.s.openai_base_url.rstrip('/')}/chat/completions",
                self.s.openai_api_key)


class OpenWebUIProvider(_OpenAICompatible):
    label = "Open WebUI"

    def _endpoint(self) -> tuple[str, str]:
        return (f"{self.s.openwebui_base_url.rstrip('/')}/api/chat/completions",
                self.s.openwebui_api_key)


class GoogleProvider(BaseProvider):
    async def _complete(self, system: str, user: str) -> str:
        url = (f"{self.s.google_base_url.rstrip('/')}/models/"
               f"{self.s.llm_model}:generateContent")
        resp = await http.request(
            "POST", url,
            headers={"x-goog-api-key": self.s.google_api_key,
                     "content-type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": self.s.llm_temperature,
                    "maxOutputTokens": MAX_TOKENS,
                    "responseMimeType": "application/json",
                },
            },
            timeout=http.LLM_TIMEOUT,
        )
        data = _json_or_error(resp, "Google AI")
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Google AI returned no candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts)


class OllamaProvider(BaseProvider):
    async def _complete(self, system: str, user: str) -> str:
        resp = await http.request(
            "POST", f"{self.s.ollama_base_url.rstrip('/')}/api/chat",
            json={
                "model": self.s.llm_model,
                "format": "json",
                "stream": False,
                "think": False,
                "options": {"temperature": self.s.llm_temperature,
                            "num_predict": MAX_TOKENS},
                "messages": [
                    {"role": "system",
                     "content": system + WRAP_HINT},
                    {"role": "user", "content": user},
                ],
            },
            timeout=http.LLM_TIMEOUT,
        )
        data = _json_or_error(resp, "Ollama")
        return (data.get("message") or {}).get("content") or ""


def _json_or_error(resp, label: str) -> dict:
    if resp.status_code != 200:
        # Bodies can carry useful error info but never secrets we sent.
        snippet = resp.text[:300]
        raise ProviderAPIError(f"{label} API error {resp.status_code}: {snippet}",
                               resp.status_code)
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned non-JSON response") from exc


def get_provider(settings: Settings) -> BaseProvider:
    mapping = {
        "anthropic": AnthropicProvider,
        "openai": OpenAIProvider,
        "google": GoogleProvider,
        "openwebui": OpenWebUIProvider,
        "ollama": OllamaProvider,
    }
    key = (settings.llm_provider or "").lower()
    if key not in mapping:
        raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
    return mapping[key](settings)
