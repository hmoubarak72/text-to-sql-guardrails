"""Wire the pipeline from settings (used by the API, the evaluation runner and integration tests)."""
from __future__ import annotations

from app.config import Settings
from app.db import make_app_engine, make_reader_engine
from app.execution.executor import SafeExecutor
from app.guardrails.plan import PlanInspector
from app.llm.base import LLMProvider
from app.llm.factory import make_provider
from app.persistence import MetaStore, NullStore
from app.pipeline import Pipeline
from app.prompting.builder import load_fewshots
from app.schema.introspect import introspect_schema, load_metadata


def build_pipeline(settings: Settings, provider: LLMProvider | None = None, persist: bool = True) -> Pipeline:
    reader = make_reader_engine(settings)
    metadata = load_metadata(settings.metadata_path)
    schema = introspect_schema(reader, settings.query_schema, metadata)
    rows = {t: ti.row_estimate or 0 for t, ti in schema.tables.items()}
    store = MetaStore(make_app_engine(settings), settings.history_preview_rows, settings.log_sql_max_chars) \
        if persist else NullStore()
    return Pipeline(settings=settings, schema=schema, metadata=metadata, fewshots=load_fewshots(settings.fewshot_path),
                    provider=provider or make_provider(settings),
                    plan_inspector=PlanInspector(reader, settings.max_plan_total_cost, settings.max_plan_rows_scanned,
                                                 settings.statement_timeout_ms, rows),
                    executor=SafeExecutor(reader, settings.max_result_rows, settings.statement_timeout_ms),
                    store=store)
