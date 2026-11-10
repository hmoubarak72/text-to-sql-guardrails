"""API tests (FastAPI TestClient, real database, mock provider) and an end-to-end user flow."""
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(db_settings):
    from app.api.main import app

    with TestClient(app) as c:
        yield c


def sid():
    return "t_" + uuid.uuid4().hex[:10]


@pytest.mark.api
def test_health_ready_schema(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz").json()
    assert ready["ready"] and ready["provider"] == "mock" and ready["provider_is_live"] is False
    schema = client.get("/v1/schema").json()
    cust = next(t for t in schema["tables"] if t["name"] == "customers")
    assert "email" not in [c["name"] for c in cust["columns"]] and "email" in cust["restricted_columns"]


@pytest.mark.api
def test_query_validation_errors(client):
    assert client.post("/v1/query", json={"question": ""}).status_code == 422
    assert client.post("/v1/query", json={"question": "x", "session_id": "bad id!"}).status_code == 422
    assert client.get("/v1/history", params={"session_id": "../etc"}).status_code == 422


@pytest.mark.api
def test_feedback_requires_matching_session(client):
    s = sid()
    qid = client.post("/v1/query", json={"question": "How many customers do we have?", "session_id": s}).json()["query_id"]
    assert client.post("/v1/feedback", json={"query_id": qid, "session_id": "someone_else", "verdict": "correct"}).status_code == 404
    ok = client.post("/v1/feedback", json={"query_id": qid, "session_id": s, "verdict": "incorrect", "comment": "x"})
    assert ok.status_code == 201 and ok.json()["review_status"] == "pending_review"


@pytest.mark.api
def test_optional_bearer_token(client, monkeypatch):
    from pydantic import SecretStr

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "api_token", SecretStr("s3cret"))
    assert client.get("/v1/schema").status_code == 401
    assert client.get("/v1/schema", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/healthz").status_code == 200


@pytest.mark.e2e
def test_end_to_end_session_flow(client):
    s = sid()
    post = lambda **kw: client.post("/v1/query", json={"session_id": s, **kw}).json()  # noqa: E731
    clar = post(question="What was our revenue in 2025?")
    assert clar["decision"] == "clarify"
    label = clar["clarification"]["interpretations"][0]["label"]
    ans = post(question=f"What was our revenue in 2025? ({label})")
    assert ans["decision"] in {"answer", "answer_with_warnings"} and ans["rows"][0][0] > 0
    assert ans["confidence"]["signals"] and "NOT a calibrated" in ans["confidence"]["note"]
    edited = post(question="What was our revenue in 2025?", sql=ans["sql"].replace("2026-01-01", "2025-07-01"))
    assert edited["source"] == "user_edited" and edited["decision"] != "blocked"
    blocked = post(question="x", sql="SELECT 1; DROP TABLE shop.orders")
    assert blocked["decision"] == "blocked" and blocked["guardrail"]["violations"][0]["rule"] == "multiple_statements"
    flagged = post(question="How many orders were placed in 2025?")
    assert flagged["decision"] == "answer_with_warnings" and any("2025" in w for w in flagged["warnings"])
    hist = client.get("/v1/history", params={"session_id": s}).json()["items"]
    assert [h["decision"] for h in hist][:2] == ["answer_with_warnings", "blocked"] and len(hist) == 5
    assert client.get("/v1/history", params={"session_id": sid()}).json()["items"] == []
