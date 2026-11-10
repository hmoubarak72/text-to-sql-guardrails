"""Runtime configuration. Every value can be overridden by environment variables (see .env.example)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- Database connections (two separate least-privilege roles) ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "t2s"
    reader_user: str = "t2s_reader"            # runtime query role: SELECT-only, read-only transactions
    reader_password: SecretStr = SecretStr("")
    app_user: str = "t2s_app"                  # history / audit / feedback writes (app_meta only)
    app_password: SecretStr = SecretStr("")
    query_schema: str = "shop"

    # --- LLM provider ---
    llm_provider: str = Field("mock", description="anthropic | openai | mock")
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o"
    llm_timeout_s: float = 45.0
    llm_temperature: float = 0.0
    mock_fixtures_path: Path = ROOT / "eval" / "golden.yaml"

    # --- Guardrail policy ---
    max_result_rows: int = 1000                # LIMIT applied / clamped on every query
    statement_timeout_ms: int = 5000
    max_subquery_depth: int = 3
    max_joins: int = 6
    max_plan_total_cost: float = 50000.0       # EXPLAIN (no ANALYZE) total cost ceiling
    max_plan_rows_scanned: int = 200000        # sum of estimated rows read by scan nodes
    max_sql_length: int = 8000
    max_repair_attempts: int = 1               # bounded regeneration after invalid schema reference

    # --- Validation / confidence ---
    enable_alternative_query: bool = True
    enable_back_translation: bool = True
    confidence_answer_threshold: float = 0.70  # >= : answer
    confidence_abstain_threshold: float = 0.40 # <  : abstain (results withheld)
    numeric_rel_tolerance: float = 1e-6
    numeric_abs_tolerance: float = 0.01

    # --- Persistence / privacy ---
    history_preview_rows: int = 20             # 0 disables storing result previews
    log_sql_max_chars: int = 2000

    # --- API ---
    api_token: SecretStr | None = None         # optional bearer token for /v1/*
    cors_origins: str = ""

    metadata_path: Path = ROOT / "config" / "schema_metadata.yaml"
    fewshot_path: Path = ROOT / "config" / "fewshots.yaml"

    def reader_dsn(self) -> str:
        return (f"postgresql+psycopg://{self.reader_user}:{self.reader_password.get_secret_value()}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}")

    def app_dsn(self) -> str:
        return (f"postgresql+psycopg://{self.app_user}:{self.app_password.get_secret_value()}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
