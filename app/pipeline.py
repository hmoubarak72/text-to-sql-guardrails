"""End-to-end orchestration: screen -> retrieve -> generate -> guard -> plan -> validate -> execute -> score -> decide."""
from __future__ import annotations

import logging
import time
import uuid

from app.confidence import score_confidence
from app.config import Settings
from app.execution.executor import SafeExecutor
from app.guardrails.plan import PlanInspector
from app.guardrails.policy import SQLPolicy
from app.llm.base import LLMError, LLMProvider
from app.models import (AlternativeComparison, BackTranslation, Check, GenerationOutput, PolicyResult, QueryRequest,
                        QueryResponse, SchemaModel, Violation)
from app.prompting.builder import (ALTERNATIVE_INSTRUCTION, build_alignment_prompt, build_backtranslation_prompt,
                                   build_generation_prompt, screen_question, select_fewshots)
from app.schema.retrieval import RetrievalResult, SchemaRetriever
from app.validation.alignment import concept_alignment, describe_sql
from app.validation.checks import post_execution_checks, pre_execution_checks
from app.validation.compare import compare_results
from app.validation.sqlfacts import extract_facts

log = logging.getLogger("t2s.pipeline")
REPAIRABLE_RULES = {"unknown_column", "unknown_table", "column_resolution", "plan_error"}


class Pipeline:
    def __init__(self, *, settings: Settings, schema: SchemaModel, metadata: dict, fewshots: list[dict],
                 provider: LLMProvider, plan_inspector: PlanInspector, executor: SafeExecutor, store) -> None:
        self.s = settings
        self.schema = schema
        self.metadata = metadata
        self.fewshots = fewshots
        self.provider = provider
        self.retriever = SchemaRetriever(schema)
        self.policy = SQLPolicy(schema, max_rows=settings.max_result_rows, max_depth=settings.max_subquery_depth,
                                max_joins=settings.max_joins, max_length=settings.max_sql_length)
        self.plan = plan_inspector
        self.executor = executor
        self.store = store

    # ------------------------------------------------------------------------------------------ main
    def run(self, req: QueryRequest) -> QueryResponse:
        t0 = time.perf_counter()
        resp = QueryResponse(query_id=str(uuid.uuid4()), session_id=req.session_id, question=req.question.strip(),
                             decision="error", decision_reason="", source="user_edited" if req.sql else "generated",
                             provider=self.provider.name)
        try:
            self._run(req, resp)
        except LLMError as exc:
            resp.decision, resp.decision_reason = "error", f"Model call failed: {exc}"
        except Exception as exc:  # fail closed: never return results from a half-validated path
            log.exception("pipeline failure")
            resp.decision, resp.decision_reason = "error", f"Internal error ({type(exc).__name__}); nothing was executed."
            resp.rows, resp.columns, resp.row_count = [], [], 0
        resp.total_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        try:
            self.store.log_query(resp)
        except Exception:
            log.exception("failed to persist query log")
        return resp

    def _run(self, req: QueryRequest, resp: QueryResponse) -> None:
        question = resp.question
        retrieval: RetrievalResult = self.retriever.retrieve(question)
        gen: GenerationOutput | None = None

        if req.sql:
            sql = req.sql
        else:
            screen = screen_question(question, self.metadata)
            if screen.kind == "unsupported":
                resp.decision, resp.unsupported_reason = "unsupported", screen.reason
                resp.decision_reason = f"Question refers to '{screen.term}', which this database cannot answer."
                return
            if screen.kind == "clarify":
                resp.decision = "clarify"
                resp.decision_reason = screen.reason
                resp.clarification = {"question": f"What do you mean by '{screen.term}'?",
                                      "interpretations": [i.model_dump() for i in screen.interpretations or []]}
                return
            system, user = build_generation_prompt(question, self.schema, retrieval,
                                                   select_fewshots(question, self.fewshots))
            gen = self.provider.generate(system, user, question=question)
            if gen.status == "clarification":
                resp.decision, resp.decision_reason = "clarify", "The model found the question ambiguous."
                resp.clarification = {"question": gen.clarification_question or "Please clarify the question.",
                                      "interpretations": [i.model_dump() for i in gen.interpretations]}
                return
            if gen.status == "unsupported" or not gen.sql:
                resp.decision = "unsupported"
                resp.unsupported_reason = gen.unsupported_reason or "The model could not answer from this database."
                resp.decision_reason = "The model reported that the database cannot answer this question."
                return
            sql = gen.sql
            resp.explanation, resp.assumptions = gen.explanation, gen.assumptions

        resp.original_sql = sql
        policy = self.policy.check(sql)
        plan = self.plan.inspect(policy.sql) if policy.allowed else None

        # Bounded repair: only for schema-reference / planning errors on generated SQL, always re-validated.
        while (not req.sql and resp.repair_attempts < self.s.max_repair_attempts
               and self._repairable(policy, plan)):
            resp.repair_attempts += 1
            errors = [v.message for v in policy.violations] + [v.message for v in (plan.violations if plan else [])]
            system, user = build_generation_prompt(
                question, self.schema, retrieval, select_fewshots(question, self.fewshots),
                extra_instruction=("Your previous SQL was rejected by the validator:\n- " + "\n- ".join(errors) +
                                   f"\nPREVIOUS SQL:\n{sql}\nReturn corrected SQL using only listed columns."))
            gen = self.provider.repair(system, user, question=question, attempt=resp.repair_attempts)
            if gen.status != "sql" or not gen.sql:
                break
            sql = gen.sql
            resp.explanation, resp.assumptions = gen.explanation, gen.assumptions
            policy = self.policy.check(sql)
            plan = self.plan.inspect(policy.sql) if policy.allowed else None

        resp.guardrail, resp.plan = policy, plan
        resp.warnings += policy.warnings
        resp.notices += policy.notices
        if not policy.allowed or (plan is not None and not plan.allowed):
            violations = policy.violations + (plan.violations if plan else [])
            self.store.log_guardrail_events(resp.query_id, resp.session_id, sql, violations)
            resp.decision = "blocked"
            resp.sql = None
            resp.decision_reason = "Blocked before execution: " + "; ".join(f"[{v.rule}] {v.message}" for v in violations)
            if resp.repair_attempts:
                resp.decision_reason += f" (after {resp.repair_attempts} repair attempt(s))"
            return
        resp.sql = policy.sql

        # ---------------- pre-execution validation
        facts = extract_facts(policy.sql, self.schema)
        checks: list[Check] = pre_execution_checks(facts, self.schema, retrieval, gen)
        concept_score, concept_diffs, concept_kinds = concept_alignment(question, facts, self.schema)
        bt = self._back_translate(question, policy.sql, facts, retrieval)
        resp.back_translation = bt

        # ---------------- execution (read-only, restricted role)
        result = self.executor.execute(policy.sql)
        resp.execution_ms = result.duration_ms
        if not result.ok:
            if result.error_code == "42501":
                self.store.log_guardrail_events(resp.query_id, resp.session_id, sql, [Violation(
                    stage="execution", rule="db_permission_denied", message=result.error or "permission denied")])
                resp.decision = "blocked"
                resp.decision_reason = f"Database permissions denied the query: {result.error}"
            else:
                resp.decision = "error"
                resp.decision_reason = f"Execution failed: {result.error}"
            return

        # ---------------- post-execution validation
        checks += post_execution_checks(facts, result, self.schema)
        alt = self._alternative(question, retrieval, policy.sql, facts, result, req)
        resp.alternative = alt
        resp.checks = checks
        conf = score_confidence(repair_attempts=resp.repair_attempts, concept_score=concept_score,
                                concept_diffs=concept_diffs, concept_kinds=concept_kinds, back_translation=bt, checks=checks, alternative=alt,
                                self_reported=gen.confidence if gen else None, executed=True)
        resp.confidence = conf
        resp.columns, resp.truncated = result.columns, result.truncated
        resp.warnings += [f"{c.name}: {c.detail}" for c in checks if c.status in {"warn", "fail"}]
        resp.warnings += [f"concept_alignment: {d}" for d in concept_diffs]
        resp.warnings += conf.warnings
        if alt and alt.match is False:
            resp.warnings.append("Alternative query disagreed: " + alt.detail)

        score = conf.score or 0.0
        has_fail = any(c.status == "fail" for c in checks)
        if score < self.s.confidence_abstain_threshold or has_fail:
            resp.decision = "abstain"
            resp.decision_reason = (f"Confidence {score:.2f} is below the abstain threshold "
                                    f"{self.s.confidence_abstain_threshold:.2f} or a validation check failed; "
                                    "results are withheld. Review the SQL and warnings, edit the SQL, or rephrase.")
            resp.row_count = result.row_count
            return
        resp.rows, resp.row_count = result.rows, result.row_count
        if score < self.s.confidence_answer_threshold or resp.warnings:
            resp.decision = "answer_with_warnings"
            resp.decision_reason = f"Confidence {score:.2f}; review the warnings before relying on this result."
        else:
            resp.decision = "answer"
            resp.decision_reason = f"Confidence {score:.2f}; all available checks passed."

    # ------------------------------------------------------------------------------------- helpers
    @staticmethod
    def _repairable(policy: PolicyResult, plan) -> bool:
        if not policy.allowed:
            return bool(policy.violations) and all(v.rule in REPAIRABLE_RULES for v in policy.violations)
        return plan is not None and not plan.allowed and all(v.rule in REPAIRABLE_RULES for v in plan.violations)

    def _back_translate(self, question, sql, facts, retrieval) -> BackTranslation:
        described = describe_sql(facts)
        if not self.s.enable_back_translation or not self.provider.is_live:
            return BackTranslation(question=described, alignment_score=None,
                                   differences=["LLM back-translation unavailable; deterministic description shown."])
        try:
            system, user = build_backtranslation_prompt(sql, self.schema, retrieval.tables)
            back_q = self.provider.back_translate(system, user, question=question, sql=sql) or described
            system, user = build_alignment_prompt(question, back_q, sql)
            judged = self.provider.judge_alignment(system, user, question=question)
            if judged is None:
                return BackTranslation(question=back_q)
            return BackTranslation(question=back_q, alignment_score=judged.alignment_score,
                                   differences=judged.differences)
        except LLMError as exc:
            return BackTranslation(question=described, differences=[f"LLM back-translation failed: {exc}"])

    def _alternative(self, question, retrieval, sql, facts, result, req) -> AlternativeComparison:
        complex_q = facts.joins > 0 or bool(facts.aggregates) or facts.subqueries > 0
        if not self.s.enable_alternative_query:
            return AlternativeComparison(attempted=False, reason="disabled by configuration")
        if not complex_q:
            return AlternativeComparison(attempted=False, reason="simple single-table lookup; not needed")
        try:
            system, user = build_generation_prompt(question, self.schema, retrieval,
                                                   select_fewshots(question, self.fewshots),
                                                   extra_instruction=ALTERNATIVE_INSTRUCTION.format(sql=sql))
            alt_gen = self.provider.alternative(system, user, question=question)
        except LLMError as exc:
            return AlternativeComparison(attempted=False, reason=f"alternative generation failed: {exc}")
        if alt_gen.status != "sql" or not alt_gen.sql:
            return AlternativeComparison(attempted=False, reason="no independent alternative was produced")
        alt_policy = self.policy.check(alt_gen.sql)       # same guardrails as the primary query
        if not alt_policy.allowed:
            return AlternativeComparison(attempted=True, reason="alternative blocked by guardrails",
                                         alt_sql=alt_gen.sql, alt_blocked=True, match=None,
                                         detail="; ".join(v.message for v in alt_policy.violations))
        alt_plan = self.plan.inspect(alt_policy.sql)
        if not alt_plan.allowed:
            return AlternativeComparison(attempted=True, reason="alternative blocked by plan limits",
                                         alt_sql=alt_policy.sql, alt_blocked=True, match=None,
                                         detail="; ".join(v.message for v in alt_plan.violations))
        alt_res = self.executor.execute(alt_policy.sql)
        if not alt_res.ok:
            return AlternativeComparison(attempted=True, reason="alternative failed to execute", alt_sql=alt_policy.sql,
                                         match=None, detail=alt_res.error or "")
        alt_facts = extract_facts(alt_policy.sql, self.schema)
        ordered = bool(facts.order_by and alt_facts.order_by and facts.limit and facts.limit < 1000)
        match, detail = compare_results(result.columns, result.rows, alt_res.columns, alt_res.rows, ordered=ordered,
                                        rel_tol=self.s.numeric_rel_tolerance, abs_tol=self.s.numeric_abs_tolerance)
        return AlternativeComparison(attempted=True, reason="complex query: independent alternative executed",
                                     alt_sql=alt_policy.sql, match=match, detail=detail,
                                     alt_columns=alt_res.columns, alt_rows=alt_res.rows[:50])
