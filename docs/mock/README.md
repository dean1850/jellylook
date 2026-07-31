# Regenerating the demo screenshots

The screenshots in `docs/` are shots of the **real UI**, driven by a real
browser against mocked upstream services (`tests/e2e/mock_upstream.py`). The
titles, artwork and ratings are all invented, so the public README carries no
third-party posters.

```bash
pip install -r requirements.txt -r requirements-dev.txt
tests/e2e/harness.sh start      # mock upstreams + jellylook on :9098
PER_PAGE=12 python docs/mock/shoot.py
tests/e2e/harness.sh stop
```

`shoot.py` runs a scan, saves one title so the ♡ state is visible, then writes
`screenshot-desktop.png`, `screenshot-filters.png`, `screenshot-season-picker.png`
and `screenshot-mobile.png`. Card animations are frozen first so the output is
reproducible.
