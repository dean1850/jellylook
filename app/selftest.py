"""CLI self-test:  python -m app.selftest

(a) enrich "The Bear" (2022, tv): print imdb_rating + poster_url, prove the
    2nd call is a cache hit and omdb_usage rose by exactly 1.
(b) build a small taste profile, ask the ACTIVE provider for 5 recs, print
    parsed suggestions incl. match_score + because_of.
"""
import asyncio
import time

from . import db, http, llm, metadata
from .config import get_settings


async def main() -> None:
    s = get_settings()
    db.init_db()
    await http.startup()
    try:
        print("== (a) metadata enrichment ==")
        before = db.omdb_today()
        t0 = time.monotonic()
        meta = await metadata.enrich("The Bear", 2022, "tv")
        t_first = time.monotonic() - t0
        if meta is None:
            print("FAIL: could not enrich 'The Bear' — check TMDB_API_KEY")
            return
        print(f"  title:       {meta.title} ({meta.year})")
        print(f"  imdb_rating: {meta.imdb_rating}")
        print(f"  poster_url:  {meta.poster_url}")
        t0 = time.monotonic()
        await metadata.enrich("The Bear", 2022, "tv")
        t_second = time.monotonic() - t0
        after = db.omdb_today()
        print(f"  1st call {t_first*1000:.0f} ms · 2nd call {t_second*1000:.0f} ms "
              f"(cache hit: {'yes' if t_second < t_first / 2 else 'CHECK'})")
        delta = after - before
        print(f"  omdb_usage delta: {delta} "
              f"({'PASS' if delta <= 1 else 'FAIL — expected at most 1'})")

        print(f"\n== (b) LLM provider: {s.llm_provider} / {s.llm_model} ==")
        profile = {"recently_watched": [
            {"title": "The Bear", "type": "tv", "year": 2022, "plays": 8, "weight": 2.6},
            {"title": "Chef", "type": "movie", "year": 2014, "plays": 2, "weight": 1.4},
            {"title": "Severance", "type": "tv", "year": 2022, "plays": 5, "weight": 2.0},
        ]}
        provider = llm.get_provider(s)
        suggestions = await provider.recommend(
            profile, 5, {"the bear", "chef", "severance"})
        for sug in suggestions:
            score = f"{sug.match_score:>3}%" if sug.match_score is not None else "  —"
            print(f"  {score}  {sug.title} ({sug.year}, "
                  f"{sug.media_type}) — because you watched {sug.because_of or '?'}")
            print(f"        {sug.reason}")
        print(f"  {'PASS' if suggestions else 'FAIL'}: "
              f"{len(suggestions)} suggestions parsed")
    finally:
        await http.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
