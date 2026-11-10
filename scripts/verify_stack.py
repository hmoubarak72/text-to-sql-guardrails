"""Post-deployment verification against a running API (Docker Compose or native).

    python scripts/verify_stack.py --api http://localhost:8000 --ui http://localhost:8501

Runs checks 2-8 of the verification plan and prints a JSON report; exits non-zero on failure.
Expects LLM_PROVIDER=mock (scripted responses) unless --live is passed, in which case model-dependent
expectations (exact values) are relaxed.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--ui", default="http://localhost:8501")
    ap.add_argument("--token", default="")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    h = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    sid = "verify_" + uuid.uuid4().hex[:8]
    c = httpx.Client(base_url=a.api, headers=h, timeout=120)
    report, ok_all = [], True

    def q(question, sql=None):
        body = {"question": question, "session_id": sid}
        if sql:
            body["sql"] = sql
        return c.post("/v1/query", json=body).json()

    def check(name, ok, detail):
        nonlocal ok_all
        ok_all &= bool(ok)
        report.append({"check": name, "ok": bool(ok), "detail": detail})

    r = c.get("/readyz")
    check("2_database_initialized_and_api_ready", r.status_code == 200 and r.json().get("ready"), r.json())
    sch = c.get("/v1/schema").json()
    check("2b_schema_introspected", len(sch.get("tables", [])) == 9,
          {"tables": [t["name"] for t in sch.get("tables", [])]})
    try:
        ui = httpx.get(a.ui.rstrip("/") + "/_stcore/health", timeout=10)
        check("3_frontend_reachable", ui.status_code == 200, ui.text[:50])
    except Exception as exc:
        check("3_frontend_reachable", False, str(exc))

    r = q("How many orders were placed by customers in Europe?")
    exp = r.get("rows") == [[389]] if not a.live else r.get("decision") in {"answer", "answer_with_warnings"}
    check("4_valid_question_expected_result", exp and r["decision"] in {"answer", "answer_with_warnings"},
          {"decision": r["decision"], "rows": r.get("rows"), "confidence": (r.get("confidence") or {}).get("score")})

    before = q("How many orders were cancelled?").get("rows")
    r = q("Delete all cancelled orders.")
    r2 = q("How many orders are there?", sql="DELETE FROM shop.orders WHERE status = 'cancelled'")
    after = q("How many orders were cancelled?").get("rows")
    blocked = (r["decision"] in {"blocked", "unsupported"}) and r2["decision"] == "blocked"
    check("5_destructive_query_blocked", blocked and before == after,
          {"generated": [r["decision"], r.get("decision_reason")], "edited": [r2["decision"], r2.get("decision_reason")],
           "cancelled_before": before, "cancelled_after": after})

    r = q("What was our revenue in 2025?")
    check("6_ambiguous_question_clarification", r["decision"] == "clarify",
          {"decision": r["decision"], "interpretations": [i["label"] for i in (r.get("clarification") or {}).get("interpretations", [])]})

    r = q("What was the total shipping cost charged in 2025?")
    r3 = q("What is the average discount on order items?")
    check("7_invalid_schema_reference_repaired_or_rejected",
          (r.get("repair_attempts", 0) >= 1 or a.live) and r["decision"] in {"answer", "answer_with_warnings", "blocked"}
          and (r3["decision"] == "blocked" or a.live),
          {"repaired": [r["decision"], r.get("repair_attempts"), r.get("original_sql", "")[:80]],
           "unrepairable": [r3["decision"], r3.get("decision_reason", "")[:160]]})

    r = q("How many orders included at least one Laptops product?")
    conf = r.get("confidence") or {}
    check("8_validation_warnings_and_confidence_details",
          r["decision"] != "answer" and len(conf.get("signals", [])) >= 7 and r.get("warnings"),
          {"decision": r["decision"], "score": conf.get("score"), "unavailable": conf.get("unavailable"),
           "warnings": r.get("warnings", [])[:3]})

    hist = c.get("/v1/history", params={"session_id": sid}).json()
    fb = c.post("/v1/feedback", json={"query_id": hist["items"][0]["query_id"], "session_id": sid, "verdict": "incorrect",
                                      "comment": "verification run"})
    check("history_and_feedback", len(hist["items"]) >= 8 and fb.status_code == 201,
          {"history_items": len(hist["items"]), "feedback_status": fb.status_code})

    print(json.dumps({"session_id": sid, "all_ok": ok_all, "checks": report}, indent=1, default=str))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
