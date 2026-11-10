# Verification record

Build environment: Ubuntu 24.04 cloud workspace, Python 3.13.18 (venv), PostgreSQL 16.15, Docker 29.9.2 / Compose v5.7.0. Date: 2026-11-10 (UTC). Provider: **mock** for every run below. No live-model call was made.

## What ran

| Step | Command | Result | Evidence |
|---|---|---|---|
| Compose config | `docker compose --env-file <placeholder env> config --quiet` | Valid; services `db`, `api`, `frontend` | `verification/docker_compose_attempt.log` |
| Compose build | `docker compose build` | **Failed: environment limitation.** Egress proxy returns `Forbidden` for `registry-1.docker.io` (also tried mirror.gcr.io, public.ecr.aws, ghcr.io, quay.io) | same log |
| Compose up / ps | `docker compose up -d`, `docker compose ps` | **Not possible** (no base images); `ps` empty | same log |
| Compose logs | `docker compose logs --no-color` | **Not run**: no containers existed | — |
| Fresh DB init (entrypoint-equivalent) | `initdb` with non-default superuser `t2s_admin`, then `db/init/01_schema.sql`, `02_seed.sql`, `03_roles.sh` in order, exactly as `docker-entrypoint-initdb.d` would | Success: 1,427 orders, 2,620 items, roles `t2s_app` and `t2s_reader` created | session transcript |
| Tests | `python -m pytest -v` | **109 passed** (88 unit, 21 integration/API/e2e), 0 skipped | `verification/pytest_native.log` |
| Expected results | `python -m eval.run_eval --build-expected` | 52 gold results; 8 independently recomputed in pure Python | `eval/expected_results.json`, `tests/test_golden_crosscheck.py` |
| Mocked evaluation | `python -m eval.run_eval --provider mock` | exit 0; see metrics below | `eval/results/mock_results.json`, `mock_summary.md`, `verification/eval_mock_stdout.json` |
| Live evaluation | `python -m eval.run_eval --provider anthropic` | **Pending**: no API key; api.openai.com blocked | — |
| Stack verification | `uvicorn` + `streamlit` (native), `python scripts/verify_stack.py` | all checks OK | `verification/verify_stack_before_restart.json` |
| Restart persistence | stop API, UI and PostgreSQL (`pg_ctl -m fast stop`), start all again | Healthy; row counts, history, feedback and audit rows unchanged | `verification/restart_persistence.log`, `verification/verify_stack_after_restart.json` |
| UI screenshots | headless Chrome via Playwright against the running UI | 5 screenshots | `screenshots/` |

## Required checks (prompt section 5)

| # | Check | Native stack | Docker |
|---|---|---|---|
| 1 | Services become healthy | ✅ `/healthz`, `/readyz` (both DB roles), Streamlit `/_stcore/health` | ⏳ pending (healthchecks defined in compose and Dockerfiles) |
| 2 | Database initializes | ✅ fresh cluster, init scripts in entrypoint order; schema introspected (9 tables) | ⏳ |
| 3 | API and frontend reachable | ✅ | ⏳ |
| 4 | Valid question returns expected result | ✅ Europe orders = 389 (matches gold and the pure-Python recomputation) | ⏳ |
| 5 | Destructive query blocked | ✅ generated `DELETE` and user-edited `DELETE` blocked; cancelled-order count 135 before and after | ⏳ |
| 6 | Ambiguous question triggers clarification | ✅ "revenue" → gross / net / collected | ⏳ |
| 7 | Invalid schema reference rejected or repaired (bounded, re-validated) | ✅ `shipping_fee` → repaired once to `shipping_cost`; `discount_rate` → still invalid after 1 attempt → blocked | ⏳ |
| 8 | Validation warnings and confidence details | ✅ Laptops fan-out: warnings, 8 signals, unavailable list, alternative disagreement 294 vs 280 | ⏳ |
| 9 | Restart without data loss | ✅ (process restart and PostgreSQL restart on the same data directory) | ⏳ (named volume `pgdata`) |

## Mocked evaluation metrics (85 cases)

| Metric | Value |
|---|---|
| Expected-behavior accuracy | 0.988 (84/85; miss: H08) |
| Unsafe queries blocked | 20/20, 0 executed |
| False-positive blocking | 0/40 |
| Hallucination detection | precision 1.00, recall 0.92 (TP 12, FP 0, FN 1) |
| Clarification / unsupported | 6/6, 6/6 |
| SQL exact match / execution accuracy | 0.976 / 1.00 — **trivial by construction under the mock** |
| Latency p50 / p95 | ~7 ms / ~25 ms (no model latency: mock) |
| Execution failures | 0 |

## To complete on a machine with Docker and registry access
```bash
cp .env.example .env && $EDITOR .env
docker compose config --quiet && docker compose build && docker compose up -d && docker compose ps
docker compose logs --no-color > docs/verification/docker_logs.txt
python scripts/verify_stack.py
docker compose exec api python -m pytest -q
docker compose exec api python -m eval.run_eval --provider mock
docker compose restart api frontend db && python scripts/verify_stack.py
# live model:
docker compose exec -e LLM_PROVIDER=anthropic api python -m eval.run_eval --provider anthropic --min-unsafe-block-rate 1.0
```
Run the live evaluation with an API key set in `.env` (`ANTHROPIC_API_KEY`), then update the README headline with the live numbers.
