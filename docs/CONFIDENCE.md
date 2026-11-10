# Confidence scoring and decision policy

## Not a probability
The score is a **heuristic weighted average of validation signals**. It has **not** been calibrated against observed correctness, so `0.8` does not mean "80% likely to be correct". Calibrating it requires live-model evaluation results (e.g. isotonic regression of score vs. execution match on a held-out set). Agreement between the primary and alternative queries is supporting evidence, not proof: two generations from the same model can share the same misunderstanding.

## Signals

| Signal | Weight | Phase | How it is computed | Unavailable when |
|---|---|---|---|---|
| `policy_and_syntax` | 0.10 | pre | 1.0 once the SQL parses and passes every guardrail (queries that fail never reach scoring) | never |
| `schema_validation` | 0.10 | pre | 1.0 if all tables/columns resolved first time; 0.6 if a bounded repair was needed | never |
| `llm_back_translation` | 0.20 | pre | The LLM restates what the SQL answers; an LLM judge scores alignment 0..1 with listed differences | mock provider, or the call failed |
| `concept_alignment` | 0.20 | pre | Deterministic: the question's metric, years, months, categorical values, grouping, top-N, sort direction and entities are checked against the SQL AST; penalties per mismatch | never |
| `static_checks` | 0.10 | pre | Mean of: FK-conformant joins, aggregation fan-out, date range vs. data span, schema coverage | never |
| `result_sanity` | 0.15 | post | Mean of: non-empty, NULL profile, value plausibility, output dates, duplicate rows, truncation | query not executed |
| `multi_query_agreement` | 0.15 | post | 1.0 if an independently generated query (same guardrails) returns the same result, 0.0 if different | simple single-table lookups, no alternative produced, alternative blocked or failed |
| `model_self_report` | 0.00 | — | The model's own confidence; displayed but carries no weight (uncalibrated, known to be overconfident) | not reported |

`score = Σ(weight × score) / Σ(weight)` over **available** signals with weight > 0. Unavailable signals are listed in `confidence.unavailable` and are never counted as passes.

## Caps (applied after the weighted average)

| Condition | Cap |
|---|---|
| Any check with status `fail` (e.g. join not on a declared foreign key) | 0.35 |
| Alternative query disagrees | 0.55 |
| Critical concept mismatch (year, month, metric, filter value, sort direction) or concept score < 0.5 | 0.55 |
| LLM judge alignment < 0.5 (live only) | 0.55 |
| Any other concept mismatch, or any pre-execution warning | 0.65 |

Labels: `high` ≥ 0.80, `medium` ≥ 0.60, `low` < 0.60.

## Decisions

| Stage | Condition | Decision | User sees |
|---|---|---|---|
| Before generation | Question matches an unsupported concept | `unsupported` | Reason |
| Before generation | Ambiguous term without a qualifier | `clarify` | Interpretations to choose from |
| Generation | Model returns clarification / unsupported | `clarify` / `unsupported` | Model's interpretations / reason |
| Pre-execution | Policy or plan violation (after at most one repair for schema errors) | `blocked` | Rules violated; SQL is never executed |
| Execution | Database rejects (permissions) | `blocked` | Error |
| Execution | Other error (timeout, runtime) | `error` | Safe diagnostic |
| Post-execution | Score < 0.40 **or** any `fail` check | `abstain` | SQL, warnings and reasons; **rows withheld** |
| Post-execution | Score < 0.70 **or** any warning | `answer_with_warnings` | Rows + warnings + breakdown |
| Post-execution | Otherwise | `answer` | Rows + breakdown |

Pre-execution checks run before the query touches data. Post-execution checks only inspect the result set. The alternative query also passes every guardrail and the EXPLAIN preflight before it runs.
