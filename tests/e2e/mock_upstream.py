"""Fake upstream services so the real jellylook can run without any API keys.

One FastAPI app stands in for Jellystat, Jellyfin, TMDb, OMDb, Seerr and an
OpenAI-compatible LLM, each under its own path prefix. Everything it returns is
invented — the titles, the artwork and the ratings — so the end-to-end tests and
the README screenshots carry no third-party content.

Run standalone:  python -m tests.e2e.mock_upstream [port]
"""
import base64
import hashlib
import json
import random
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="jellylook mock upstream")

USERS = [{"id": "u1", "name": "dean"}, {"id": "u2", "name": "robert"}]

# What the fake viewers have "watched" — the seeds a scan reasons from.
HISTORY = {
    "u1": [
        ("The Night Ledger", "tv", 2022, 14),
        ("Harbour Patrol", "movie", 2021, 3),
        ("Cold Meridian", "movie", 2019, 2),
        ("Tin Rooftops", "tv", 2023, 9),
    ],
    "u2": [
        ("Lantern Sound", "tv", 2020, 6),
        ("Harbour Patrol", "movie", 2021, 1),
    ],
}

# The catalogue the fake LLM recommends from. Deliberately varied across genre,
# decade, runtime, rating, library ownership and streaming availability so the
# filter tests have something to bite on.
#   title, type, year, genres, runtime, imdb, tmdb, streaming, trailer, owned
CATALOGUE = [
    ("Midnight Dispatch",   "tv",    2024, [(18, "Drama"), (53, "Thriller")], 52, 8.6, 8.1, True,  True,  False),
    ("The Copper Line",     "movie", 2023, [(53, "Thriller")],                118, 8.2, 7.8, True,  True,  False),
    ("Static Fields",       "tv",    2025, [(878, "Science Fiction")],         48, 8.9, 8.4, False, True,  True),
    ("Southern Approach",   "movie", 2022, [(18, "Drama"), (36, "History")],  134, 7.9, 7.5, True,  False, False),
    ("Glasshouse Protocol", "tv",    2024, [(53, "Thriller"), (9648, "Mystery")], 44, 8.1, 7.9, False, True, False),
    ("The Long Quiet",      "movie", 2021, [(18, "Drama")],                    96, 7.7, 7.3, True,  True,  False),
    ("Paper Harbour",       "tv",    2023, [(35, "Comedy")],                   28, 7.8, 7.6, True,  False, False),
    ("Ninth Signal",        "movie", 2025, [(878, "Science Fiction"), (53, "Thriller")], 142, 7.4, 7.2, False, True, False),
    ("Winter Ferry",        "movie", 2018, [(18, "Drama"), (10749, "Romance")], 105, 7.1, 6.9, True,  False, False),
    ("Salt and Iron",       "tv",    2020, [(10759, "Action & Adventure")],     55, 6.8, 6.6, False, True,  False),
    ("The Quiet Ledger",    "movie", 1998, [(80, "Crime")],                    127, 8.4, 8.0, False, False, False),
    ("Marlow Street",       "tv",    2016, [(80, "Crime"), (18, "Drama")],      50, 8.0, 7.7, True,  True,  False),
]

BY_TITLE = {c[0].lower(): c for c in CATALOGUE}
BY_ID = {1000 + i: c for i, c in enumerate(CATALOGUE)}
TITLE_TO_ID = {c[0].lower(): 1000 + i for i, c in enumerate(CATALOGUE)}

PROVIDERS = [
    {"provider_name": "Streamline", "logo_path": "/streamline.png"},
    {"provider_name": "Kettle+", "logo_path": "/kettle.png"},
]


# --- generated artwork ------------------------------------------------------------
def _poster_svg(title: str, kind: str = "poster") -> bytes:
    """Deterministic abstract artwork, so screenshots are stable run to run."""
    seed = int(hashlib.sha256(title.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    palettes = [
        ("#0e3a4d", "#1b5e7a", "#00A4DC"), ("#3a1b4d", "#5e2b7a", "#AA5CC3"),
        ("#4d2a0e", "#7a4a1b", "#e0863a"), ("#0e4d33", "#1b7a55", "#3adc9a"),
        ("#4d0e1e", "#7a1b33", "#dc3a5e"), ("#2a2a4d", "#44447a", "#7a7adc"),
        ("#4d3f0e", "#7a651b", "#dcc23a"), ("#0e2a4d", "#1b447a", "#3a8adc"),
    ]
    c1, c2, accent = palettes[seed % len(palettes)]
    if kind == "logo":
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="92" height="92">'
            f'<rect width="92" height="92" rx="18" fill="{c2}"/>'
            f'<circle cx="46" cy="46" r="22" fill="{accent}" opacity="0.85"/></svg>'
        ).encode()

    # Greedy wrap, then scale the type to whatever the longest line needs, so a
    # long title stays inside the poster frame instead of bleeding off the edge.
    lines: list[str] = []
    for word in title.upper().split():
        if lines and len(lines[-1]) + 1 + len(word) <= 13:
            lines[-1] += " " + word
        else:
            lines.append(word)
    longest = max((len(line) for line in lines), default=1)
    size = max(14, min(26, int(232 / (0.62 * longest))))
    step = int(size * 1.3)
    start = 220 - (len(lines) - 1) * step // 2
    tspans = "".join(
        f'<tspan x="150" dy="{0 if j == 0 else step}">{line}</tspan>'
        for j, line in enumerate(lines)
    )
    shapes = "".join(
        f'<circle cx="{rng.randint(20, 280)}" cy="{rng.randint(20, 430)}" '
        f'r="{rng.randint(30, 90)}" fill="{accent}" opacity="0.08"/>'
        for _ in range(5)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" '
        f'viewBox="0 0 300 450"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/>'
        f'</linearGradient></defs><rect width="300" height="450" fill="url(#g)"/>{shapes}'
        f'<rect x="24" y="24" width="252" height="402" fill="none" stroke="{accent}" '
        f'stroke-opacity="0.35" stroke-width="1.5"/>'
        f'<text x="150" y="{start}" text-anchor="middle" font-family="Georgia,serif" '
        f'font-size="{size}" font-weight="bold" fill="#ffffff" fill-opacity="0.92" '
        f'letter-spacing="1.5">{tspans}</text>'
        f'<text x="150" y="400" text-anchor="middle" font-family="sans-serif" '
        f'font-size="11" fill="#ffffff" fill-opacity="0.4" letter-spacing="4">'
        f'A JELLYLOOK DEMO</text></svg>'
    ).encode()


@app.get("/img/{path:path}")
async def image(path: str):
    name = path.rsplit("/", 1)[-1].removesuffix(".jpg").removesuffix(".png")
    kind = "logo" if name in ("streamline", "kettle") else "poster"
    return Response(_poster_svg(name.replace("_", " "), kind),
                    media_type="image/svg+xml")


def data_uri(title: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(_poster_svg(title)).decode()


# --- jellystat ---------------------------------------------------------------------
@app.get("/jellystat/api/getAllUsers")
async def js_users():
    return [{"UserId": u["id"], "UserName": u["name"]} for u in USERS]


@app.get("/jellystat/api/getUserHistory")
async def js_history(userid: str = "", size: int = 200):
    rows = []
    for title, kind, year, plays in HISTORY.get(userid, []):
        row = {"NowPlayingItemName": title, "ProductionYear": year,
               "PlayCount": plays, "ActivityDateInserted": "2026-07-20T10:00:00Z"}
        if kind == "tv":
            row["SeriesName"] = title
        else:
            row["ItemType"] = "Movie"
        rows.append(row)
    return rows


# --- jellyfin ----------------------------------------------------------------------
@app.get("/jellyfin/Users")
async def jf_users():
    return [{"Id": u["id"], "Name": u["name"]} for u in USERS]


@app.get("/jellyfin/Items")
async def jf_items():
    """Ownership set — only the catalogue entries flagged as owned."""
    owned = [c for c in CATALOGUE if c[9]]
    return {"Items": [
        {"Name": c[0], "Type": "Series" if c[1] == "tv" else "Movie",
         "ProviderIds": {"Tmdb": str(TITLE_TO_ID[c[0].lower()])}}
        for c in owned
    ]}


# --- tmdb --------------------------------------------------------------------------
# The api_key / year params are declared explicitly rather than swallowed by
# **kwargs: FastAPI reads **kwargs as a required query field called "_".
@app.get("/tmdb/search/{kind}")
async def tmdb_search(kind: str, query: str = "", api_key: str = "",
                      year: int | None = None,
                      first_air_date_year: int | None = None):
    entry = BY_TITLE.get(query.strip().lower())
    if not entry or (entry[1] == "tv") != (kind == "tv"):
        return {"results": []}
    tmdb_id = TITLE_TO_ID[entry[0].lower()]
    return {"results": [{"id": tmdb_id, "poster_path": f"/{_slug(entry[0])}.jpg"}]}


def _slug(title: str) -> str:
    return title.lower().replace(" ", "_")


@app.get("/tmdb/{kind}/{tmdb_id}")
async def tmdb_detail(kind: str, tmdb_id: int, append_to_response: str = "",
                      api_key: str = ""):
    entry = BY_ID.get(tmdb_id)
    if not entry:
        return JSONResponse({"success": False}, status_code=404)
    title, media, year, genres, runtime, imdb, tmdb_score, streams, trailer, _owned = entry
    slug = _slug(title)
    detail = {
        "id": tmdb_id,
        "vote_average": tmdb_score,
        "overview": f"{title} — a fictional title generated for the jellylook demo.",
        "genres": [{"id": gid, "name": name} for gid, name in genres],
        "poster_path": f"/{slug}.jpg",
        "backdrop_path": f"/{slug}_bd.jpg",
        "original_language": "en",
        "popularity": 40.0 - tmdb_id % 20,
        "external_ids": {"imdb_id": f"tt{tmdb_id}"},
    }
    if kind == "tv":
        detail["name"] = title
        detail["first_air_date"] = f"{year}-03-01"
        detail["episode_run_time"] = [runtime]
    else:
        detail["title"] = title
        detail["release_date"] = f"{year}-03-01"
        detail["runtime"] = runtime

    if "videos" in append_to_response:
        detail["videos"] = {"results": (
            [{"site": "YouTube", "type": "Trailer", "official": True,
              "key": f"demo{tmdb_id}"}] if trailer else []
        )}
    if "watch/providers" in append_to_response:
        detail["watch/providers"] = {"results": (
            {"AU": {"link": "https://www.themoviedb.org/watch",
                    "flatrate": PROVIDERS}} if streams else {}
        )}
    return detail


@app.get("/tmdb/{kind}/{tmdb_id}/watch/providers")
async def tmdb_providers(kind: str, tmdb_id: int, api_key: str = ""):
    entry = BY_ID.get(tmdb_id)
    if not entry or not entry[7]:
        return {"id": tmdb_id, "results": {}}
    return {"id": tmdb_id,
            "results": {"AU": {"link": "https://www.themoviedb.org/watch",
                               "flatrate": PROVIDERS}}}


# --- omdb --------------------------------------------------------------------------
@app.get("/omdb")
async def omdb(i: str = "", apikey: str = ""):
    try:
        entry = BY_ID.get(int(i.removeprefix("tt")))
    except ValueError:
        entry = None
    if not entry:
        return {"Response": "False"}
    return {"Response": "True", "imdbRating": str(entry[5]),
            "Plot": f"{entry[0]} — demo plot.", "Genre": entry[3][0][1]}


# --- seerr -------------------------------------------------------------------------
_requested: set[str] = set()


@app.get("/seerr/api/v1/status")
async def seerr_status():
    return {"version": "mock"}


@app.get("/seerr/api/v1/{kind}/{tmdb_id}")
async def seerr_media(kind: str, tmdb_id: int):
    body: dict = {"id": tmdb_id}
    if kind == "tv":
        body["seasons"] = [
            {"seasonNumber": n, "episodeCount": 8 + n, "name": f"Season {n}"}
            for n in range(1, 4)
        ]
    if f"{kind}:{tmdb_id}" in _requested:
        body["mediaInfo"] = {"status": 2}
    return body


@app.post("/seerr/api/v1/request")
async def seerr_request(payload: dict):
    _requested.add(f"{payload.get('mediaType')}:{payload.get('mediaId')}")
    return JSONResponse({"id": 1}, status_code=201)


# --- OpenAI-compatible LLM -----------------------------------------------------------
@app.post("/llm/v1/chat/completions")
async def llm(request: Request):
    """Return catalogue titles, obeying the prompt's exclusions.

    A mock that ignored the exclude list would let the feedback tests pass
    whether or not the real exclusion logic worked, so it honours them — with
    the same weighting a real model applies:

      * the REJECTED block is treated as hard. Nothing from it comes back.
      * the exclude list is soft. Real models drift back to previously offered
        titles once they run out of ideas, and this catalogue is 12 entries
        deep, so exhausting it must degrade rather than return an empty array.

    The distinction is what makes the "hide survives a rescan" test meaningful:
    it passes only because hidden titles are genuinely suppressed, not because
    the mock ran dry.
    """
    payload = await request.json()
    prompt = " ".join(m.get("content", "") for m in payload.get("messages", []))
    wanted = _requested_count(prompt)
    banned = {t.lower() for t in _rejected_titles(prompt)}
    soft = {t.lower() for t in _listed_titles(prompt)} - banned

    picks: list[dict] = []
    for pool in (0, 1):  # pass 0 respects the soft list; pass 1 ignores it
        for title, kind, year, *_rest in CATALOGUE:
            key = title.lower()
            if key in banned or any(p["title"] == title for p in picks):
                continue
            if pool == 0 and key in soft:
                continue
            picks.append({
                "title": title, "year": year, "type": kind,
                "reason": "Shares its mood with what you've been watching.",
                "match_score": max(40, 96 - len(picks) * 3),
                "because_of": "The Night Ledger",
            })
            if len(picks) >= wanted:
                break
        if len(picks) >= wanted:
            break
    return {"choices": [{"message": {"content":
            json.dumps({"recommendations": picks})}}]}


def _requested_count(prompt: str) -> int:
    marker = "Recommend exactly "
    idx = prompt.find(marker)
    if idx != -1:
        digits = prompt[idx + len(marker):].split(" ", 1)[0]
        if digits.isdigit():
            return int(digits)
    return 60


def _rejected_titles(prompt: str) -> list[str]:
    """Titles from the REJECTED taste block only."""
    idx = prompt.find("explicitly REJECTED")
    if idx == -1:
        return []
    start = prompt.find("[", idx)
    end = prompt.find("]", start)
    if start == -1 or end == -1:
        return []
    return _strip_years(prompt[start:end + 1])


def _listed_titles(prompt: str) -> list[str]:
    """Every title in every JSON array in the prompt (the union of exclusions)."""
    out: list[str] = []
    for chunk in prompt.split("["):
        end = chunk.find("]")
        if end != -1:
            out.extend(_strip_years("[" + chunk[:end + 1]))
    return out


def _strip_years(blob: str) -> list[str]:
    try:
        values = json.loads(blob)
    except json.JSONDecodeError:
        return []
    # Entries look like "Title (2021)"; the catalogue is keyed on bare titles.
    return [v.rsplit(" (", 1)[0] if v.endswith(")") else v
            for v in values if isinstance(v, str)]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1",
                port=int(sys.argv[1]) if len(sys.argv) > 1 else 9099,
                log_level="warning")
