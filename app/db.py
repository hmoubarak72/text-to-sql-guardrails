"""Database engines. The runtime query engine and the metadata engine use different roles."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from app.config import Settings, get_settings


def make_reader_engine(settings: Settings) -> Engine:
    # Role-level defaults (read-only transactions, statement_timeout, search_path) are set in the database;
    # the connection options repeat the timeout so the guarantee holds even if role defaults drift.
    return create_engine(
        settings.reader_dsn(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "options": f"-c statement_timeout={settings.statement_timeout_ms} -c default_transaction_read_only=on",
            "application_name": "t2s-runtime-reader",
            "connect_timeout": 5,
        },
    )


def make_app_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.app_dsn(),
        pool_pre_ping=True,
        pool_size=3,
        connect_args={"application_name": "t2s-app-meta", "connect_timeout": 5},
    )


@lru_cache
def reader_engine() -> Engine:
    return make_reader_engine(get_settings())


@lru_cache
def app_engine() -> Engine:
    return make_app_engine(get_settings())


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
