"""AST-based SQL policy engine (fails closed).

Every query - generated, alternative, or user-edited - passes through `SQLPolicy.check`. Rules:
  1. Parse with the PostgreSQL dialect; any parse error or unknown syntax (sqlglot "Command") is rejected.
  2. Exactly one statement.
  3. Root must be SELECT or a set operation (UNION/INTERSECT/EXCEPT) of SELECTs.
  4. No write-capable or session-changing node anywhere in the tree (DML, DDL, COPY, SET, locks, SELECT INTO,
     transaction control), including inside CTEs and subqueries. No recursive CTEs.
  5. Functions must be on an explicit allowlist (unknown functions such as pg_sleep, pg_read_file, dblink,
     set_config are rejected).
  6. Tables must be in the permitted schema allowlist (no pg_catalog, information_schema, app_meta, table functions).
  7. Columns must resolve against the permitted (non-sensitive) column set; `SELECT *` is expanded to permitted
     columns only.
  8. Subquery nesting depth, join count, and SQL length limits.
  9. LIMIT is added when missing and clamped to the configured maximum.
The SQL that is executed is the normalized output of this check, so what was validated is what runs.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify

from app.models import PolicyResult, SchemaModel, Violation

DIALECT = "postgres"

FORBIDDEN_NODES: dict[type, str] = {
    exp.Insert: "write_operation", exp.Update: "write_operation", exp.Delete: "write_operation",
    exp.Merge: "write_operation", exp.Create: "ddl_operation", exp.Drop: "ddl_operation", exp.Alter: "ddl_operation",
    exp.TruncateTable: "ddl_operation", exp.Copy: "copy_operation", exp.Command: "unsupported_command",
    exp.Set: "session_change", exp.Transaction: "transaction_control", exp.Commit: "transaction_control",
    exp.Rollback: "transaction_control", exp.Lock: "row_locking", exp.Into: "select_into",
}
for _name in ("Grant", "Revoke", "Use", "Pragma", "Analyze", "Describe", "Cache", "Uncache", "Refresh", "LoadData"):
    if hasattr(exp, _name):
        FORBIDDEN_NODES[getattr(exp, _name)] = "unsupported_command"

ALLOWED_FUNCTION_CLASSES = {
    "Count", "Sum", "Avg", "Min", "Max", "Round", "Coalesce", "Nullif", "Cast", "TryCast", "Case", "If", "Extract",
    "TimestampTrunc", "DateTrunc", "Lower", "Upper", "Length", "Trim", "Concat", "Substring", "Abs", "Ceil", "Floor",
    "Greatest", "Least", "ToChar", "RowNumber", "Rank", "DenseRank", "Stddev", "StddevSamp", "StddevPop", "Variance",
    "VariancePop", "PercentileCont", "PercentileDisc", "CountIf", "Div", "Mod", "Pow", "Sqrt", "Ln", "Exp", "Sign",
    "DatePart", "Initcap", "Left", "Right", "Replace", "StrPosition", "Lag", "Lead", "FirstValue", "LastValue",
    "NthValue", "Ntile", "CumeDist", "PercentRank", "ArrayAgg", "GroupConcat", "StringAgg", "Date", "DateDiff",
    "DateAdd", "DateSub", "CurrentDate", "CurrentTimestamp", "Interval", "Filter", "Distinct", "Bracket",
    "BoolAnd", "BoolOr", "LogicalAnd", "LogicalOr", "Corr", "CovarSamp", "CovarPop", "Quantile",
    "ApproxDistinct", "Trunc", "TimeToStr", "StrToDate", "TsOrDsToDate", "Lpad", "Rpad", "Split", "Repeat",
    "MakeInterval", "Pad", "Btrim", "Chr", "AtTimeZone", "And", "Or", "Xor", "Not", "Exists", "MakeDate",
    "DateFromParts",
}
ALLOWED_ANONYMOUS = {"date_part", "to_char", "to_date", "age", "justify_days", "initcap", "btrim", "make_date",
                     "date_bin", "width_bucket", "percentile_cont", "nullif", "split_part", "string_agg"}
# Named only to produce clearer messages; anything not allowlisted is rejected regardless.
DANGEROUS_FUNCTIONS = {"pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import",
                       "lo_export", "dblink", "dblink_exec", "set_config", "current_setting", "pg_terminate_backend",
                       "pg_cancel_backend", "pg_reload_conf", "query_to_xml", "nextval", "setval", "txid_current",
                       "pg_advisory_lock", "copy", "version", "pg_sleep_for", "pg_sleep_until", "generate_series"}
CURRENT_TIME_FUNCS = {"CurrentDate", "CurrentTimestamp", "CurrentTime"}

_NAME_RE = re.compile(r"(?:Column '([^']+)' could not be resolved|Unknown column: ([A-Za-z0-9_\"]+))")


def _violation(rule: str, message: str, stage: str = "policy") -> Violation:
    return Violation(stage=stage, rule=rule, message=message)


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return type(node).__name__


class SQLPolicy:
    def __init__(self, schema: SchemaModel, max_rows: int = 1000, max_depth: int = 3, max_joins: int = 6,
                 max_length: int = 8000) -> None:
        self.schema = schema
        self.schema_name = schema.schema_name
        self.max_rows = max_rows
        self.max_depth = max_depth
        self.max_joins = max_joins
        self.max_length = max_length
        self.allowed = {t: {c.name: c.type for c in ti.allowed_columns()} for t, ti in schema.tables.items()}
        self.all_columns = {t: {c.name for c in ti.columns} for t, ti in schema.tables.items()}

    # ------------------------------------------------------------------ public
    def check(self, sql: str | None) -> PolicyResult:
        if not sql or not sql.strip():
            return PolicyResult(allowed=False, violations=[_violation("empty_sql", "No SQL to validate.", "parse")])
        if len(sql) > self.max_length:
            return PolicyResult(allowed=False, violations=[_violation(
                "sql_too_long", f"SQL exceeds {self.max_length} characters.", "parse")])
        if "\x00" in sql:
            return PolicyResult(allowed=False, violations=[_violation("invalid_bytes", "SQL contains NUL bytes.",
                                                                      "parse")])
        try:
            statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
        except (ParseError, TokenError) as exc:
            return PolicyResult(allowed=False, violations=[_violation(
                "parse_error", f"SQL could not be parsed: {str(exc).splitlines()[0][:200]}", "parse")])
        if len(statements) != 1:
            return PolicyResult(allowed=False, violations=[_violation(
                "multiple_statements", f"Exactly one statement is allowed; found {len(statements)}.", "parse")])
        tree = statements[0]
        violations: list[Violation] = []
        warnings: list[str] = []

        if not isinstance(tree, (exp.Select, exp.SetOperation)):
            rule = FORBIDDEN_NODES.get(type(tree), "non_select_statement")
            violations.append(_violation(rule, f"Only read-only SELECT queries are allowed (got {type(tree).__name__.upper()})."))
            return PolicyResult(allowed=False, violations=violations)

        for node in tree.walk():
            for cls, rule in FORBIDDEN_NODES.items():
                if isinstance(node, cls):
                    violations.append(_violation(rule, f"Forbidden construct inside query: {cls.__name__.upper()}."))
                    break
        if violations:
            return PolicyResult(allowed=False, violations=_dedupe(violations))

        with_ = tree.args.get("with") or tree.args.get("with_")
        if with_ is not None and with_.args.get("recursive"):
            violations.append(_violation("recursive_cte", "Recursive CTEs are not allowed."))
        cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
        for cte in tree.find_all(exp.CTE):
            if not isinstance(cte.this, (exp.Select, exp.SetOperation)):
                violations.append(_violation("write_capable_cte", f"CTE '{cte.alias_or_name}' is not a SELECT."))

        for fn in tree.find_all(exp.Func):
            name = _func_name(fn)
            if isinstance(fn, exp.Anonymous):
                if name in DANGEROUS_FUNCTIONS:
                    violations.append(_violation("dangerous_function", f"Function '{name}' is not allowed."))
                elif name not in ALLOWED_ANONYMOUS:
                    violations.append(_violation("disallowed_function", f"Function '{name}' is not on the allowlist."))
            elif name not in ALLOWED_FUNCTION_CLASSES:
                violations.append(_violation("disallowed_function",
                                             f"Function '{fn.sql_name().lower()}' is not on the allowlist."))
            if name in CURRENT_TIME_FUNCS:
                warnings.append("Query uses the current date/time; the dataset is static "
                                f"({self.schema.data_start}..{self.schema.data_end}), so results may be empty.")

        referenced_tables: set[str] = set()
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                violations.append(_violation("table_function", "Table functions in FROM are not allowed."))
                continue
            name, db, catalog = table.name.lower(), (table.db or "").lower(), (table.catalog or "").lower()
            if not db and name in cte_names:
                continue
            if catalog:
                violations.append(_violation("unauthorized_object", f"Cross-database reference '{table.sql()}' is not allowed."))
            elif db and db != self.schema_name:
                violations.append(_violation("unauthorized_object", f"Schema '{db}' is not permitted."))
            elif name not in self.allowed:
                violations.append(_violation("unknown_table", f"Table '{name}' does not exist in schema '{self.schema_name}'."))
            else:
                referenced_tables.add(name)
                if not db:
                    table.set("db", exp.to_identifier(self.schema_name))

        depth = _max_select_depth(tree)
        if depth > self.max_depth:
            violations.append(_violation("nesting_depth", f"Subquery nesting depth {depth} exceeds limit {self.max_depth}."))
        joins = len(list(tree.find_all(exp.Join)))
        if joins > self.max_joins:
            violations.append(_violation("too_many_joins", f"{joins} joins exceed the limit of {self.max_joins}."))
        if violations:
            return PolicyResult(allowed=False, violations=_dedupe(violations), warnings=warnings,
                                referenced_tables=sorted(referenced_tables), subquery_depth=depth, join_count=joins)

        # Column resolution against the permitted (non-sensitive) schema.
        try:
            qualified = qualify(tree.copy(), schema={self.schema_name: self.allowed}, dialect=DIALECT,
                                validate_qualify_columns=True, quote_identifiers=False)
        except OptimizeError as exc:
            msg = str(exc).split(". Line")[0]
            m = _NAME_RE.search(msg)
            col = (m.group(1) or m.group(2)).strip('"').lower() if m else None
            if col and any(col in cols and col not in self.allowed[t] for t, cols in self.all_columns.items()):
                v = _violation("restricted_column", f"Column '{col}' is restricted and cannot be queried.")
            elif col:
                v = _violation("unknown_column", f"{msg}. The column does not exist in the referenced tables.")
            else:
                v = _violation("column_resolution", msg[:300])
            return PolicyResult(allowed=False, violations=[v], warnings=warnings,
                                referenced_tables=sorted(referenced_tables), subquery_depth=depth, join_count=joins)
        except Exception as exc:  # sqlglot internal assertion on unusual SQL: fail closed
            return PolicyResult(allowed=False, violations=[_violation(
                "column_resolution", f"Column references could not be validated ({type(exc).__name__}).")],
                warnings=warnings, referenced_tables=sorted(referenced_tables), subquery_depth=depth, join_count=joins)

        referenced_columns = set()
        alias_to_table = {}
        for t in qualified.find_all(exp.Table):
            alias_to_table[t.alias_or_name.lower()] = t.name.lower()
        for col in qualified.find_all(exp.Column):
            tbl = alias_to_table.get((col.table or "").lower())
            if tbl in self.allowed and col.name.lower() in self.allowed[tbl]:
                referenced_columns.add(f"{tbl}.{col.name.lower()}")

        # LIMIT enforcement on the outermost query (the original tree keeps readable aliases).
        limit_added, limit_value, limit_violation = self._enforce_limit(tree)
        if limit_violation:
            return PolicyResult(allowed=False, violations=[limit_violation], warnings=warnings)
        notices = []
        if limit_added:
            notices.append(f"No LIMIT specified; LIMIT {self.max_rows} was applied.")
        elif limit_value == self.max_rows and limit_value is not None:
            notices.append(f"LIMIT is capped at {self.max_rows} rows.")
        final_sql = tree.sql(dialect=DIALECT, pretty=True, comments=False)

        # Defense in depth: the normalized SQL must re-parse to exactly one SELECT.
        reparsed = sqlglot.parse(final_sql, dialect=DIALECT)
        if len(reparsed) != 1 or not isinstance(reparsed[0], (exp.Select, exp.SetOperation)):
            return PolicyResult(allowed=False, violations=[_violation("normalization_mismatch",
                                                                      "Normalized SQL failed re-validation.")])
        return PolicyResult(allowed=True, sql=final_sql, warnings=warnings, notices=notices, referenced_tables=sorted(referenced_tables),
                            referenced_columns=sorted(referenced_columns), limit_applied=limit_value,
                            limit_was_added=limit_added, subquery_depth=depth, join_count=joins)

    # ------------------------------------------------------------------ helpers
    def _enforce_limit(self, tree: exp.Expression) -> tuple[bool, int, Violation | None]:
        limit = tree.args.get("limit")
        if limit is None:
            tree.set("limit", exp.Limit(expression=exp.Literal.number(self.max_rows)))
            return True, self.max_rows, None
        value = limit.expression if isinstance(limit, exp.Limit) else limit
        if isinstance(value, exp.Literal) and value.is_int:
            n = int(value.this)
            if n > self.max_rows:
                limit.set("expression", exp.Literal.number(self.max_rows))
                return False, self.max_rows, None
            return False, n, None
        return False, 0, _violation("non_literal_limit", "LIMIT must be an integer literal.")


def _dedupe(vs: list[Violation]) -> list[Violation]:
    seen, out = set(), []
    for v in vs:
        key = (v.rule, v.message)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _max_select_depth(tree: exp.Expression) -> int:
    """Number of SELECTs nested inside other SELECTs (top-level query = 0)."""
    best = 0
    for node in tree.find_all(exp.Select):
        depth, parent = 0, node.parent
        while parent is not None:
            if isinstance(parent, exp.Select):
                depth += 1
            parent = parent.parent
        best = max(best, depth)
    return best


def redact_literals(sql: str, max_chars: int = 2000) -> str:
    """For audit logs: replace string literals so logged SQL does not retain user-supplied values."""
    redacted = re.sub(r"'(?:[^']|'')*'", "'?'", sql or "")
    return redacted[:max_chars]
