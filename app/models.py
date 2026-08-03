"""Pydantic models shared across modules. IMPLEMENTED in scaffold."""
from typing import Optional

from pydantic import BaseModel


class UserRef(BaseModel):
    id: str
    name: str


class WatchedItem(BaseModel):
    title: str
    media_type: str  # movie | tv
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    play_count: int = 0
    last_played: Optional[str] = None


class Suggestion(BaseModel):
    title: str
    media_type: str  # movie | tv
    year: Optional[int] = None
    reason: Optional[str] = None
    match_score: Optional[int] = None  # 0-100, LLM estimate
    because_of: Optional[str] = None   # watched seed title


class MediaMeta(BaseModel):
    title: str
    media_type: str
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    imdb_rating: Optional[float] = None
    tmdb_rating: Optional[float] = None
    poster_url: Optional[str] = None
    backdrop_url: Optional[str] = None
    overview: Optional[str] = None
    genres: Optional[str] = None
    # Comma-delimited TMDb genre ids, sentinel-wrapped as ",28,12," so a LIKE
    # filter cannot match 12 inside 120. Display text stays in `genres`.
    genre_ids: Optional[str] = None
    runtime: Optional[int] = None            # minutes; TV uses episode length
    original_language: Optional[str] = None
    popularity: Optional[float] = None
    trailer_key: Optional[str] = None        # YouTube key, from TMDb /videos
    providers: Optional[str] = None          # JSON, region-filtered; see metadata.py


class Recommendation(BaseModel):
    id: str
    scan_id: str
    created_at: str
    media_type: str
    title: str
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    imdb_rating: Optional[float] = None
    tmdb_rating: Optional[float] = None
    poster_url: Optional[str] = None
    backdrop_url: Optional[str] = None
    overview: Optional[str] = None
    genres: Optional[str] = None
    reason: Optional[str] = None
    match_score: Optional[int] = None
    because_of: Optional[str] = None
    is_in_library: bool = False
    seerr_status: Optional[str] = None
    # True once a scan filed this title's Seerr request on the user's behalf,
    # because it cleared the auto-request threshold.
    auto_requested: bool = False
    genre_ids: Optional[str] = None
    runtime: Optional[int] = None
    original_language: Optional[str] = None
    popularity: Optional[float] = None
    trailer_key: Optional[str] = None
    providers: Optional[str] = None


class FeedbackEntry(BaseModel):
    """One viewer's verdict on one title.

    title/year/poster_url are denormalised so a verdict outlives the
    recommendation row it came from — otherwise a 👎 would stop working once
    the row is purged and the title would come straight back.
    """
    user_id: str
    media_type: str
    tmdb_id: int
    verdict: str  # saved | hidden
    title: str
    year: Optional[int] = None
    poster_url: Optional[str] = None
    created_at: str


class ScanRecord(BaseModel):
    id: str
    created_at: str
    source_users: str
    provider: Optional[str] = None
    model: Optional[str] = None
    item_count: int = 0
    duration_ms: int = 0
    status: str = "ok"
    trigger: str = "manual"  # manual | auto (the scheduler ran it)
