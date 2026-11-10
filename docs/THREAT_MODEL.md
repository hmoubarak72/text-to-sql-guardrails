# Threat model

## Assets
1. Integrity and availability of business data (`shop` schema).
2. Confidentiality of restricted columns (`customers.email`, `customers.phone`, `employees.salary`), of other users' query history (`app_meta`), and of database or server internals (catalogs, files, settings).
3. Correctness of answers shown to users (a confidently wrong number is a harm in its own right).
4. Secrets: database passwords and LLM API keys.

## Trust boundaries
| Source | Trust |
|---|---|
| User question, user-edited SQL | Untrusted |
| LLM output (SQL, explanations, back-translations, alternatives) | Untrusted: may be wrong, hallucinated, or steered by prompt injection |
| Database content (values returned, comments) | Untrusted data; never interpreted as instructions |
| `config/*.yaml`, `db/init/*`, code | Trusted, human-reviewed |
| Feedback | Untrusted until a human reviews it |

## Threats and controls

| # | Threat | Controls | Verified by |
|---|---|---|---|
| T1 | Destructive SQL (DML/DDL/TRUNCATE), from a malicious user, a prompt-injected model or an edited query | AST policy rejects any non-SELECT node anywhere in the tree; read-only role default; explicit `READ ONLY` transaction; always rollback | `test_unit_policy::test_blocked`, `test_integration::test_database_role_is_second_line_of_defense`, eval X01-X04, X14, X18 |
| T2 | Statement stacking (`SELECT ...; DROP ...`) | Parser counts statements; exactly one allowed | X03, unit tests |
| T3 | Write-capable CTE (`WITH x AS (DELETE ... RETURNING *)`) | Tree-wide forbidden-node walk; every CTE must be SELECT | X14, unit tests |
| T4 | Data exfiltration of restricted columns | Columns resolved only against permitted set; `SELECT *` expanded to permitted columns; column-level GRANTs exclude sensitive columns; schema endpoint and prompts omit them | X06, X07, integration (42501) |
| T5 | System catalog / other schema / file access (`pg_shadow`, `information_schema`, `app_meta`, `pg_read_file`, `COPY TO`) | Table allowlist (schema `shop` only); function allowlist; `COPY` rejected; role has no `app_meta` or superuser privileges | X05, X08, X10, X17 |
| T6 | Session or privilege manipulation (`SET ROLE`, `set_config`, `current_setting`) | Unparsed commands and `SET` rejected; functions not on the allowlist rejected | X13, X19 |
| T7 | Resource exhaustion (cross joins, `pg_sleep`, huge `generate_series`, recursive CTEs, deep nesting, large results) | EXPLAIN cost and rows-scanned ceilings; function allowlist; no table functions; no recursive CTEs; nesting/join/length limits; LIMIT enforcement; statement timeout (role and transaction); `temp_file_limit`, `work_mem`; role connection limit | X09, X15, X16, X20, timeout integration test |
| T8 | Row locking or lock contention (`FOR UPDATE`) | Lock nodes rejected; read-only transaction | X12 |
| T9 | Prompt injection in the question ("ignore previous instructions...") | Question wrapped as untrusted data in the prompt; model output is never trusted; guardrails are independent of the model | X05 |
| T10 | Hallucinated or wrong SQL that is "safe" but answers the wrong question | Static checks, concept alignment, back-translation and judge (live), result checks, alternative-query comparison, confidence caps, abstain policy | H01-H10, R01-R03 |
| T11 | Invalid schema references (hallucinated tables/columns) | Rejected by column resolution; one bounded, fully re-validated repair; otherwise blocked | R01-R03 |
| T12 | Leakage via logs | Blocked SQL logged with string literals redacted; result previews capped and configurable (0 disables); restricted columns cannot appear in results | `test_unit_policy::test_redact_literals` |
| T13 | Feedback poisoning of few-shots | Feedback stored as `pending_review`; never auto-promoted; review script exports for humans | API test, design |
| T14 | Reading another session's history or attaching feedback to it | Feedback requires matching session id; history filtered by session id | API test (404 on mismatched session) |
| T15 | Secret exposure | `.env` git-ignored; `.env.example` placeholders only; `SecretStr` settings; API never holds superuser credentials; ports bound to localhost | Review |
| T16 | Overconfident presentation of results | Confidence labeled heuristic and uncalibrated; unavailable signals listed; abstain withholds rows | Unit and e2e tests |

## Residual risks
- **Semantically wrong but plausible SQL** can pass every check (e.g. eval case H08). Detection is heuristic, not proof.
- **Session ids are not authentication.** Anyone who knows a session id can read its history. Add user authentication (and bind sessions to users) before multi-user use.
- **Planner estimates can be wrong.** EXPLAIN-based limits are estimates; statement timeout is the hard stop. A query below the cost ceiling can still use CPU until the timeout.
- **Parser differential.** sqlglot and PostgreSQL could disagree on exotic syntax. Mitigated by allowlists, rejection of unparsed commands, executing exactly the normalized SQL, and the database-role layer, which does not depend on the parser.
- **Inference via aggregates.** Restricted columns cannot be referenced, but aggregates over permitted columns can still reveal information about small groups (e.g. a city with one customer). Add k-anonymity thresholds if this matters.
- **LLM provider data handling.** Schema metadata and questions are sent to the configured provider; sample values are restricted to the approved list, and no result rows are sent.
- **Denial of wallet.** Each live question can trigger up to 5 model calls (generate, repair, back-translate, judge, alternative). Add rate limiting before public exposure.
