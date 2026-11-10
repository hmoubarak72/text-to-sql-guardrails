"""FastAPI service."""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import ping
from app.models import FeedbackRequest, QueryRequest, QueryResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("t2s.api")
STATE: dict = {}


def _init_pipeline():
    from app.bootstrap import build_pipeline

    STATE["pipeline"] = build_pipeline(get_settings())
    log.info("pipeline ready: provider=%s tables=%d", STATE["pipeline"].provider.name,
             len(STATE["pipeline"].schema.tables))


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _init_pipeline()
    except Exception as exc:  # stay up so /healthz works; /readyz reports not ready and retries lazily
        log.error("pipeline init failed (%s); will retry on readiness checks", type(exc).__name__)
    yield


app = FastAPI(title="Text-to-SQL with Guardrails", version="1.0.0", lifespan=lifespan)
_settings = get_settings()
if _settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in _settings.cors_origins.split(",")],
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = get_settings().api_token
    if token is None or not token.get_secret_value():
        return
    expected = f"Bearer {token.get_secret_value()}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def pipeline():
    if "pipeline" not in STATE:
        try:
            _init_pipeline()
        except Exception:
            raise HTTPException(status_code=503, detail="service not ready (database unavailable)")
    return STATE["pipeline"]


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    pipe = STATE.get("pipeline")
    if pipe is None:
        try:
            _init_pipeline()
            pipe = STATE["pipeline"]
        except Exception:
            return JSONResponse(status_code=503, content={"ready": False, "reason": "pipeline not initialized"})
    reader_ok = ping(pipe.executor.engine)
    app_ok = ping(pipe.store.engine) if hasattr(pipe.store, "engine") else True
    ok = reader_ok and app_ok
    return JSONResponse(status_code=200 if ok else 503,
                        content={"ready": ok, "reader_db": reader_ok, "app_db": app_ok, "provider": pipe.provider.name,
                                 "provider_is_live": pipe.provider.is_live})


@app.post("/v1/query", response_model=QueryResponse, dependencies=[Depends(require_token)])
async def query(req: QueryRequest):
    return await run_in_threadpool(pipeline().run, req)


@app.get("/v1/schema", dependencies=[Depends(require_token)])
def schema():
    pipe = pipeline()
    s = pipe.schema
    return {
        "schema": s.schema_name, "data_start": s.data_start, "data_end": s.data_end,
        "tables": [{"name": t.name, "description": t.description, "row_estimate": t.row_estimate,
                    "primary_key": t.primary_key,
                    "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable, "description": c.description,
                                 "categorical_values": c.categorical_values} for c in t.allowed_columns()],
                    "restricted_columns": [c.name for c in t.columns if c.sensitive],
                    "foreign_keys": [fk.model_dump() for fk in t.foreign_keys]} for t in s.tables.values()],
        "glossary": [g.model_dump() for g in s.glossary],
    }


@app.get("/v1/history", dependencies=[Depends(require_token)])
def history(session_id: str = Query(..., pattern=r"^[A-Za-z0-9_\-]+$", max_length=128),
            limit: int = Query(50, ge=1, le=200)):
    return {"session_id": session_id, "items": pipeline().store.history(session_id, limit)}


@app.post("/v1/feedback", dependencies=[Depends(require_token)], status_code=201)
def feedback(fb: FeedbackRequest):
    fid = pipeline().store.add_feedback(fb)
    if fid is None:
        raise HTTPException(status_code=404, detail="query not found for this session")
    return {"feedback_id": fid, "review_status": "pending_review",
            "note": "Feedback is stored for human review; it is never auto-promoted into few-shot examples."}
