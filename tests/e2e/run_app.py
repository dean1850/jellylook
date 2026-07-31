"""Launch the real jellylook against tests/e2e/mock_upstream.

Nothing in app/ is stubbed: the FastAPI app, the SQLite schema, the scan
pipeline and the browser-side JS are all the shipping code. Only the base URLs
of the outbound services are redirected, which is what makes the end-to-end run
meaningful — a bug in db.py or app.js shows up here.

    python -m tests.e2e.run_app --mock-port 9099 --app-port 9098 --data-dir /tmp/x
"""
import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock-port", type=int, default=9099)
    ap.add_argument("--app-port", type=int, default=9098)
    ap.add_argument("--data-dir", default="/tmp/jellylook-e2e")
    args = ap.parse_args()

    mock = f"http://127.0.0.1:{args.mock_port}"
    # Tuning knobs use setdefault so a caller (the screenshot script wants a
    # fuller grid, for instance) can override them from the environment.
    for key, value in (("RECS_PER_SCAN", "12"), ("PER_PAGE", "8"),
                       ("LOG_LEVEL", "WARNING"), ("TMDB_REGION", "AU")):
        os.environ.setdefault(key, value)
    os.environ.update({
        "DATA_DIR": args.data_dir,
        "HISTORY_SOURCE": "jellystat",
        "JELLYSTAT_URL": f"{mock}/jellystat",
        "JELLYSTAT_API_KEY": "test",
        "JELLYFIN_URL": f"{mock}/jellyfin",
        "JELLYFIN_API_KEY": "test",
        "SEERR_URL": f"{mock}/seerr",
        "SEERR_API_KEY": "test",
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "mock-model",
        "OPENAI_BASE_URL": f"{mock}/llm/v1",
        "OPENAI_API_KEY": "test",
        "TMDB_API_KEY": "test",
        "OMDB_API_KEY": "test",
    })

    # Imported only after the environment is set: config.get_settings() is
    # lru_cached, so the first call decides the whole process's configuration.
    from app import metadata
    metadata.TMDB_BASE = f"{mock}/tmdb"
    metadata.OMDB_BASE = f"{mock}/omdb"
    metadata.IMG_POSTER = f"{mock}/img/w500"
    metadata.IMG_BACKDROP = f"{mock}/img/w1280"

    import uvicorn
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=args.app_port, log_level="warning")


if __name__ == "__main__":
    main()
