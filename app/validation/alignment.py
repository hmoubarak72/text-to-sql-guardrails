"""Back-translation (SQL -> question) and semantic alignment.

Two independent mechanisms:
  * LLM back-translation + LLM judge (live providers only).
  * Deterministic describer + concept alignment: extracts checkable concepts from the question (metric, years,
    months, categorical values, grouping, top-N, sort direction, entities) and verifies each against the SQL AST.
    This works offline and catches concrete mismatches (wrong year, wrong status, SUM vs AVG, missing GROUP BY).
"""
from __future__ import annotations

import re

from app.models import SchemaModel
from app.validation.sqlfacts import SQLFacts

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], start=1)}
GROUP_WORDS = {"category", "categories", "region", "regions", "channel", "channels", "status", "month", "months",
               "year", "years", "segment", "segments", "country", "countries", "city", "cities", "method", "methods",
               "reason", "reasons", "customer", "customers", "product", "products", "quarter", "week", "day",
               "job", "title"}
ENTITY_RULES = [
    (r"\bcategor(y|ies)\b", ["categories"], []),
    (r"\bregions?\b", ["regions"], []),
    (r"\brefund", ["refunds"], []),
    (r"\b(payments?|paid|payment method)\b", ["payments"], []),
    (r"\bcustomers?\b", ["customers"], ["orders.customer_id", "payments"]),
    (r"\bproducts?\b(?!\s+categor)", ["products"], ["order_items.product_id"]),
    (r"\bemployees?|staff\b", ["employees"], []),
]
PENALTY = {"metric": 0.35, "year": 0.35, "month": 0.25, "value_missing": 0.3, "value_extra": 0.3, "grouping": 0.25,
           "top_n": 0.2, "direction": 0.25, "entity": 0.2, "extra_date": 0.15}


def describe_sql(f: SQLFacts) -> str:
    """Deterministic plain-English description of what a query computes."""
    parts = []
    if f.aggregates:
        aggs = []
        for name, distinct, cols in f.aggregates:
            target = ", ".join(sorted(set(cols))) or "rows"
            aggs.append(f"{name.upper()}{' DISTINCT' if distinct else ''} of {target}")
        parts.append("Computes " + "; ".join(dict.fromkeys(aggs)))
    else:
        parts.append("Lists " + (", ".join(f.output_columns[:8]) or "rows"))
    parts.append("from " + ", ".join(f.tables))
    if f.join_pairs:
        parts.append("joining on " + ", ".join(f"{a} = {b}" for a, b in f.join_pairs))
    conds = []
    if f.string_literals:
        conds += [("not " if neg else "") + repr(v) for v, neg in f.string_literals]
    if f.date_literals:
        ds = sorted(f.date_literals)
        conds.append(f"dates {ds[0].isoformat()}..{ds[-1].isoformat()}")
    if f.year_literals:
        conds.append("year " + ", ".join(map(str, sorted(set(f.year_literals)))))
    if conds:
        parts.append("filtered to " + ", ".join(conds))
    if f.group_by:
        parts.append("grouped by " + ", ".join(f.group_by))
    if f.order_by:
        parts.append("ordered by " + ", ".join(f"{e}{' desc' if d else ''}" for e, d in f.order_by))
    if f.limit is not None:
        parts.append(f"returning at most {f.limit} rows")
    return " ".join(parts) + "."


def _has(q: str, pattern: str) -> bool:
    return re.search(pattern, q) is not None


CRITICAL_KINDS = {"metric", "year", "month", "value_missing", "value_extra", "direction"}


def concept_alignment(question: str, f: SQLFacts, schema: SchemaModel) -> tuple[float, list[str], set[str]]:
    q = question.lower()
    diffs: list[tuple[str, str]] = []
    funcs = {a[0] for a in f.aggregates}

    # --- metric ---------------------------------------------------------------------------------
    wants_avg = _has(q, r"\b(average|mean|avg)\b")
    wants_count = _has(q, r"\bhow many\b|\bnumber of\b|\bcount\b") and not _has(q, r"\bhow many units\b")
    wants_sum = _has(q, r"\b(total|sum|revenue|sales|spend|spent|units sold|how many units|margin)\b") and not wants_count
    if wants_avg and "avg" not in funcs and not ({"sum", "count"} <= funcs):
        diffs.append(("metric", "Question asks for an average but the SQL does not compute AVG."))
    if wants_count and "count" not in funcs and not ("sum" in funcs and "quantity" in " ".join(sum((a[2] for a in f.aggregates), []))):
        diffs.append(("metric", "Question asks for a count but the SQL does not COUNT."))
    if wants_sum and not wants_avg and "sum" not in funcs and funcs:
        diffs.append(("metric", f"Question asks for a total but the SQL uses {', '.join(sorted(funcs)).upper()}."))
    if wants_sum and not wants_avg and "avg" in funcs and "sum" not in funcs:
        diffs.append(("metric", "SQL averages where the question asks for a total."))

    # --- dates ----------------------------------------------------------------------------------
    q_years = {int(y) for y in re.findall(r"\b(20\d{2})\b", q)}
    sql_years = f.years
    if q_years:
        missing = q_years - sql_years
        extra = sql_years - q_years
        if missing and (sql_years or f.date_literals):
            diffs.append(("year", f"Question mentions {sorted(q_years)} but the SQL filters {sorted(sql_years) or 'no year'}."))
        elif missing:
            diffs.append(("year", f"Question mentions {sorted(q_years)} but the SQL has no date filter."))
        elif extra and not (len(q_years) == 1 and _has(q, r"\bsince\b|\bafter\b|\bfrom\b")):
            diffs.append(("year", f"SQL filters years {sorted(extra)} not mentioned in the question."))
    elif f.date_literals and not _has(q, r"\b(since|before|after|between|month|quarter|week|day|date|recent|latest|last|first)\b"):
        diffs.append(("extra_date", "SQL restricts dates although the question does not mention a period."))
    q_months = {MONTHS[m] for m in MONTHS if re.search(rf"\b{m}\b", q)}
    if q_months and f.date_literals:
        sql_months = {d.month for d in f.date_literals}
        if not q_months & sql_months:
            diffs.append(("month", "Question names a month that does not appear in the SQL date range."))

    # --- categorical values ---------------------------------------------------------------------
    value_index: dict[str, str] = {}
    for t in schema.tables.values():
        for c in t.allowed_columns():
            for v in c.categorical_values:
                value_index[v.lower()] = f"{t.name}.{c.name}"
    q_norm = q.replace("_", " ")
    q_values = {v for v in value_index if re.search(r"(?<![a-z])" + re.escape(v.replace("_", " ")) + r"(?![a-z])", q_norm)}
    # keep the longest match: "gift card" matches gift_card, not also card
    q_values = {v for v in q_values
                if not any(w != v and re.search(r"(?<![a-z])" + re.escape(v.replace("_", " ")) + r"(?![a-z])",
                                                w.replace("_", " ")) for w in q_values)}
    sql_pos = {v.lower() for v, neg in f.string_literals if not neg}
    sql_any = {v.lower() for v, _ in f.string_literals}
    for v in sorted(q_values - sql_any):
        # a value can legitimately be satisfied by grouping on its column (e.g. "web vs mobile")
        col = value_index[v]
        if col in f.group_by or any(col.split(".")[1] == g.split(".")[-1] for g in f.group_by):
            continue
        if v in {"returned"} and "refunds" in f.tables:
            continue
        diffs.append(("value_missing", f"Question mentions '{v}' but the SQL does not filter on it."))
    for v in sorted((sql_pos & set(value_index)) - q_values):
        diffs.append(("value_extra", f"SQL filters on '{v}', which the question does not mention."))

    # --- grouping / ranking ---------------------------------------------------------------------
    m = re.search(r"(\w+)?\s*\b(by|per|each|for every)\s+(\w+)", q)
    agent_verbs = {"placed", "made", "issued", "paid", "sold", "bought", "ordered", "purchased", "used", "generated"}
    if m and m.group(3) in GROUP_WORDS and not (m.group(2) == "by" and m.group(1) in agent_verbs) \
            and not f.group_by and not f.uses_window:
        diffs.append(("grouping", f"Question asks for a breakdown by {m.group(3)} but the SQL has no GROUP BY."))
    mt = re.search(r"\btop\s+(\d+)\b|\b(\d+)\s+(?:most|highest|largest|best|biggest|lowest|cheapest)\b", q)
    if mt:
        n = int(mt.group(1) or mt.group(2))
        if f.limit != n:
            diffs.append(("top_n", f"Question asks for the top {n} but the SQL returns up to {f.limit} rows."))
    elif _has(q, r"^(which|what)\b.*\b(the most|the highest|the largest|the lowest|the fewest|the least)\b") \
            and f.limit not in (1, None) and not f.group_by:
        diffs.append(("top_n", "Question asks for a single top item but the SQL does not limit to one row."))
    if f.order_by and _has(q, r"\b(top|highest|most|largest|biggest|best)\b") and not _has(q, r"\b(lowest|least|fewest|cheapest)\b"):
        if not f.order_by[0][1]:
            diffs.append(("direction", "Question asks for the highest values but the SQL sorts ascending."))
    if f.order_by and _has(q, r"\b(lowest|least|fewest|cheapest|smallest)\b") and f.order_by[0][1]:
        diffs.append(("direction", "Question asks for the lowest values but the SQL sorts descending."))

    # --- entities -------------------------------------------------------------------------------
    used_cols = set(f.group_by) | set(f.filter_columns) | {c for a in f.aggregates for c in a[2]}
    for pattern, tables, alternatives in ENTITY_RULES:
        if _has(q, pattern) and not (set(tables) & set(f.tables)) and not any(
                a in used_cols or a in f.tables for a in alternatives):
            diffs.append(("entity", f"Question refers to {tables[0]} but the SQL does not use them."))

    score = 1.0
    seen_kinds: dict[str, int] = {}
    for kind, _ in diffs:
        seen_kinds[kind] = seen_kinds.get(kind, 0) + 1
        score -= PENALTY[kind] if seen_kinds[kind] == 1 else PENALTY[kind] / 2
    return max(0.0, round(score, 3)), [d for _, d in diffs], {k for k, _ in diffs}
