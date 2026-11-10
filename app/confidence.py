"""Explainable confidence scoring and the answer / warn / abstain decision.

score = sum(weight_i * score_i) / sum(weight_i) over AVAILABLE signals only; unavailable signals are listed,
never silently treated as passes. Hard caps then apply:
  * any check with status "fail"            -> score capped at 0.35 (forces abstain)
  * alternative query disagrees              -> capped at 0.55 (answer with warnings at most)
  * critical concept mismatch (year, metric, filter value, sort direction) or concept alignment < 0.5
                                             -> capped at 0.55
  * any other concept mismatch, or any pre-execution check warning -> capped at 0.65
  * LLM judge alignment < 0.5 (live only)    -> capped at 0.55
This is a heuristic, uncalibrated score. It has not been calibrated against observed accuracy.
"""
from __future__ import annotations

from app.models import AlternativeComparison, BackTranslation, Check, ConfidenceReport, Signal

WEIGHTS = {
    "policy_and_syntax": 0.10,
    "schema_validation": 0.10,
    "llm_back_translation": 0.20,
    "concept_alignment": 0.20,
    "static_checks": 0.10,
    "result_sanity": 0.15,
    "multi_query_agreement": 0.15,
    "model_self_report": 0.00,   # displayed for transparency; uncalibrated, so it carries no weight
}


def _mean(checks: list[Check]) -> float | None:
    vals = [c.score for c in checks if c.score is not None and c.status != "unavailable"]
    return round(sum(vals) / len(vals), 3) if vals else None


def score_confidence(*, repair_attempts: int, concept_score: float | None, concept_diffs: list[str],
                     concept_kinds: set[str] | None = None,
                     back_translation: BackTranslation | None, checks: list[Check],
                     alternative: AlternativeComparison | None, self_reported: float | None,
                     executed: bool) -> ConfidenceReport:
    pre = [c for c in checks if c.phase == "pre_execution"]
    post = [c for c in checks if c.phase == "post_execution"]
    sig: list[Signal] = []

    def add(name: str, score: float | None, detail: str, available: bool = True) -> None:
        sig.append(Signal(name=name, weight=WEIGHTS[name], score=score if available else None,
                          available=available and score is not None, detail=detail))

    add("policy_and_syntax", 1.0, "Parsed by a PostgreSQL-dialect parser and passed every guardrail rule.")
    add("schema_validation", 1.0 if repair_attempts == 0 else 0.6,
        "All tables/columns resolved against the permitted schema" +
        ("." if repair_attempts == 0 else f" after {repair_attempts} bounded repair attempt(s)."))
    if back_translation is not None and back_translation.alignment_score is not None:
        add("llm_back_translation", back_translation.alignment_score,
            f"LLM back-translation: \"{back_translation.question}\"" +
            (f" Differences: {'; '.join(back_translation.differences)}" if back_translation.differences else ""))
    else:
        add("llm_back_translation", None, "Unavailable: no live LLM judge (mock provider or judge call failed).",
            available=False)
    add("concept_alignment", concept_score,
        "Deterministic question-vs-SQL concept check" + (": " + "; ".join(concept_diffs) if concept_diffs else
                                                         ": no mismatches found."))
    add("static_checks", _mean(pre), "; ".join(f"{c.name}={c.status}" for c in pre) or "none")
    if executed:
        add("result_sanity", _mean(post), "; ".join(f"{c.name}={c.status}" for c in post) or "none")
    else:
        add("result_sanity", None, "Unavailable: query was not executed.", available=False)
    if alternative is not None and alternative.attempted and alternative.match is not None:
        add("multi_query_agreement", 1.0 if alternative.match else 0.0, alternative.detail)
    else:
        reason = alternative.reason if alternative is not None else "Not attempted."
        add("multi_query_agreement", None, f"Unavailable: {reason}", available=False)
    add("model_self_report", self_reported if self_reported is not None else None,
        "Model's own confidence; shown for transparency, weight 0 (uncalibrated).",
        available=self_reported is not None)

    avail = [s for s in sig if s.available and s.weight > 0]
    total_w = sum(s.weight for s in avail)
    score = round(sum(s.weight * (s.score or 0.0) for s in avail) / total_w, 3) if total_w else None
    warnings = []
    if score is not None:
        if any(c.status == "fail" for c in checks):
            score = min(score, 0.35)
            warnings.append("A validation check failed; confidence capped at 0.35.")
        if alternative is not None and alternative.match is False:
            score = min(score, 0.55)
            warnings.append("Independent alternative query produced different results; confidence capped at 0.55.")
        from app.validation.alignment import CRITICAL_KINDS
        kinds = concept_kinds or set()
        if (concept_score is not None and concept_score < 0.5) or kinds & CRITICAL_KINDS:
            score = min(score, 0.55)
            warnings.append("Question/SQL concept mismatch on a critical element; confidence capped at 0.55.")
        elif kinds or any(c.status == "warn" for c in pre):
            score = min(score, 0.65)
            warnings.append("Validation warnings present; confidence capped at 0.65.")
        if back_translation is not None and back_translation.alignment_score is not None \
                and back_translation.alignment_score < 0.5:
            score = min(score, 0.55)
            warnings.append("LLM judge rates question/SQL alignment low; confidence capped at 0.55.")
    label = "n/a" if score is None else ("high" if score >= 0.8 else "medium" if score >= 0.6 else "low")
    return ConfidenceReport(score=score, label=label, signals=sig,
                            unavailable=[s.name for s in sig if not s.available], warnings=warnings)
