"""Unit tests: retrieval, screening, alignment, static checks, comparison, confidence, providers (offline)."""
import pytest

from app.confidence import score_confidence
from app.guardrails.policy import SQLPolicy
from app.llm.base import inline_schema
from app.llm.mock_provider import MockProvider
from app.models import AlternativeComparison, Check, ExecutionResult, GenerationOutput
from app.prompting.builder import build_generation_prompt, load_fewshots, screen_question, select_fewshots
from app.config import Settings
from app.schema.introspect import load_metadata
from app.schema.retrieval import SchemaRetriever
from app.validation.alignment import concept_alignment, describe_sql
from app.validation.checks import check_date_range, check_fan_out, check_joins, post_execution_checks
from app.validation.compare import compare_results
from app.validation.sqlfacts import extract_facts

pytestmark = pytest.mark.unit
META = load_metadata(Settings().metadata_path)


def facts(schema, sql):
    return extract_facts(SQLPolicy(schema).check(sql).sql, schema)


# ------------------------------------------------------------------ retrieval / prompting
def test_retrieval_keeps_fk_bridge(schema):
    res = SchemaRetriever(schema).retrieve("Which region's customers got the most refunds?")
    assert {"customers", "regions", "refunds", "orders"} <= set(res.tables)
    assert "orders" in res.bridge_tables


def test_retrieval_is_focused(schema):
    res = SchemaRetriever(schema).retrieve("How many orders were placed via mobile?")
    assert "employees" not in res.tables and "orders" in res.tables


def test_glossary_forces_tables(schema):
    res = SchemaRetriever(schema).retrieve("What is net revenue for laptops?")
    assert "refunds" in res.tables and "net revenue" in res.glossary_hits


def test_prompt_excludes_sensitive_columns(schema):
    r = SchemaRetriever(schema).retrieve("list customers and employees")
    _, user = build_generation_prompt("list customers", schema, r, select_fewshots("x", load_fewshots(Settings().fewshot_path)))
    assert "email" not in user and "salary" not in user and "<question>" in user
    assert user.count("Q: ") >= 3


@pytest.mark.parametrize("q,kind", [
    ("What was our revenue in 2025?", "clarify"),
    ("Who are our top customers?", "clarify"),
    ("How many orders last month?", "clarify"),
    ("What was our gross revenue in 2025?", None),
    ("What was our revenue in 2025? (Gross revenue)", None),
    ("What is the inventory of tents?", "unsupported"),
    ("Which marketing campaign worked?", "unsupported"),
    ("When did we start selling tents?", None),          # 'start' must not match 'star'
])
def test_screening(q, kind):
    assert screen_question(q, META).kind == kind


# ------------------------------------------------------------------ alignment
def test_alignment_detects_wrong_year(schema):
    f = facts(schema, "SELECT COUNT(*) FROM shop.orders WHERE order_date >= DATE '2024-01-01' AND order_date < DATE '2025-01-01'")
    score, diffs, kinds = concept_alignment("How many orders were placed in 2025?", f, schema)
    assert "year" in kinds and score < 1
    score, diffs, kinds = concept_alignment("How many orders were placed in 2024?", f, schema)
    assert not diffs and score == 1.0


def test_alignment_detects_metric_and_value_and_direction(schema):
    f = facts(schema, "SELECT method, SUM(amount) FROM shop.payments GROUP BY method")
    assert "metric" in concept_alignment("average payment amount by method", f, schema)[2]
    f = facts(schema, "SELECT COUNT(*) FROM shop.orders WHERE status = 'shipped'")
    kinds = concept_alignment("How many delivered orders?", f, schema)[2]
    assert {"value_missing", "value_extra"} <= kinds
    f = facts(schema, "SELECT name FROM shop.products ORDER BY unit_price ASC LIMIT 3")
    assert "direction" in concept_alignment("top 3 most expensive products", f, schema)[2]


def test_alignment_gift_card_not_card(schema):
    f = facts(schema, "SELECT COUNT(*) FROM shop.payments WHERE method = 'gift_card'")
    assert concept_alignment("How many payments were made with a gift card?", f, schema)[1] == []


def test_describe_sql(schema):
    text = describe_sql(facts(schema, "SELECT status, COUNT(*) FROM shop.orders GROUP BY status"))
    assert "COUNT" in text and "grouped by orders.status" in text


# ------------------------------------------------------------------ static checks
def test_join_not_on_fk_fails(schema):
    bad = facts(schema, "SELECT COUNT(*) FROM shop.orders o JOIN shop.customers c ON c.customer_id = o.order_id")
    assert check_joins(bad, schema).status == "fail"
    good = facts(schema, "SELECT COUNT(*) FROM shop.orders o JOIN shop.customers c ON c.customer_id = o.customer_id")
    assert check_joins(good, schema).status == "pass"


def test_fan_out(schema):
    bad = facts(schema, "SELECT SUM(p.amount) FROM shop.payments p JOIN shop.orders o ON o.order_id = p.order_id "
                        "JOIN shop.order_items oi ON oi.order_id = o.order_id")
    assert check_fan_out(bad, schema).status == "warn"
    ok = facts(schema, "SELECT COUNT(DISTINCT o.order_id) FROM shop.orders o JOIN shop.order_items oi ON oi.order_id = o.order_id")
    assert check_fan_out(ok, schema).status == "pass"
    one_to_one = facts(schema, "SELECT SUM(o.shipping_cost) FROM shop.orders o LEFT JOIN shop.refunds r ON r.order_id = o.order_id")
    assert check_fan_out(one_to_one, schema).status == "pass"   # refunds.order_id is UNIQUE


def test_date_range_outside_data(schema):
    f = facts(schema, "SELECT COUNT(*) FROM shop.orders WHERE order_date >= DATE '2019-01-01' AND order_date < DATE '2020-01-01'")
    assert check_date_range(f, schema).status == "warn"


def test_post_checks(schema):
    f = facts(schema, "SELECT customer_id, city FROM shop.customers")
    res = ExecutionResult(ok=True, columns=["customer_id", "city"], rows=[[1, None], [2, None], [3, None], [3, None]],
                          row_count=4)
    statuses = {c.name: c.status for c in post_execution_checks(f, res, schema)}
    assert statuses["null_profile"] == "warn" and statuses["duplicate_rows"] == "warn"
    f = facts(schema, "SELECT COUNT(*) AS orders FROM shop.orders")
    res = ExecutionResult(ok=True, columns=["orders"], rows=[[99999]], row_count=1)
    assert {c.name: c.status for c in post_execution_checks(f, res, schema)}["value_plausibility"] == "warn"


# ------------------------------------------------------------------ comparison
def test_compare_order_insensitive_with_tolerance_nulls_duplicates():
    a = [["x", 1.0000001], ["y", None], ["y", None]]
    b = [["y", None], ["x", 1.0], ["y", None]]
    assert compare_results(["a", "b"], a, ["c", "d"], b)[0]
    assert not compare_results(["a"], [[1], [1]], ["a"], [[1], [2]])[0]
    assert not compare_results(["a"], [[1], [2]], ["a"], [[2], [1]], ordered=True)[0]
    assert not compare_results(["a", "b"], [[1, 2]], ["a"], [[1]])[0]
    assert compare_results(["a"], [[100.004]], ["a"], [[100.0]], abs_tol=0.01)[0]


# ------------------------------------------------------------------ confidence
def _checks(status="pass"):
    return [Check(name="join_keys", phase="pre_execution", status=status, score=1.0 if status == "pass" else 0.0,
                  detail=""),
            Check(name="non_empty", phase="post_execution", status="pass", score=1.0, detail="")]


def test_confidence_renormalizes_and_lists_unavailable():
    rep = score_confidence(repair_attempts=0, concept_score=1.0, concept_diffs=[], back_translation=None,
                           checks=_checks(), alternative=None, self_reported=0.2, executed=True)
    assert rep.score == 1.0
    assert set(rep.unavailable) == {"llm_back_translation", "multi_query_agreement"}
    assert "NOT a calibrated probability" in rep.note


def test_confidence_caps():
    fail = score_confidence(repair_attempts=0, concept_score=1.0, concept_diffs=[], back_translation=None,
                            checks=_checks("fail"), alternative=None, self_reported=None, executed=True)
    assert fail.score <= 0.35
    disagree = score_confidence(repair_attempts=0, concept_score=1.0, concept_diffs=[], back_translation=None,
                                checks=_checks(), self_reported=None, executed=True,
                                alternative=AlternativeComparison(attempted=True, reason="", match=False, detail="x"))
    assert disagree.score <= 0.55
    critical = score_confidence(repair_attempts=0, concept_score=0.65, concept_diffs=["y"], concept_kinds={"year"},
                                back_translation=None, checks=_checks(), alternative=None, self_reported=None,
                                executed=True)
    assert critical.score <= 0.55 and critical.label == "low"


# ------------------------------------------------------------------ providers
def test_mock_provider_replays_and_defaults():
    mp = MockProvider(fixtures=[{"question": "Q one?", "gold_sql": "SELECT 1"},
                                {"question": "Q two", "mock": {"generation": {"status": "unsupported",
                                                                              "unsupported_reason": "no"}}}])
    assert mp.generate("", "", question="q ONE").sql == "SELECT 1"
    assert mp.generate("", "", question="Q two").status == "unsupported"
    assert mp.generate("", "", question="unknown").status == "unsupported"
    assert mp.back_translate("", "", question="x", sql="y") is None and not mp.is_live


def test_structured_schema_is_inlined():
    sch = inline_schema(GenerationOutput)
    assert "$defs" not in str(sch) and sch["properties"]["status"]["enum"] == ["sql", "clarification", "unsupported"]
