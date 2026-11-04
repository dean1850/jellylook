# Plan: Claude Haiku 5.5 + Sonnet 5.5

## Goal

Run jellylook's Anthropic provider on the current Claude models:

- **Claude Haiku 5.5** (`claude-haiku-5-5`) — the new default `LLM_MODEL`. One
  call per scan, cheap ($0.10 / $0.50 per MTok under 100K prompt tokens) and fast.
- **Claude Sonnet 5.5** (`claude-sonnet-5-5`) — the new default
  `LLM_FALLBACK_MODEL`. Used only when the primary model refuses, errors out, or
  returns nothing usable, so a bad Haiku answer no longer costs the whole scan.

## What breaks if only the model string changes

| Change on the 5.5 models | Effect on today's request | Fix |
|---|---|---|
| Non-default `temperature` returns a 400 | Every scan fails (`LLM_TEMPERATURE=0.7`) | Send `temperature` only to older Claude models that accept it |
| Adaptive thinking is on by default and counts toward `max_tokens` | 8,000 tokens shared between thinking and 60 JSON objects can truncate the array | `max_tokens` 16,000 and an explicit `output_config.effort` (`LLM_EFFORT`, default `medium`) |
| Responses can start with `thinking` blocks | Already safe — text blocks are read by `type` | Keep, covered by a test |
| `stop_reason: "refusal"` (new safety classifiers) | An empty answer reported as "no usable recommendations" | Detect it, report it, and try the fallback model |

## Improvements that come with it

- **Structured outputs** (`output_config.format`, JSON schema) on current Claude
  models: the response is guaranteed to match the schema, so the "not valid
  JSON" retry stops being needed on the hosted path.
- **One self-correcting retry on a 400**: if the API rejects `temperature`,
  `effort` or the output format for a model the capability check didn't
  anticipate, the request is retried once without that field (same pattern the
  OpenAI adapter already uses).
- **529 "overloaded"** joins the transient-status retry set.
- **The scan history records the model that actually answered**, so a fallback
  shows up in Settings → Status and the scan list.

## Backwards compatibility

- Older Claude models (`claude-3*`, `claude-*-4`, `-4-0`, `-4-1`, `-4-5`, `-4-6`)
  keep the previous request shape: `temperature` sent, no `effort`, no output
  format. A `.env` still pinned to `claude-haiku-4-5` behaves as before.
- `LLM_FALLBACK_MODEL=` (empty) turns the fallback off. It only applies when
  `LLM_PROVIDER=anthropic`, and is ignored when it equals `LLM_MODEL`.
- `anthropic-version` stays `2023-06-01`; the `2026-10-21` API version is a
  separate migration.

## Also fixed

- `python -m app.selftest` passed a `set` as the exclude list, which
  `json.dumps` cannot serialise — the self-test crashed before calling the model.

## Files

`app/config.py`, `app/llm.py`, `app/http.py`, `app/recommender.py`,
`app/selftest.py`, `app/main.py`, `app/static/app.js`, `app/static/index.html`,
`.env.example`, `README.md`, `tests/test_llm.py`.
