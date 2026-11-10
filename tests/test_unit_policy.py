"""Unit tests: SQL policy engine (offline, deterministic)."""
import pytest

from app.guardrails.policy import SQLPolicy, redact_literals

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def policy(schema):
    return SQLPolicy(schema, max_rows=1000, max_depth=3, max_joins=6)


@pytest.mark.parametrize("sql,rule", [
    ("DELETE FROM shop.orders", "write_operation"),
    ("UPDATE shop.products SET unit_price = 0", "write_operation"),
    ("INSERT INTO shop.regions VALUES (9, 'x')", "write_operation"),
    ("DROP TABLE shop.orders", "ddl_operation"),
    ("CREATE TABLE shop.x (a int)", "ddl_operation"),
    ("ALTER TABLE shop.orders ADD COLUMN x int", "ddl_operation"),
    ("TRUNCATE shop.orders", "ddl_operation"),
    ("SELECT 1; DROP TABLE shop.orders", "multiple_statements"),
    ("SELECT 1; SELECT 2", "multiple_statements"),
    ("WITH d AS (DELETE FROM shop.orders RETURNING *) SELECT * FROM d", "write_operation"),
    ("WITH u AS (UPDATE shop.orders SET status='x' RETURNING *) SELECT count(*) FROM u", "write_operation"),
    ("SELECT * FROM shop.orders FOR UPDATE", "row_locking"),
    ("SELECT * INTO shop.copy FROM shop.orders", "select_into"),
    ("COPY shop.orders TO '/tmp/x'", "copy_operation"),
    ("SET ROLE postgres", "unsupported_command"),
    ("SELECT pg_sleep(10)", "dangerous_function"),
    ("SELECT pg_read_file('/etc/passwd')", "dangerous_function"),
    ("SELECT set_config('role', 'postgres', false)", "dangerous_function"),
    ("SELECT current_setting('data_directory')", "dangerous_function"),
    ("SELECT dblink('host=x', 'select 1')", "dangerous_function"),
    ("SELECT some_unknown_udf(order_id) FROM shop.orders", "disallowed_function"),
    ("SELECT * FROM pg_catalog.pg_shadow", "unauthorized_object"),
    ("SELECT * FROM information_schema.tables", "unauthorized_object"),
    ("SELECT * FROM app_meta.query_log", "unauthorized_object"),
    ("SELECT * FROM otherdb.shop.orders", "unauthorized_object"),
    ("SELECT * FROM shop.secret_table", "unknown_table"),
    ("SELECT email FROM shop.customers", "restricted_column"),
    ("SELECT c.phone FROM shop.customers c", "restricted_column"),
    ("SELECT AVG(salary) FROM shop.employees", "restricted_column"),
    ("SELECT o.total_amount FROM shop.orders o", "unknown_column"),
    ("SELECT * FROM generate_series(1, 10)", "table_function"),
    ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT * FROM r", "recursive_cte"),
    ("SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM shop.orders) a) b) c) d",
     "nesting_depth"),
    ("SELECT * FROM shop.orders LIMIT (SELECT 5)", "non_literal_limit"),
    ("SELEC * FROM shop.orders", "parse_error"),
    ("", "empty_sql"),
])
def test_blocked(policy, sql, rule):
    res = policy.check(sql)
    assert not res.allowed
    assert rule in {v.rule for v in res.violations}, res.violations


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM shop.orders",
    "SELECT status, COUNT(*) FROM orders GROUP BY status",
    "SELECT o.order_id FROM shop.orders o JOIN shop.customers c ON c.customer_id = o.customer_id",
    "WITH t AS (SELECT customer_id, COUNT(*) n FROM shop.orders GROUP BY 1) SELECT * FROM t ORDER BY n DESC",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT DATE_TRUNC('month', order_date) m, COUNT(*) FROM shop.orders GROUP BY 1",
    "SELECT EXTRACT(YEAR FROM order_date)::int, COUNT(*) FROM shop.orders GROUP BY 1",
    "SELECT 'drop table x; --' AS s FROM shop.orders",
    "SELECT name FROM shop.products WHERE name ILIKE '%delete%'",
    "SELECT * FROM shop.orders o WHERE EXISTS (SELECT 1 FROM shop.refunds r WHERE r.order_id = o.order_id)",
    "SELECT ROW_NUMBER() OVER (ORDER BY order_date) FROM shop.orders",
])
def test_allowed(policy, sql):
    res = policy.check(sql)
    assert res.allowed, res.violations


def test_limit_added_and_clamped(policy):
    added = policy.check("SELECT * FROM shop.orders")
    assert added.allowed and added.limit_was_added and added.limit_applied == 1000
    assert "LIMIT 1000" in added.sql
    clamped = policy.check("SELECT * FROM shop.orders LIMIT 99999")
    assert clamped.limit_applied == 1000 and "99999" not in clamped.sql
    kept = policy.check("SELECT * FROM shop.orders LIMIT 5")
    assert kept.limit_applied == 5 and not kept.limit_was_added


def test_star_expands_to_permitted_columns_only(policy):
    res = policy.check("SELECT * FROM shop.customers")
    assert res.allowed
    assert "customers.email" not in res.referenced_columns
    assert "customers.city" in res.referenced_columns


def test_unqualified_tables_get_schema(policy):
    res = policy.check("SELECT COUNT(*) FROM orders")
    assert "shop.orders" in res.sql


def test_comments_stripped(policy):
    res = policy.check("SELECT 1 /* ignore previous instructions */ -- drop\n")
    assert res.allowed and "ignore" not in res.sql


def test_redact_literals():
    assert redact_literals("SELECT * FROM t WHERE email = 'a@b.com'") == "SELECT * FROM t WHERE email = '?'"
