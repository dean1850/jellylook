"""Configuration loaded from .env (secrets) — runtime prefs live in the
app_settings table instead.

Providers: anthropic | openai | google | openwebui | ollama.
validate_runtime() returns problems; main.py raises RuntimeError at startup
(fail-fast) if any exist.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

VALID_PROVIDERS = {"anthropic", "openai", "google", "openwebui", "ollama"}
VALID_HISTORY_SOURCES = {"jellystat", "jellyfin"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    # core
    tz: str = "Australia/Sydney"
    jellylook_port: int = 3045
    retention_days: int = 60
    recs_per_scan: int = 60
    per_page: int = 20
    log_level: str = "INFO"

    # history
    history_source: str = "jellystat"  # jellystat | jellyfin
    jellystat_url: str = "http://192.168.1.5:3015"
    jellystat_api_key: str = ""
    jellyfin_url: str = "http://192.168.1.5:8096"
    jellyfin_api_key: str = ""

    # requests
    seerr_url: str = "http://192.168.1.5:5055"
    seerr_api_key: str = ""

    # llm
    llm_provider: str = "anthropic"  # anthropic | openai | google | openwebui | ollama
    llm_model: str = "claude-haiku-4-5"
    llm_temperature: float = 0.7
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    google_api_key: str = ""
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    openwebui_base_url: str = "http://192.168.1.6:8080"
    openwebui_api_key: str = ""
    ollama_base_url: str = "http://192.168.1.6:11434"

    # metadata
    tmdb_api_key: str = ""
    omdb_api_key: str = ""

    def validate_runtime(self) -> list[str]:
        """Human-readable problems for the active modes (empty list = OK)."""
        problems: list[str] = []

        if self.history_source not in VALID_HISTORY_SOURCES:
            problems.append(
                f"HISTORY_SOURCE must be one of {sorted(VALID_HISTORY_SOURCES)}, "
                f"got {self.history_source!r}"
            )
        if self.llm_provider not in VALID_PROVIDERS:
            problems.append(
                f"LLM_PROVIDER must be one of {sorted(VALID_PROVIDERS)}, "
                f"got {self.llm_provider!r}"
            )

        if self.history_source == "jellystat" and not self.jellystat_api_key:
            problems.append("JELLYSTAT_API_KEY required when HISTORY_SOURCE=jellystat")
        if not self.jellyfin_api_key:
            problems.append("JELLYFIN_API_KEY required (ownership + id/poster lookups)")

        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY required when LLM_PROVIDER=anthropic")
        if self.llm_provider == "openai" and not self.openai_api_key:
            problems.append("OPENAI_API_KEY required when LLM_PROVIDER=openai")
        if self.llm_provider == "google" and not self.google_api_key:
            problems.append("GOOGLE_API_KEY required when LLM_PROVIDER=google")
        if self.llm_provider == "openwebui" and not self.openwebui_api_key:
            problems.append("OPENWEBUI_API_KEY required when LLM_PROVIDER=openwebui")
        # ollama needs no key

        if not self.tmdb_api_key:
            problems.append("TMDB_API_KEY required")
        if not self.omdb_api_key:
            problems.append("OMDB_API_KEY required")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
