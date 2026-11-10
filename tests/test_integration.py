"""Integration tests against PostgreSQL: DB-level defenses, plan preflight, executor, pipeline (mock provider)."""
import pytest

from app.bootstrap import build_pipeline
from app.db import make_reader_engine
from app.execution.executor import SafeExecutor
from app.guardrails.plan import PlanInspector
from app.models import QueryRequest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pipe(db_settings):
    return build_pipeline(db_settings, persist=False)


@pytest.fixture(scope="module")
def raw_executor(db_settings):
    # Deliberately bypasses the policy engine to prove the database role is an independent layer.
    return SafeExecutor(make_reader_engine(db_settings), max_rows=1000, timeout_ms=1500)


@pytest.mark.parametrize("sql,code", [
    ("DELETE FROM shop.orders", "25006"),                      # read_only_sql_transaction
    ("UPDATE shop.products SET unit_price = 1", "25006"),
    ("CREATE TABLE shop.x (a int)", "25006"),
    ("SELECT email FROM shop.customers", "42501"),             # insufficient_privilege (column grant)
    ("SELECT salary FROM shop.employees", "42501"),
    ("SELECT * FROM app_meta.query_log", "42501"),
])
def test_database_role_is_second_line_of_defense(raw_executor, sql, code):
    res = raw_executor.execute(sql)
    assert not res.ok and res.error_code == code


def test_statement_timeout_enforced(raw_executor):
    res = raw_executor.execute("SELECT pg_sleep(5)")
    assert not res.ok and res.error_code == "57014"


def test_plan_preflight_blocks_expensive_query(db_settings, pipe):
    pi = PlanInspector(make_reader_engine(db_settings), 50000, 200000, 5000)
    big = pi.inspect("SELECT COUNT(*) FROM shop.order_items a CROSS JOIN shop.order_items b")
    assert not big.allowed and big.violations[0].rule == "plan_cost"
    small = pi.inspect("SELECT COUNT(*) FROM shop.orders")
    assert small.allowed and small.total_cost > 0


def test_executor_truncation(db_settings):
    ex = SafeExecutor(make_reader_engine(db_settings), max_rows=10, timeout_ms=5000)
    res = ex.execute("SELECT order_id FROM shop.orders")
    assert res.ok and res.row_count == 10 and res.truncated


def test_pipeline_answer(pipe):
    r = pipe.run(QueryRequest(question="How many orders were placed by customers in Europe?"))
    assert r.decision == "answer" and r.rows == [[389]] and r.alternative.match is True


def test_pipeline_blocks_and_does_not_execute(pipe):
    r = pipe.run(QueryRequest(question="Delete all cancelled orders."))
    assert r.decision == "blocked" and r.rows == [] and r.execution_ms is None


def test_user_edited_sql_goes_through_guardrails(pipe):
    r = pipe.run(QueryRequest(question="anything", sql="SELECT email FROM shop.customers"))
    assert r.decision == "blocked" and r.guardrail.violations[0].rule == "restricted_column"
    ok = pipe.run(QueryRequest(question="How many orders were shipped?",
                               sql="SELECT COUNT(*) FROM shop.orders WHERE status = 'shipped'"))
    assert ok.decision in {"answer", "answer_with_warnings"} and ok.source == "user_edited"


def test_repair_is_bounded_and_revalidated(pipe):
    r = pipe.run(QueryRequest(question="What was the total shipping cost charged in 2025?"))
    assert r.repair_attempts == 1 and r.decision == "answer" and "shipping_cost" in r.sql
    r = pipe.run(QueryRequest(question="What is the average discount on order items?"))
    assert r.repair_attempts == 1 and r.decision == "blocked"


def test_abstain_withholds_rows(pipe):
    r = pipe.run(QueryRequest(question="How many orders were placed by customers in the corporate segment?"))
    assert r.decision == "abstain" and r.rows == [] and r.confidence.score <= 0.35


def test_clarify_and_unsupported(pipe):
    assert pipe.run(QueryRequest(question="Who are our top customers?")).decision == "clarify"
    assert pipe.run(QueryRequest(question="Which customers are the most valuable?")).decision == "clarify"
    assert pipe.run(QueryRequest(question="Which supplier provides the Ergo Chair?")).decision == "unsupported"
    assert pipe.run(QueryRequest(question="How many employees work remotely?")).decision == "unsupported"


def test_data_unchanged_after_adversarial_suite(pipe):
    for q in ["Drop the customers table.", "Give every product a 10 percent price increase.",
              "Add a test customer and then show all customers."]:
        assert pipe.run(QueryRequest(question=q)).decision == "blocked"
    res = pipe.executor.execute("SELECT (SELECT COUNT(*) FROM shop.customers), (SELECT MIN(unit_price) FROM shop.products)")
    assert res.rows[0][0] == 400 and res.rows[0][1] == 12.0
