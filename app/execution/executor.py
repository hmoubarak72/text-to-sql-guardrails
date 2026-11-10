"""Sandboxed execution: restricted role, READ ONLY transaction, statement timeout, always rolled back."""
from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal

from sqlalchemy import Engine, text

from app.models import ExecutionResult


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return "<binary>"
    return v


class SafeExecutor:
    def __init__(self, engine: Engine, max_rows: int, timeout_ms: int) -> None:
        self.engine = engine
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms

    def execute(self, sql: str) -> ExecutionResult:
        start = time.perf_counter()
        try:
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    conn.execute(text(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}"))
                    result = conn.execute(text(sql))
                    columns = list(result.keys())
                    fetched = result.fetchmany(self.max_rows + 1)
                finally:
                    trans.rollback()  # never commit, even for SELECT
        except Exception as exc:
            orig = getattr(exc, "orig", exc)
            code = getattr(orig, "sqlstate", None)
            msg = str(orig).splitlines()[0][:300]
            if code == "57014":
                msg = f"Query exceeded the {self.timeout_ms} ms statement timeout and was cancelled."
            return ExecutionResult(ok=False, error=msg, error_code=code,
                                   duration_ms=round((time.perf_counter() - start) * 1000, 2))
        truncated = len(fetched) > self.max_rows
        rows = [[_jsonable(v) for v in r] for r in fetched[: self.max_rows]]
        return ExecutionResult(ok=True, columns=columns, rows=rows, row_count=len(rows), truncated=truncated,
                               duration_ms=round((time.perf_counter() - start) * 1000, 2))
