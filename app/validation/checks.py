"""Pre-execution static checks and post-execution result sanity checks."""
from __future__ import annotations

from datetime import date

from app.models import Check, ExecutionResult, GenerationOutput, SchemaModel
from app.schema.retrieval import RetrievalResult
from app.validation.sqlfacts import SQLFacts

NON_NEGATIVE_COLUMNS = {"quantity", "unit_price", "unit_cost", "amount", "shipping_cost", "discount"}


# ======================================================================================== pre-execution
def check_joins(f: SQLFacts, schema: SchemaModel) -> Check:
    fk = set()
    for t, c, rt, rc in schema.fk_edges():
        fk.add((f"{t}.{c}", f"{rt}.{rc}"))
        fk.add((f"{rt}.{rc}", f"{t}.{c}"))
    bad = [f"{a} = {b}" for a, b in f.join_pairs if a.split(".")[0] != b.split(".")[0] and (a, b) not in fk]
    if bad:
        return Check(name="join_keys", phase="pre_execution", status="fail", score=0.0,
                     detail="Join condition does not follow a declared foreign key: " + "; ".join(bad))
    if f.joins and not f.join_pairs:
        return Check(name="join_keys", phase="pre_execution", status="warn", score=0.5,
                     detail="Join without an equality condition on keys (possible cross join).")
    detail = "All join conditions follow declared foreign keys." if f.joins else "No joins."
    return Check(name="join_keys", phase="pre_execution", status="pass", score=1.0, detail=detail)


def check_fan_out(f: SQLFacts, schema: SchemaModel) -> Check:
    """SUM/AVG/COUNT over a parent table's rows while a join path reaches a child (many-side) table inflates results.

    Walks the query's own join conditions: stepping from table X to table Y where Y holds a non-unique FK to X is a
    one-to-many step. A non-DISTINCT aggregate whose source table can reach such a step (without grouping by the
    child's key) is flagged.
    """
    many_side: set[tuple[str, str]] = set()   # (parent, child) where child -> parent is many-to-one
    for t in schema.tables.values():
        for fk in t.foreign_keys:
            if not fk.unique:
                many_side.add((fk.ref_table, t.name))
    adj: dict[str, set[str]] = {}
    for a, b in f.join_pairs:
        ta, tb = a.split(".")[0], b.split(".")[0]
        if ta != tb:
            adj.setdefault(ta, set()).add(tb)
            adj.setdefault(tb, set()).add(ta)

    def fanned_by(src: str) -> str | None:
        seen, stack = {src}, [src]
        while stack:
            x = stack.pop()
            for y in adj.get(x, ()):
                if y in seen:
                    continue
                if (x, y) in many_side:
                    pk = schema.tables[y].primary_key
                    if not any(g == f"{y}.{p}" for g in f.group_by for p in pk):
                        return y
                seen.add(y)
                stack.append(y)
        return None

    issues = []
    for func, distinct, cols in f.aggregates:
        if distinct or func in {"min", "max"}:
            continue
        src_tables = {c.split(".")[0] for c in cols} or ({f.tables[0]} if f.tables else set())
        for parent in src_tables:
            child = fanned_by(parent)
            if child and child not in src_tables:
                issues.append(f"{func.upper()} over {parent} rows while joined (one-to-many) to {child} may double "
                              "count; use COUNT(DISTINCT ...) or aggregate before joining.")
    if issues:
        return Check(name="aggregation_fan_out", phase="pre_execution", status="warn", score=0.1,
                     detail=" ".join(dict.fromkeys(issues)))
    return Check(name="aggregation_fan_out", phase="pre_execution", status="pass", score=1.0,
                 detail="No aggregation over a duplicated (fanned-out) parent table detected.")


def check_date_range(f: SQLFacts, schema: SchemaModel) -> Check:
    if not f.date_literals and not f.year_literals:
        return Check(name="date_range", phase="pre_execution", status="pass", score=1.0, detail="No date filters.")
    start, end = date.fromisoformat(schema.data_start), date.fromisoformat(schema.data_end)
    outside = [d.isoformat() for d in f.date_literals if d < start.replace(month=1, day=1) or d > date(end.year + 1, 1, 1)]
    outside += [str(y) for y in f.year_literals if y < start.year or y > end.year]
    if outside:
        return Check(name="date_range", phase="pre_execution", status="warn", score=0.3,
                     detail=f"Date filter {', '.join(outside)} is outside the data span {start}..{end}; "
                            "results will likely be empty.")
    return Check(name="date_range", phase="pre_execution", status="pass", score=1.0,
                 detail=f"Date filters fall within the data span {start}..{end}.")


def check_schema_coverage(f: SQLFacts, retrieval: RetrievalResult | None, gen: GenerationOutput | None) -> Check:
    problems = []
    if retrieval is not None:
        missing = [t for t in retrieval.forced_by_glossary if t not in f.tables]
        if missing and retrieval.glossary_hits:
            problems.append(f"Glossary terms {retrieval.glossary_hits} imply tables {missing}, which the SQL does not use.")
    if gen is not None and gen.tables_used:
        claimed = {t.split(".")[-1].lower() for t in gen.tables_used}
        actual = set(f.tables)
        if claimed != actual:
            problems.append(f"Model-declared tables {sorted(claimed)} differ from tables actually referenced {sorted(actual)}.")
    if problems:
        return Check(name="schema_coverage", phase="pre_execution", status="warn", score=0.4, detail=" ".join(problems))
    return Check(name="schema_coverage", phase="pre_execution", status="pass", score=1.0,
                 detail="Referenced tables are consistent with the question's glossary terms and the model's claims.")


def pre_execution_checks(f: SQLFacts, schema: SchemaModel, retrieval: RetrievalResult | None,
                         gen: GenerationOutput | None) -> list[Check]:
    return [check_joins(f, schema), check_fan_out(f, schema), check_date_range(f, schema),
            check_schema_coverage(f, retrieval, gen)]


# ======================================================================================= post-execution
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def post_execution_checks(f: SQLFacts, res: ExecutionResult, schema: SchemaModel) -> list[Check]:
    checks: list[Check] = []
    rows, cols = res.rows, res.columns
    if not rows:
        checks.append(Check(name="non_empty", phase="post_execution", status="warn", score=0.4,
                            detail="Query returned no rows; filters may not match the data."))
        return checks
    checks.append(Check(name="non_empty", phase="post_execution", status="pass", score=1.0,
                        detail=f"{res.row_count} row(s) returned."))

    # NULL-heavy columns (a common symptom of a wrong or outer join)
    heavy = []
    for i, c in enumerate(cols):
        nulls = sum(1 for r in rows if r[i] is None)
        if len(rows) >= 3 and nulls / len(rows) > 0.5:
            heavy.append(f"{c} ({nulls}/{len(rows)} NULL)")
        elif len(rows) < 3 and nulls == len(rows):
            heavy.append(f"{c} (all NULL)")
    checks.append(Check(name="null_profile", phase="post_execution", status="warn" if heavy else "pass",
                        score=0.3 if heavy else 1.0,
                        detail=("NULL-heavy columns may indicate a bad join or filter: " + ", ".join(heavy))
                        if heavy else "No NULL-heavy columns."))

    # Implausible values: negative money/quantities; counts larger than the counted table.
    implausible = []
    agg_names = {fn for fn, _, _ in f.aggregates}
    for i, c in enumerate(cols):
        vals = [r[i] for r in rows if _is_num(r[i])]
        if not vals:
            continue
        lc = c.lower()
        if any(v < 0 for v in vals) and (any(k in lc for k in NON_NEGATIVE_COLUMNS | {"revenue", "total", "count", "sales", "units"})):
            implausible.append(f"{c} has negative values")
        if ("count" in agg_names) and any(k in lc for k in ("count", "orders", "customers", "num", "n_")) and f.tables:
            base = f.tables[0]
            cap = schema.tables[base].row_estimate or 0
            total = sum(vals) if f.group_by else max(vals)
            if cap and total > cap * 1.05:
                implausible.append(f"{c} totals {total:,.0f}, more than the {cap:,} rows in {base} (possible fan-out)")
        if any(abs(v) > 1e11 for v in vals):
            implausible.append(f"{c} has implausibly large magnitude")
    checks.append(Check(name="value_plausibility", phase="post_execution", status="warn" if implausible else "pass",
                        score=0.2 if implausible else 1.0,
                        detail="; ".join(implausible) if implausible else "Numeric values are within plausible ranges."))

    # Dates in the output must fall inside the data span.
    start, end = schema.data_start, schema.data_end
    out_dates = []
    for i, c in enumerate(cols):
        for r in rows:
            v = r[i]
            if isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
                if v[:10] < "2015-01-01" or v[:10] > end:
                    out_dates.append(f"{c}={v[:10]}")
    checks.append(Check(name="output_dates", phase="post_execution", status="warn" if out_dates else "pass",
                        score=0.4 if out_dates else 1.0,
                        detail=("Dates outside the data span: " + ", ".join(out_dates[:5])) if out_dates
                        else f"Output dates (if any) fall within known ranges (data ends {end}; start {start})."))

    # Exact duplicate rows in a non-aggregated result suggest a fan-out join or missing DISTINCT.
    if not f.aggregates and not f.has_distinct:
        dup = len(rows) - len({tuple(map(str, r)) for r in rows})
        checks.append(Check(name="duplicate_rows", phase="post_execution", status="warn" if dup else "pass",
                            score=0.5 if dup else 1.0,
                            detail=f"{dup} duplicate row(s); a join may be multiplying rows." if dup
                            else "No duplicate rows."))
    if res.truncated:
        checks.append(Check(name="truncation", phase="post_execution", status="warn", score=0.8,
                            detail=f"Result truncated at {res.row_count} rows."))
    return checks
