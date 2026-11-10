"""Data models shared across pipeline stages and the API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- schema


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    description: str | None = None
    sensitive: bool = False
    categorical_values: list[str] = Field(default_factory=list)


class ForeignKeyInfo(BaseModel):
    column: str
    ref_table: str
    ref_column: str
    unique: bool = False          # FK column is unique -> one-to-one relationship


class TableInfo(BaseModel):
    name: str
    schema_name: str
    description: str | None = None
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    row_estimate: int | None = None

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)

    def allowed_columns(self) -> list[ColumnInfo]:
        return [c for c in self.columns if not c.sensitive]


class GlossaryTerm(BaseModel):
    term: str
    definition: str
    tables: list[str] = Field(default_factory=list)


class SchemaModel(BaseModel):
    schema_name: str
    tables: dict[str, TableInfo]
    glossary: list[GlossaryTerm] = Field(default_factory=list)
    data_start: str | None = None
    data_end: str | None = None

    def fk_edges(self) -> list[tuple[str, str, str, str]]:
        """(table, column, ref_table, ref_column) for every FK."""
        return [(t.name, fk.column, fk.ref_table, fk.ref_column) for t in self.tables.values() for fk in t.foreign_keys]


# --------------------------------------------------------------------------- generation


class Interpretation(BaseModel):
    label: str
    description: str
    example_sql: str | None = None


class GenerationOutput(BaseModel):
    """Structured output requested from the LLM (tool/JSON-schema constrained)."""

    status: Literal["sql", "clarification", "unsupported"]
    sql: str | None = None
    explanation: str = ""
    tables_used: list[str] = Field(default_factory=list)
    columns_used: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: float | None = Field(None, ge=0, le=1, description="Model self-reported confidence (uncalibrated)")
    clarification_question: str | None = None
    interpretations: list[Interpretation] = Field(default_factory=list)
    unsupported_reason: str | None = None


class BackTranslation(BaseModel):
    question: str
    alignment_score: float | None = Field(None, ge=0, le=1)
    differences: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- guardrails / execution


class Violation(BaseModel):
    stage: Literal["parse", "policy", "plan", "execution"]
    rule: str
    message: str


class PolicyResult(BaseModel):
    allowed: bool
    violations: list[Violation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)   # informational (e.g. LIMIT applied); not validation warnings
    sql: str | None = None                     # normalized SQL with enforced LIMIT
    referenced_tables: list[str] = Field(default_factory=list)
    referenced_columns: list[str] = Field(default_factory=list)  # "table.column"
    limit_applied: int | None = None
    limit_was_added: bool = False
    subquery_depth: int = 0
    join_count: int = 0


class PlanResult(BaseModel):
    allowed: bool
    total_cost: float | None = None
    plan_rows: float | None = None
    rows_scanned: float | None = None
    violations: list[Violation] = Field(default_factory=list)
    plan: Any = None


class ExecutionResult(BaseModel):
    ok: bool
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: float = 0.0
    error: str | None = None
    error_code: str | None = None


# --------------------------------------------------------------------------- validation / confidence

CheckStatus = Literal["pass", "warn", "fail", "unavailable"]


class Check(BaseModel):
    name: str
    phase: Literal["pre_execution", "post_execution"]
    status: CheckStatus
    detail: str
    score: float | None = None


class Signal(BaseModel):
    name: str
    weight: float
    score: float | None
    available: bool
    detail: str


class ConfidenceReport(BaseModel):
    score: float | None
    label: Literal["high", "medium", "low", "n/a"]
    signals: list[Signal]
    unavailable: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    note: str = ("Heuristic weighted score over available signals. It is NOT a calibrated probability of "
                 "correctness; agreement between generated queries is supporting evidence, not proof.")


class AlternativeComparison(BaseModel):
    attempted: bool
    reason: str
    alt_sql: str | None = None
    alt_blocked: bool = False
    match: bool | None = None
    detail: str = ""
    alt_columns: list[str] = Field(default_factory=list)
    alt_rows: list[list[Any]] = Field(default_factory=list)


Decision = Literal["answer", "answer_with_warnings", "clarify", "unsupported", "abstain", "blocked", "error"]


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    session_id: str = Field("default", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_\-]+$")
    sql: str | None = Field(None, max_length=8000, description="User-edited SQL; re-enters the full safety pipeline")


class QueryResponse(BaseModel):
    query_id: str
    session_id: str
    question: str
    decision: Decision
    decision_reason: str
    source: Literal["generated", "user_edited"]
    sql: str | None = None
    original_sql: str | None = None
    explanation: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    clarification: dict | None = None
    unsupported_reason: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    execution_ms: float | None = None
    total_latency_ms: float = 0.0
    guardrail: PolicyResult | None = None
    plan: PlanResult | None = None
    checks: list[Check] = Field(default_factory=list)
    back_translation: BackTranslation | None = None
    alternative: AlternativeComparison | None = None
    confidence: ConfidenceReport | None = None
    warnings: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    repair_attempts: int = 0
    provider: str = ""


class FeedbackRequest(BaseModel):
    query_id: str
    session_id: str = Field(..., pattern=r"^[A-Za-z0-9_\-]+$", max_length=128)
    verdict: Literal["correct", "incorrect"]
    comment: str | None = Field(None, max_length=2000)
    corrected_sql: str | None = Field(None, max_length=8000)
