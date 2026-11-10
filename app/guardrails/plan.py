"""Non-executing plan inspection: EXPLAIN (FORMAT JSON) without ANALYZE, inside a read-only transaction."""
from __future__ import annotations

import json

from sqlalchemy import Engine, text

from app.models import PlanResult, Violation



def _walk(node: dict):
    yield node
    for child in node.get("Plans", []) or []:
        yield from _walk(child)


def summarize_plan(plan_json, table_rows: dict[str, int] | None = None) -> tuple[float, float, float, list[str]]:
    root = plan_json[0]["Plan"]
    total_cost = float(root.get("Total Cost", 0.0))
    plan_rows = float(root.get("Plan Rows", 0.0))
    scanned = 0.0
    node_types = []
    for n in _walk(root):
        node_types.append(n.get("Node Type", "?"))
        if n.get("Node Type") in {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Tid Scan"}:
            # Sequential scans read the whole relation (use catalog row estimates); index scans read roughly the
            # planner's row estimate. EXPLAIN without ANALYZE has no actual counts, so this is an estimate.
            rel = n.get("Relation Name")
            if n.get("Node Type") == "Seq Scan" and table_rows and rel in table_rows:
                scanned += float(table_rows[rel])
            else:
                scanned += float(n.get("Plan Rows", 0.0))
    return total_cost, plan_rows, scanned, node_types


class PlanInspector:
    def __init__(self, engine: Engine, max_total_cost: float, max_rows_scanned: int, timeout_ms: int,
                 table_rows: dict[str, int] | None = None) -> None:
        self.engine = engine
        self.table_rows = table_rows or {}
        self.max_total_cost = max_total_cost
        self.max_rows_scanned = max_rows_scanned
        self.timeout_ms = timeout_ms

    def inspect(self, sql: str) -> PlanResult:
        try:
            with self.engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    conn.execute(text(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}"))
                    raw = conn.execute(text("EXPLAIN (FORMAT JSON) " + sql)).scalar_one()
                finally:
                    trans.rollback()
        except Exception as exc:  # planner errors (e.g. type mismatch) are reported, not executed
            msg = str(getattr(exc, "orig", exc)).splitlines()[0][:300]
            return PlanResult(allowed=False, violations=[Violation(stage="plan", rule="plan_error",
                                                                   message=f"Query planning failed: {msg}")])
        plan = raw if isinstance(raw, list) else json.loads(raw)
        total_cost, plan_rows, scanned, _ = summarize_plan(plan, self.table_rows)
        violations = []
        if total_cost > self.max_total_cost:
            violations.append(Violation(stage="plan", rule="plan_cost",
                                        message=f"Estimated cost {total_cost:,.0f} exceeds limit {self.max_total_cost:,.0f}."))
        if scanned > self.max_rows_scanned:
            violations.append(Violation(stage="plan", rule="rows_scanned",
                                        message=f"Estimated rows scanned {scanned:,.0f} exceed limit {self.max_rows_scanned:,}."))
        return PlanResult(allowed=not violations, total_cost=total_cost, plan_rows=plan_rows, rows_scanned=scanned,
                          violations=violations, plan=plan)
