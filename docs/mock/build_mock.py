"""Generate mock.html — the jellylook UI with staged demo data for screenshots."""
import base64, html, pathlib, random

random.seed(11)

PALETTES = [
    ("#0e3a4d", "#1b5e7a", "#00A4DC"), ("#3a1b4d", "#5e2b7a", "#AA5CC3"),
    ("#4d2a0e", "#7a4a1b", "#e0863a"), ("#0e4d33", "#1b7a55", "#3adc9a"),
    ("#4d0e1e", "#7a1b33", "#dc3a5e"), ("#2a2a4d", "#44447a", "#7a7adc"),
    ("#4d3f0e", "#7a651b", "#dcc23a"), ("#0e2a4d", "#1b447a", "#3a8adc"),
]

def poster(title, i):
    c1, c2, accent = PALETTES[i % len(PALETTES)]
    words = title.split()
    lines = [" ".join(words[:2]), " ".join(words[2:])] if len(words) > 2 else [title]
    tspans = "".join(
        f'<tspan x="150" dy="{0 if j==0 else 34}">{html.escape(l.upper())}</tspan>'
        for j, l in enumerate(lines) if l
    )
    shapes = "".join(
        f'<circle cx="{random.randint(20,280)}" cy="{random.randint(20,430)}" '
        f'r="{random.randint(30,90)}" fill="{accent}" opacity="0.08"/>' for _ in range(5)
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>
<rect width="300" height="450" fill="url(#g)"/>{shapes}
<rect x="24" y="24" width="252" height="402" fill="none" stroke="{accent}" stroke-opacity="0.35" stroke-width="1.5"/>
<text x="150" y="220" text-anchor="middle" font-family="Georgia,serif" font-size="26"
 font-weight="bold" fill="#ffffff" fill-opacity="0.92" letter-spacing="2">{tspans}</text>
<text x="150" y="400" text-anchor="middle" font-family="sans-serif" font-size="11"
 fill="#ffffff" fill-opacity="0.4" letter-spacing="4">A JELLYLOOK DEMO</text></svg>'''
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()

CARDS = [
    dict(title="Midnight Dispatch",  year=2024, type="tv",   match=94, imdb=8.6, tmdb=8.1, because="The Night Ledger",  state="btn"),
    dict(title="The Copper Line",    year=2023, type="film", match=91, imdb=8.2, tmdb=7.8, because="Harbour Patrol",    state="btn"),
    dict(title="Static Fields",      year=2025, type="tv",   match=None, imdb=8.9, tmdb=8.4, because="The Night Ledger", state="lib"),
    dict(title="Southern Approach",  year=2022, type="film", match=87, imdb=7.9, tmdb=7.5, because="Harbour Patrol",    state="requested"),
    dict(title="Glasshouse Protocol",year=2024, type="tv",   match=85, imdb=8.1, tmdb=7.9, because="Cold Meridian",     state="btn"),
    dict(title="The Long Quiet",     year=2021, type="film", match=83, imdb=7.7, tmdb=7.3, because="Cold Meridian",     state="btn"),
    dict(title="Paper Harbour",      year=2023, type="tv",   match=81, imdb=7.8, tmdb=7.6, because="The Night Ledger",  state="available"),
    dict(title="Ninth Signal",       year=2025, type="film", match=79, imdb=7.4, tmdb=7.2, because="Harbour Patrol",    state="btn"),
]

def card_html(c, i):
    p = poster(c["title"], i)
    badge = ""
    if c["state"] == "lib":
        badge = '<span class="lib-chip" style="position:absolute;top:8px;right:8px;background:var(--surface)">In library</span>'
    elif c["match"] is not None:
        badge = f'<span class="match-badge" style="--pct:{c["match"]}">{c["match"]}%</span>'
    action = {
        "btn": '<button class="seerr-btn">+ Add to Seerr</button>',
        "lib": '<span class="lib-chip">In library</span>',
        "requested": '<button class="seerr-btn is-state" disabled>Requested &#10003;</button>',
        "available": '<button class="seerr-btn is-state" disabled>Available</button>',
    }[c["state"]]
    return f'''<article class="card" style="--i:{i}">
  <div class="poster-wrap"><img src="{p}" alt="{c["title"]} poster">{badge}</div>
  <div class="card-body">
    <h3 class="card-title">{c["title"]} <span class="year">&rsquo;{str(c["year"])[-2:]}</span></h3>
    <div class="rating-row"><span class="imdb-badge">&#9733; {c["imdb"]}</span>
      <span class="tmdb-score">TMDb {c["tmdb"]}</span>
      <span class="type-chip">{"tv" if c["type"]=="tv" else "film"}</span></div>
    <p class="because">Because you watched <strong>{c["because"]}</strong></p>
    <div class="card-actions">{action}</div>
  </div>
</article>'''

cards = "\n".join(card_html(c, i) for i, c in enumerate(CARDS))

page = f'''<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jellylook</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../app/static/style.css">
<style>.card{{opacity:1!important;transform:none!important;animation:none!important}}</style>
</head><body>
<header>
  <div class="header-center"><span class="wordmark">jellylook</span>
    <span class="meta">last scan &middot; 2h ago &middot; dean</span></div>
  <div class="header-right">
    <button class="icon-btn" aria-label="Settings">&#9881;</button>
    <button class="icon-btn" aria-label="Help">?</button></div>
</header>
<main>
  <section class="controls">
    <div class="user-row">
      <div class="chips">
        <label class="chip is-on"><input type="checkbox" checked> dean</label>
        <label class="chip"><input type="checkbox"> robert</label>
      </div>
      <button class="scan-btn"><span class="scan-label">&#8982; Scan</span></button>
    </div>
    <div class="filter-row">
      <div class="seg"><span class="seg-label">Filter</span>
        <button class="seg-btn is-active">All</button><button class="seg-btn">TV</button>
        <button class="seg-btn">Film</button></div>
      <div class="seg"><span class="seg-label">Sort</span>
        <select><option>Match %</option></select></div>
    </div>
  </section>
  <p class="status-line"></p>
  <section class="grid">{cards}</section>
  <nav class="pager">
    <button class="page-btn" disabled>&lsaquo;</button>
    <button class="page-btn is-current">1</button><button class="page-btn">2</button>
    <button class="page-btn">3</button><button class="page-btn">&rsaquo;</button>
  </nav>
</main>
<div class="overlay hidden" id="season-overlay">
  <div class="modal"><h2>Choose seasons &mdash; Midnight Dispatch</h2>
    <label class="season-all"><input type="checkbox"> All seasons</label>
    <div class="season-list">
      <label><input type="checkbox" checked><span>Season 1</span><span class="ep-count">10 ep</span></label>
      <label><input type="checkbox" checked><span>Season 2</span><span class="ep-count">8 ep</span></label>
      <label><input type="checkbox"><span>Season 3</span><span class="ep-count">8 ep</span></label>
    </div>
    <div class="modal-actions"><button class="ghost-btn">Cancel</button>
      <button class="primary-btn">Add to Seerr</button></div></div>
</div>
</body></html>'''

pathlib.Path("/home/claude/jellylook/mock/mock.html").write_text(page)
print("mock.html written")
