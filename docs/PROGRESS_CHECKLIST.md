# Progress checklist (mapped to *Project_08_Text_to_SQL_Interface_with_Guardrails_and_Hallucination_Detection.docx*)

Legend: **Implemented** = code exists · **Tested** = automated tests pass · **Verified** = exercised against a running stack · **Pending** = not done, with reason.

| Phase | Done | Status |
|---|---|---|
| Phase 1: Schema-Aware Prompt Engine | ☑ | Implemented, tested, verified |
| Phase 2: SQL Generation and Safety Layer | ☑ | Implemented, tested, verified (live LLM calls pending: no API key) |
| Phase 3: Hallucination Detection System | ☑ | Implemented, tested, verified with mocked provider; LLM back-translation judge pending live run |
| Phase 4: Query Interface | ☑ | Implemented, tested, verified (API + Streamlit, screenshots) |
| Phase 5: Evaluation Suite | ◐ | Golden set, runner, mocked results and native verification done; **Docker build/up and live-model eval pending** |
| Phase 6: Polish for Portfolio | ◐ | README, docs, demo script and screenshots done; **demo recording pending** (requires a human to record) |

## Step-level detail

### Phase 1
| Spec step | Where | Status |
|---|---|---|
| 1.1 Auto-extract schema (tables, typed columns, PK/FK, categorical sample values) | `app/schema/introspect.py`; approved values in `config/schema_metadata.yaml` (sensitive columns never sampled) | Tested, verified |
| 1.2 Dynamic prompt: relevant schema, FK relationships, sample values, descriptions/glossary, 3-5 few-shots | `app/prompting/builder.py`, `config/fewshots.yaml` (5 examples; top 3 by similarity used) | Tested |
| 1.3 Schema filtering by embedding similarity | `app/schema/retrieval.py` (local hashed embeddings + threshold + FK bridges; `Embedder` protocol for hosted models) | Tested |
| 1.4 Structured clarification with interpretations and example queries | `screen_question` + model `clarification` status; UI buttons | Tested, verified |

### Phase 2
| Spec step | Where | Status |
|---|---|---|
| 2.1 Structured output (SQL, explanation, confidence, tables/columns); syntax validation before execution | `app/llm/*` (tool/function calling + Pydantic); parsing with **sqlglot** (dialect-aware AST) rather than `sqlparse`, which only tokenizes | Adapters tested with stubbed SDKs; **live calls pending** |
| 2.2 Guardrail middleware: block DDL/DML, LIMIT 1000, depth > 3, EXPLAIN row ceiling, configurable, logged | `app/guardrails/policy.py`, `plan.py`, `app_meta.guardrail_events` | Tested, verified |
| 2.3 Read-only transaction with rollback + SELECT-only DB user | `executor.py`, `db/init/03_roles.sh` | Tested (role tested independently of policy) |
| 2.4 Execution layer: results, time, row count, EXPLAIN plan, audit | `executor.py`, `QueryResponse.plan`, `app_meta.query_log` | Tested, verified |

### Phase 3
| Spec step | Where | Status |
|---|---|---|
| 3.1 SQL-to-question back-translation + alignment score | LLM back-translation + judge (live) and deterministic describer + concept alignment (`app/validation/alignment.py`) | Deterministic path tested; **LLM path pending live run** |
| 3.2 Result sanity checks (ranges, counts, date spans, NULL-heavy joins) | `app/validation/checks.py` | Tested |
| 3.3 Multi-query validation (two independent approaches, compare, show both) | `Pipeline._alternative`, `app/validation/compare.py`; UI shows both results on disagreement | Tested, verified |
| 3.4 Combined confidence (syntax, back-translation, sanity, agreement, coverage) | `app/confidence.py`, `docs/CONFIDENCE.md` | Tested |

### Phase 4
| Spec step | Where | Status |
|---|---|---|
| 4.1 `POST /v1/query`, `GET /v1/schema`, `GET /v1/history` (+ feedback, health, readiness) | `app/api/main.py` | Tested, verified |
| 4.2 Streamlit UI: input, highlighted editable SQL, sortable results, confidence breakdown, history | `frontend/app.py`; `docs/screenshots/` | Verified (screenshots) |
| 4.3 Feedback loop | `POST /v1/feedback`, `scripts/review_feedback.py`. **Deviation by design:** feedback is never auto-promoted to few-shots or eval cases; a human reviews it first (per the build prompt) | Tested |

### Phase 5
| Spec step | Where | Status |
|---|---|---|
| 5.1 50+ golden questions with verified SQL and results, incl. ambiguous and unanswerable | `eval/golden.yaml` (85 cases), `eval/expected_results.json`, `tests/test_golden_crosscheck.py` | Done |
| 5.2 Automated evals: exact match, execution match, hallucination detection, guardrail effectiveness | `eval/run_eval.py` → `eval/results/mock_results.json` | **Mocked run done**; live run pending |
| 5.3 Docker Compose (Postgres + API + frontend) and README leading with eval numbers | `docker-compose.yml`, `docker/*.Dockerfile`, `README.md` | `config` validated; **build/up pending** (registry blocked in build environment) |

### Phase 6
| Spec step | Where | Status |
|---|---|---|
| 6.1 Demo under 4 minutes | `docs/DEMO_SCRIPT.md` | Script done; **recording pending** |
| 6.2 Narrative leading with safety | README headline table | Done; final numbers should be refreshed after the live evaluation |
