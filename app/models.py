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


class ScanRecord(BaseModel):
    id: str
    created_at: str
    source_users: str
    provider: Optional[str] = None
    model: Optional[str] = None
    item_count: int = 0
    duration_ms: int = 0
    status: str = "ok"
