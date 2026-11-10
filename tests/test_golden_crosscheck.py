"""Verifies golden expected results independently of SQL: recomputes answers in pure Python from the
deterministic seed generator and compares with eval/expected_results.json (produced by executing gold SQL)."""
import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from db.seed.generate import build_dataset

pytestmark = pytest.mark.unit
EXPECTED = json.loads((Path(__file__).parents[1] / "eval" / "expected_results.json").read_text())
D = build_dataset()
ORDERS = {o["order_id"]: o for o in D["orders"]}


def rev(items):
    return round(float(sum(Decimal(i["quantity"]) * i["unit_price"] * (1 - i["discount"]) for i in items)), 2)


def live_items(year=None):
    return [i for i in D["order_items"] if ORDERS[i["order_id"]]["status"] != "cancelled"
            and (year is None or ORDERS[i["order_id"]]["order_date"].year == year)]


def test_counts():
    assert EXPECTED["L01"]["rows"] == [[len(D["customers"])]]
    assert EXPECTED["D01"]["rows"] == [[sum(o["order_date"].year == 2024 for o in D["orders"])]]
    assert EXPECTED["D04"]["rows"] == [[sum(o["order_date"].strftime("%Y-%m") == "2025-03" for o in D["orders"])]]
    assert EXPECTED["B01"]["rows"] == [[sum(o["status"] == "cancelled" for o in D["orders"])]]


def test_status_breakdown():
    c = Counter(o["status"] for o in D["orders"])
    assert {r[0]: r[1] for r in EXPECTED["A01"]["rows"]} == dict(c)


def test_europe_orders():
    eu = {c["customer_id"] for c in D["customers"] if c["region_id"] == 2}
    assert EXPECTED["J01"]["rows"] == [[sum(o["customer_id"] in eu for o in D["orders"])]]


def test_gross_revenue_total_and_2025():
    assert EXPECTED["A03"]["rows"][0][0] == pytest.approx(rev(live_items()), abs=0.01)
    assert EXPECTED["C01F"]["rows"][0][0] == pytest.approx(rev(live_items(2025)), abs=0.01)


def test_top_category_2025():
    prod = {p["product_id"]: p for p in D["products"]}
    cats = {c["category_id"]: c["name"] for c in D["categories"]}
    by_cat = Counter()
    for i in live_items(2025):
        by_cat[cats[prod[i["product_id"]]["category_id"]]] += float(i["quantity"] * i["unit_price"] * (1 - i["discount"]))
    name, value = by_cat.most_common(1)[0]
    assert EXPECTED["J03"]["rows"][0][0] == name
    assert EXPECTED["J03"]["rows"][0][1] == pytest.approx(value, abs=0.01)


def test_laptop_orders_distinct():
    prod = {p["product_id"]: p for p in D["products"]}
    orders = {i["order_id"] for i in D["order_items"] if prod[i["product_id"]]["category_id"] == 5}
    assert EXPECTED["H05"]["rows"] == [[len(orders)]]


def test_weekend_orders_2024():
    n = sum(o["order_date"].year == 2024 and o["order_date"].weekday() >= 5 for o in D["orders"])
    assert EXPECTED["D06"]["rows"] == [[n]]


def test_signups_2025():
    assert EXPECTED["D03"]["rows"] == [[sum(date(2025, 1, 1) <= c["signup_date"] <= date(2025, 12, 31)
                                            for c in D["customers"])]]
