"""History, guardrail audit log and feedback storage (app_meta schema, written by the t2s_app role only)."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, text

from app.guardrails.policy import redact_literals
from app.models import FeedbackRequest, QueryResponse, Violation


class MetaStore:
    def __init__(self, engine: Engine, preview_rows: int = 20, sql_max_chars: int = 2000) -> None:
        self.engine = engine
        self.preview_rows = preview_rows
        self.sql_max_chars = sql_max_chars

    def log_query(self, r: QueryResponse) -> None:
        summary = {
            "decision_reason": r.decision_reason,
            "confidence_label": r.confidence.label if r.confidence else None,
            "blocked_rules": [v.rule for v in (r.guardrail.violations if r.guardrail else [])]
                             + [v.rule for v in (r.plan.violations if r.plan else [])],
            "checks": {c.name: c.status for c in r.checks},
            "alternative_match": r.alternative.match if r.alternative else None,
            "repair_attempts": r.repair_attempts,
            "columns": r.columns,
        }
        preview = None
        if self.preview_rows > 0 and r.rows:
            preview = {"columns": r.columns, "rows": r.rows[: self.preview_rows]}
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO app_meta.query_log (query_id, session_id, question, source, decision, sql_text, confidence,"
                " row_count, truncated, duration_ms, total_latency_ms, provider, warnings, response_summary, result_preview)"
                " VALUES (:id, :sid, :q, :src, :dec, :sql, :conf, :rc, :tr, :dur, :lat, :prov, CAST(:w AS jsonb),"
                " CAST(:sum AS jsonb), CAST(:prev AS jsonb))"),
                {"id": r.query_id, "sid": r.session_id, "q": r.question[:2000], "src": r.source, "dec": r.decision,
                 "sql": (r.sql or r.original_sql or "")[: self.sql_max_chars] or None,
                 "conf": r.confidence.score if r.confidence else None, "rc": r.row_count, "tr": r.truncated,
                 "dur": r.execution_ms, "lat": r.total_latency_ms, "prov": r.provider,
                 "w": json.dumps(r.warnings[:50]), "sum": json.dumps(summary, default=str),
                 "prev": json.dumps(preview, default=str) if preview else None})

    def log_guardrail_events(self, query_id: str, session_id: str, sql: str | None, violations: list[Violation]) -> None:
        if not violations:
            return
        with self.engine.begin() as conn:
            for v in violations:
                conn.execute(text(
                    "INSERT INTO app_meta.guardrail_events (query_id, session_id, stage, rule, reason, sql_text)"
                    " VALUES (:qid, :sid, :stage, :rule, :reason, :sql)"),
                    {"qid": query_id, "sid": session_id, "stage": v.stage, "rule": v.rule, "reason": v.message[:500],
                     "sql": redact_literals(sql or "", self.sql_max_chars)})

    def history(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT query_id, created_at, question, source, decision, sql_text, confidence, row_count, truncated,"
                " total_latency_ms, warnings, response_summary, result_preview FROM app_meta.query_log"
                " WHERE session_id = :sid ORDER BY created_at DESC LIMIT :lim"), {"sid": session_id, "lim": limit})
            return [dict(r._mapping) | {"query_id": str(r.query_id), "created_at": r.created_at.isoformat()}
                    for r in rows]

    def add_feedback(self, fb: FeedbackRequest) -> int | None:
        with self.engine.begin() as conn:
            owner = conn.execute(text("SELECT session_id FROM app_meta.query_log WHERE query_id = :id"),
                                 {"id": fb.query_id}).scalar()
            if owner is None or owner != fb.session_id:
                return None
            return conn.execute(text(
                "INSERT INTO app_meta.feedback (query_id, session_id, verdict, comment, corrected_sql)"
                " VALUES (:id, :sid, :v, :c, :cs) RETURNING feedback_id"),
                {"id": fb.query_id, "sid": fb.session_id, "v": fb.verdict, "c": fb.comment,
                 "cs": fb.corrected_sql}).scalar_one()

    def pending_feedback(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT f.feedback_id, f.verdict, f.comment, f.corrected_sql, q.question, q.sql_text, q.decision"
                " FROM app_meta.feedback f JOIN app_meta.query_log q USING (query_id)"
                " WHERE f.review_status = 'pending_review' ORDER BY f.created_at"))
            return [dict(r._mapping) for r in rows]


class NullStore:
    """No-op store used by offline evaluation and unit tests."""

    def log_query(self, r: QueryResponse) -> None: ...
    def log_guardrail_events(self, *a, **k) -> None: ...
    def history(self, session_id: str, limit: int = 50) -> list: return []
    def add_feedback(self, fb: FeedbackRequest) -> int | None: return None
