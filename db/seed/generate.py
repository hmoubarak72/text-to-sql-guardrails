"""Deterministic seed-data generator for the `shop` schema.

Run `python db/seed/generate.py` to (re)write db/init/02_seed.sql. The output is fully determined by
SEED, so expected evaluation results are reproducible. `build_dataset()` is also imported by tests
to cross-check golden answers with pure-Python computations that are independent of SQL.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SEED = 20240101
START = date(2024, 1, 1)
END = date(2025, 12, 31)

REGIONS = ["North America", "Europe", "Middle East & Africa", "Asia Pacific", "Latin America"]
CITIES = {
    "North America": [("Houston", "USA"), ("Chicago", "USA"), ("Toronto", "Canada"), ("Calgary", "Canada"), ("Denver", "USA")],
    "Europe": [("Amsterdam", "Netherlands"), ("Berlin", "Germany"), ("Madrid", "Spain"), ("Lyon", "France")],
    "Middle East & Africa": [("Dubai", "UAE"), ("Kuwait City", "Kuwait"), ("Tripoli", "Libya"), ("Lagos", "Nigeria")],
    "Asia Pacific": [("Singapore", "Singapore"), ("Sydney", "Australia"), ("Osaka", "Japan")],
    "Latin America": [("Bogota", "Colombia"), ("Santiago", "Chile"), ("Monterrey", "Mexico")],
}
FIRST = ["Amira", "Ben", "Chloe", "Daniel", "Elena", "Farid", "Grace", "Hassan", "Ines", "Jamal", "Keiko", "Liam",
         "Maya", "Noah", "Omar", "Priya", "Quinn", "Rania", "Sofia", "Tariq", "Uma", "Victor", "Wen", "Yusuf", "Zara"]
LAST = ["Abbott", "Barakat", "Chen", "Diaz", "Evans", "Farah", "Garcia", "Haddad", "Ito", "Johnson", "Khalil", "Lopez",
        "Mansour", "Nakamura", "Okafor", "Patel", "Rossi", "Saleh", "Tanaka", "Usman", "Vogel", "Williams"]
SEGMENTS = [("consumer", 0.6), ("corporate", 0.25), ("small_business", 0.15)]

CATEGORIES = [
    (1, "Electronics", None), (2, "Home & Kitchen", None), (3, "Outdoors", None), (4, "Office", None),
    (5, "Laptops", 1), (6, "Audio", 1), (7, "Cookware", 2), (8, "Camping", 3), (9, "Furniture", 4),
]
PRODUCTS = {
    5: [("Aero 13 Laptop", 1199), ("Aero 15 Laptop", 1499), ("Workhorse 14 Laptop", 899), ("Student 11 Laptop", 449)],
    6: [("Pulse Wireless Earbuds", 129), ("Studio Headphones", 249), ("Boom Speaker", 89), ("Mini Speaker", 39),
        ("Noise-Cancel Headphones", 329)],
    1: [("USB-C Hub", 49), ("4K Monitor", 399), ("Webcam Pro", 99), ("Charging Dock", 59)],
    7: [("Cast Iron Skillet", 45), ("Nonstick Pan Set", 129), ("Dutch Oven", 89), ("Chef Knife", 75),
        ("Espresso Maker", 219)],
    2: [("Air Purifier", 179), ("Robot Vacuum", 349), ("Smart Kettle", 69)],
    8: [("Trail Tent 2P", 199), ("Trail Tent 4P", 329), ("Sleeping Bag", 119), ("Camp Stove", 79), ("Headlamp", 29)],
    3: [("Hiking Backpack", 139), ("Water Filter", 49), ("Trekking Poles", 59)],
    9: [("Ergo Chair", 349), ("Standing Desk", 549), ("Bookshelf", 159), ("Monitor Arm", 99)],
    4: [("Notebook Pack", 12), ("Desk Lamp", 45), ("Label Printer", 129), ("Paper Shredder", 89)],
}
STATUSES = [("delivered", 0.70), ("shipped", 0.08), ("pending", 0.04), ("cancelled", 0.10), ("returned", 0.08)]
CHANNELS = [("web", 0.5), ("mobile", 0.35), ("store", 0.15)]
METHODS = [("card", 0.6), ("paypal", 0.2), ("bank_transfer", 0.12), ("gift_card", 0.08)]
REASONS = ["damaged", "wrong_item", "late_delivery", "changed_mind"]
JOB_TITLES = ["Account Manager", "Sales Associate", "Regional Director", "Support Specialist"]


def _money(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _pick(rng: random.Random, weighted):
    r = rng.random()
    acc = 0.0
    for value, w in weighted:
        acc += w
        if r <= acc:
            return value
    return weighted[-1][0]


def _rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def build_dataset() -> dict[str, list[dict]]:
    rng = random.Random(SEED)
    data: dict[str, list[dict]] = {k: [] for k in
                                   ["regions", "customers", "categories", "products", "orders", "order_items",
                                    "payments", "refunds", "employees"]}
    for i, name in enumerate(REGIONS, start=1):
        data["regions"].append({"region_id": i, "name": name})

    for i in range(1, 401):
        region_id = rng.choices(range(1, 6), weights=[35, 25, 20, 12, 8])[0]
        city, country = rng.choice(CITIES[REGIONS[region_id - 1]])
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        data["customers"].append({
            "customer_id": i, "first_name": fn, "last_name": ln,
            "email": f"{fn.lower()}.{ln.lower()}.{i}@example.com",
            "phone": f"+1-555-{rng.randint(1000000, 9999999)}" if rng.random() > 0.1 else None,
            "city": city, "country": country, "region_id": region_id,
            "segment": _pick(rng, SEGMENTS),
            "signup_date": _rand_date(rng, date(2023, 1, 1), date(2025, 10, 31)),
        })

    for cid, name, parent in CATEGORIES:
        data["categories"].append({"category_id": cid, "name": name, "parent_category_id": parent})

    pid = 0
    for cat_id, items in PRODUCTS.items():
        for name, price in items:
            pid += 1
            data["products"].append({
                "product_id": pid, "name": name, "category_id": cat_id, "unit_price": _money(price),
                "unit_cost": _money(price * rng.uniform(0.45, 0.7)),
                "is_active": rng.random() > 0.12,
                "launched_on": _rand_date(rng, date(2022, 1, 1), date(2025, 6, 30)),
            })
    products = data["products"]

    oid = item_id = pay_id = ref_id = 0
    for c in data["customers"]:
        n_orders = rng.choices([0, 1, 2, 3, 4, 5, 6, 8, 10], weights=[6, 14, 18, 18, 14, 10, 8, 7, 5])[0]
        first_possible = max(START, c["signup_date"])
        if first_possible > END:
            continue
        for _ in range(n_orders):
            oid += 1
            od = _rand_date(rng, first_possible, END)
            status = _pick(rng, STATUSES)
            if od > END - timedelta(days=10) and status == "delivered":
                status = "shipped"
            order = {"order_id": oid, "customer_id": c["customer_id"], "order_date": od, "status": status,
                     "channel": _pick(rng, CHANNELS),
                     "shipping_cost": _money(0 if rng.random() < 0.3 else rng.choice([4.99, 7.99, 12.5, 19.99]))}
            data["orders"].append(order)
            total = Decimal("0")
            for p in rng.sample(products, rng.choices([1, 2, 3, 4], weights=[45, 30, 17, 8])[0]):
                item_id += 1
                qty = rng.choices([1, 2, 3, 5], weights=[70, 18, 8, 4])[0]
                disc = _money(rng.choice([0, 0, 0, 0.05, 0.1, 0.15, 0.2]))
                price = p["unit_price"]
                data["order_items"].append({"order_item_id": item_id, "order_id": oid, "product_id": p["product_id"],
                                            "quantity": qty, "unit_price": price, "discount": disc})
                total += price * qty * (1 - disc)
            total = _money(total + order["shipping_cost"])
            if status != "cancelled":
                pay_id += 1
                data["payments"].append({"payment_id": pay_id, "order_id": oid, "method": _pick(rng, METHODS),
                                         "amount": total, "paid_at": min(END, od + timedelta(days=rng.randint(0, 2)))})
            if status == "returned" or (status == "delivered" and rng.random() < 0.03):
                ref_id += 1
                amt = total if status == "returned" else _money(total * Decimal(str(rng.choice([0.25, 0.5]))))
                data["refunds"].append({"refund_id": ref_id, "order_id": oid, "amount": max(amt, Decimal("1.00")),
                                        "reason": rng.choice(REASONS),
                                        "refunded_at": min(END, od + timedelta(days=rng.randint(5, 30)))})

    for i in range(1, 26):
        data["employees"].append({
            "employee_id": i, "full_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "job_title": rng.choice(JOB_TITLES), "region_id": rng.randint(1, 5),
            "salary": _money(rng.randint(48, 160) * 1000), "hired_on": _rand_date(rng, date(2015, 1, 1), date(2025, 6, 1)),
        })
    return data


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


ORDER = ["regions", "customers", "categories", "products", "orders", "order_items", "payments", "refunds", "employees"]


def to_sql(data: dict[str, list[dict]]) -> str:
    out = ["-- GENERATED by db/seed/generate.py (seed=%d). Do not edit by hand." % SEED, "BEGIN;"]
    for table in ORDER:
        rows = data[table]
        cols = list(rows[0].keys())
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            values = ",\n".join("(" + ", ".join(_lit(r[c]) for c in cols) + ")" for r in chunk)
            out.append(f"INSERT INTO shop.{table} ({', '.join(cols)}) VALUES\n{values};")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    ds = build_dataset()
    target = Path(__file__).resolve().parents[1] / "init" / "02_seed.sql"
    target.write_text(to_sql(ds), encoding="utf-8")
    print(f"wrote {target} :: " + ", ".join(f"{k}={len(v)}" for k, v in ds.items()))
