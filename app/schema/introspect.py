"""Schema introspection with SQLAlchemy, merged with curated metadata (descriptions, glossary, sensitivity).

A column is treated as sensitive (excluded from prompts and from the policy allowlist) when EITHER the curated
metadata marks it sensitive OR the runtime role lacks SELECT privilege on it. The second rule makes the
allowlist fail closed if metadata is incomplete.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy import Engine, inspect, text

from app.models import ColumnInfo, ForeignKeyInfo, GlossaryTerm, SchemaModel, TableInfo


def load_metadata(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def introspect_schema(engine: Engine, schema: str, metadata: dict) -> SchemaModel:
    insp = inspect(engine)
    meta_tables = metadata.get("tables", {})
    tables: dict[str, TableInfo] = {}

    with engine.connect() as conn:
        privileges = {
            (r.table_name, r.column_name): bool(r.can_select)
            for r in conn.execute(text(
                "SELECT c.table_name, c.column_name, "
                "has_column_privilege(current_user, quote_ident(c.table_schema)||'.'||quote_ident(c.table_name), "
                "c.column_name, 'SELECT') AS can_select "
                "FROM information_schema.columns c WHERE c.table_schema = :s"), {"s": schema})
        }
        estimates = {
            r.relname: int(max(r.reltuples, 0))
            for r in conn.execute(text(
                "SELECT c.relname, c.reltuples FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relkind = 'r'"), {"s": schema})
        }

    for tname in sorted(insp.get_table_names(schema=schema)):
        tmeta = meta_tables.get(tname, {})
        sensitive = set(tmeta.get("sensitive", []))
        cat_values = tmeta.get("categorical_values", {}) or {}
        col_desc = tmeta.get("columns", {}) or {}
        pk = insp.get_pk_constraint(tname, schema=schema).get("constrained_columns", []) or []
        cols = []
        for c in insp.get_columns(tname, schema=schema):
            name = c["name"]
            is_sensitive = name in sensitive or not privileges.get((tname, name), False)
            cols.append(ColumnInfo(
                name=name, type=str(c["type"]), nullable=bool(c.get("nullable", True)), primary_key=name in pk,
                description=col_desc.get(name), sensitive=is_sensitive,
                categorical_values=[] if is_sensitive else list(cat_values.get(name, [])),
            ))
        unique_cols = {tuple(u["column_names"]) for u in insp.get_unique_constraints(tname, schema=schema)}
        unique_cols |= {tuple(ix["column_names"]) for ix in insp.get_indexes(tname, schema=schema) if ix.get("unique")}
        fks = []
        for fk in insp.get_foreign_keys(tname, schema=schema):
            for col, ref_col in zip(fk["constrained_columns"], fk["referred_columns"]):
                fks.append(ForeignKeyInfo(column=col, ref_table=fk["referred_table"], ref_column=ref_col,
                                          unique=(col,) in unique_cols or [col] == pk))
        tables[tname] = TableInfo(name=tname, schema_name=schema, description=tmeta.get("description"), columns=cols,
                                  primary_key=pk, foreign_keys=fks, row_estimate=estimates.get(tname))

    glossary = [GlossaryTerm(term=k, definition=v.get("definition", ""), tables=v.get("tables", []))
                for k, v in (metadata.get("glossary") or {}).items()]
    span = metadata.get("data_timespan") or {}
    return SchemaModel(schema_name=schema, tables=tables, glossary=glossary,
                       data_start=span.get("start"), data_end=span.get("end"))


def schema_from_dict(raw: dict) -> SchemaModel:
    """Build a SchemaModel from a serialized snapshot (used by offline unit tests)."""
    return SchemaModel.model_validate(raw)
