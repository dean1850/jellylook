"""Shared fixtures. Nothing here touches the network."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("TMDB_API_KEY", "test")
os.environ.setdefault("OMDB_API_KEY", "test")
os.environ.setdefault("JELLYFIN_API_KEY", "test")
os.environ.setdefault("JELLYSTAT_API_KEY", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """An empty database per test, isolated from the developer's real data."""
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "jellylook.db"))
    db.init_db()
    return db


def iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def make_rec(title: str, tmdb_id: int, *, scan_id: str = "s1", media_type: str = "movie",
             created_at: str | None = None, year: int = 2020,
             genre_ids: str | None = ",53,", runtime: int | None = 120,
             imdb_rating: float | None = 7.5, match_score: int = 80,
             is_in_library: bool = False, seerr_status: str | None = None,
             providers: str | None = '{"flatrate":[{"name":"Demo"}]}') -> dict:
    return {
        "id": uuid.uuid4().hex, "scan_id": scan_id,
        "created_at": created_at or iso(), "media_type": media_type,
        "title": title, "year": year, "tmdb_id": tmdb_id, "imdb_id": f"tt{tmdb_id}",
        "imdb_rating": imdb_rating, "tmdb_rating": 7.0, "poster_url": "/p.jpg",
        "backdrop_url": None, "overview": "o", "genres": "Thriller",
        "genre_ids": genre_ids, "runtime": runtime, "original_language": "en",
        "popularity": 10.0, "trailer_key": "abc", "providers": providers,
        "reason": "r", "match_score": match_score, "because_of": "Seed",
        "is_in_library": is_in_library, "seerr_status": seerr_status,
    }


def make_scan(scan_id: str, *, created_at: str | None = None,
              users: str = "u1", count: int = 1) -> dict:
    return {"id": scan_id, "created_at": created_at or iso(), "source_users": users,
            "provider": "p", "model": "m", "item_count": count,
            "duration_ms": 1, "status": "ok"}
