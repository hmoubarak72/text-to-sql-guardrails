import json
from pathlib import Path

import pytest

from app.config import Settings
from app.models import SchemaModel

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def schema() -> SchemaModel:
    return SchemaModel.model_validate(json.loads((FIX / "schema_snapshot.json").read_text()))


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def db_settings(settings):
    """Skips DB-backed tests (reported as SKIPPED, never as passed) when PostgreSQL is unreachable."""
    from app.db import make_reader_engine, ping

    if not ping(make_reader_engine(settings)):
        pytest.skip("PostgreSQL not reachable with current settings")
    return settings
