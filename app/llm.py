"""LLM provider abstraction: anthropic | openai | google | openwebui | ollama.

ONE call per scan returns all `n` suggestions. Every adapter implements
_complete(system, user) -> raw text; the shared recommend() handles the
prompt, robust JSON extraction and one stricter retry.
"""
import json
import logging
import re

from . import http
from .config import Settings
from .models import Suggestion

log = logging.getLogger("jellylook.llm")

MAX_TOKENS = 8000


def _build_prompts(profile: dict, n: int, exclude: set[str]) -> tuple[str, str]:
    system = (
        "You are a recommendation engine for TV shows and movies. You respond "
        "with ONLY a JSON array — no prose, no markdown fences, no commentary."
    )
    exclude_list = sorted(exclude)[:200]
    user = (
        f"Here is a viewer's RECENT viewing profile (most recent and most "
        f"replayed titles carry the most weight):\n\n"
        f"{json.dumps(profile, indent=1)}\n\n"
        f"Recommend exactly {n} titles they have NOT seen. Never suggest "
        f"anything in this exclude list (or any trivial variant of it):\n"
        f"{json.dumps(exclude_list)}\n\n"
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
    return []


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

    async def _complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    async def recommend(
        self, profile: dict, n: int, exclude: set[str]
    ) -> list[Suggestion]:
        system, user = _build_prompts(profile, n, exclude)
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
            raise RuntimeError("LLM did not return parseable recommendations")
        return suggestions


class AnthropicProvider(BaseProvider):
    async def _complete(self, system: str, user: str) -> str:
        resp = await http.request(
            "POST", "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.s.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.s.llm_model,
                "max_tokens": MAX_TOKENS,
                "temperature": self.s.llm_temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=http.LLM_TIMEOUT,
        )
        data = _json_or_error(resp, "Anthropic")
        blocks = data.get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


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
                 "content": system + ' Wrap the array as {"recommendations": [...]}.'},
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
                     "content": system + ' Wrap the array as {"recommendations": [...]}.'},
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
        raise RuntimeError(f"{label} API error {resp.status_code}: {snippet}")
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
