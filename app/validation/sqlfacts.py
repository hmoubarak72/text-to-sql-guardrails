"""Structural facts extracted from a validated SQL query (shared by back-translation and static checks)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from app.models import SchemaModel

AGG_CLASSES = {exp.Count: "count", exp.Sum: "sum", exp.Avg: "avg", exp.Min: "min", exp.Max: "max"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class SQLFacts:
    tree: exp.Expression
    tables: list[str] = field(default_factory=list)
    alias_to_table: dict[str, str] = field(default_factory=dict)
    aggregates: list[tuple[str, bool, list[str]]] = field(default_factory=list)  # (func, distinct, ["t.c"])
    group_by: list[str] = field(default_factory=list)
    order_by: list[tuple[str, bool]] = field(default_factory=list)              # (expr sql, desc)
    limit: int | None = None
    join_pairs: list[tuple[str, str]] = field(default_factory=list)              # ("t.c", "t.c") equalities in ON
    joins: int = 0
    left_joins: int = 0
    string_literals: list[tuple[str, bool]] = field(default_factory=list)        # (value, negated)
    date_literals: list[date] = field(default_factory=list)
    year_literals: list[int] = field(default_factory=list)

    @property
    def years(self) -> set[int]:
        """Years covered by date filters (half-open upper bounds like 2025-01-01 are not counted)."""
        ys = set(self.year_literals)
        for d in self.date_literals:
            if not (d.month == 1 and d.day == 1 and any(o.year == d.year - 1 for o in self.date_literals)):
                ys.add(d.year)
        return ys
    filter_columns: list[str] = field(default_factory=list)
    output_columns: list[str] = field(default_factory=list)
    has_distinct: bool = False
    uses_window: bool = False
    subqueries: int = 0


def _col_ref(col: exp.Column, alias_map: dict[str, str]) -> str:
    t = alias_map.get((col.table or "").lower(), (col.table or "").lower())
    return f"{t}.{col.name.lower()}"


def extract_facts(sql: str, schema: SchemaModel) -> SQLFacts:
    allowed = {schema.schema_name: {t: {c.name: c.type for c in ti.allowed_columns()} for t, ti in schema.tables.items()}}
    tree = sqlglot.parse_one(sql, dialect="postgres")
    try:
        tree = qualify(tree, schema=allowed, dialect="postgres", validate_qualify_columns=False, quote_identifiers=False)
    except Exception:
        pass
    f = SQLFacts(tree=tree)
    for t in tree.find_all(exp.Table):
        if t.name.lower() in schema.tables:
            f.alias_to_table[t.alias_or_name.lower()] = t.name.lower()
            if t.name.lower() not in f.tables:
                f.tables.append(t.name.lower())
    am = f.alias_to_table
    for node in tree.find_all(exp.AggFunc):
        for cls, name in AGG_CLASSES.items():
            if isinstance(node, cls):
                distinct = node.find(exp.Distinct) is not None
                cols = [_col_ref(c, am) for c in node.find_all(exp.Column)]
                f.aggregates.append((name, distinct, cols))
    root = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if root is not None:
        g = root.args.get("group")
        if g:
            f.group_by = [(_col_ref(e, am) if isinstance(e, exp.Column) else e.sql()) for e in g.expressions]
        o = root.args.get("order") or tree.args.get("order")
        if o:
            f.order_by = [(e.this.sql(), bool(e.args.get("desc"))) for e in o.expressions]
        lim = tree.args.get("limit") or root.args.get("limit")
        if lim is not None and isinstance(lim.expression, exp.Literal) and lim.expression.is_int:
            f.limit = int(lim.expression.this)
        f.has_distinct = root.args.get("distinct") is not None
        f.output_columns = [e.alias_or_name for e in root.expressions]
    for j in tree.find_all(exp.Join):
        f.joins += 1
        if (j.side or "").upper() in {"LEFT", "RIGHT", "FULL"}:
            f.left_joins += 1
        on = j.args.get("on")
        if on is not None:
            for eq in on.find_all(exp.EQ):
                if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column):
                    f.join_pairs.append((_col_ref(eq.left, am), _col_ref(eq.right, am)))
    for where in tree.find_all(exp.Where):
        for c in where.find_all(exp.Column):
            f.filter_columns.append(_col_ref(c, am))
    for lit in tree.find_all(exp.Literal):
        if not lit.is_string:
            continue
        val = lit.this
        negated = isinstance(lit.parent, exp.NEQ) or (lit.find_ancestor(exp.Not) is not None)
        if _DATE_RE.match(val):
            try:
                f.date_literals.append(date.fromisoformat(val))
            except ValueError:
                pass
        else:
            f.string_literals.append((val, negated))
    for lit in tree.find_all(exp.Literal):
        if lit.is_int and 2000 <= int(lit.this) <= 2100 and lit.find_ancestor(exp.Limit) is None:
            f.year_literals.append(int(lit.this))
    f.uses_window = tree.find(exp.Window) is not None
    f.subqueries = max(0, len(list(tree.find_all(exp.Select))) - 1)
    return f
