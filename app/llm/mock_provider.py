"""Deterministic mock provider for tests, offline evaluation and demos without an API key.

It REPLAYS scripted responses keyed by question (from eval/golden.yaml). Some scripted responses are
intentionally wrong to exercise hallucination detection. Results obtained with this provider measure the
pipeline (guardrails, validators, confidence, decision policy) - NOT the SQL-generation ability of any model.

Back-translation and LLM alignment judging are deliberately NOT scripted: the mock returns None, so the
pipeline falls back to its deterministic SQL describer and concept-alignment checker.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.llm.base import LLMProvider, T
from app.models import BackTranslation, GenerationOutput


def normalize_question(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower()).rstrip("?.! ")


class MockProvider(LLMProvider):
    name = "mock"
    is_live = False

    def __init__(self, fixtures_path: Path | None = None, fixtures: list[dict] | None = None) -> None:
        cases = fixtures if fixtures is not None else self._load(fixtures_path)
        self._by_q: dict[str, dict] = {}
        for case in cases:
            mock = dict(case.get("mock") or {})
            if "generation" not in mock and case.get("gold_sql"):
                mock["generation"] = {"status": "sql", "sql": case["gold_sql"],
                                      "explanation": "Scripted mock response (gold SQL).", "confidence": 0.9}
            self._by_q[normalize_question(case["question"])] = mock
        self.calls: list[str] = []

    @staticmethod
    def _load(path: Path | None) -> list[dict]:
        if not path or not Path(path).exists():
            return []
        with open(path, encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("cases", [])

    def _fixture(self, question: str) -> dict:
        return self._by_q.get(normalize_question(question), {})

    def structured(self, system: str, user: str, tool_name: str, model: type[T]) -> T:  # pragma: no cover
        raise NotImplementedError("MockProvider answers only through its scripted methods")

    def generate(self, system: str, user: str, *, question: str) -> GenerationOutput:
        self.calls.append("generate")
        gen = self._fixture(question).get("generation")
        if not gen:
            return GenerationOutput(status="unsupported",
                                    unsupported_reason="Mock provider has no scripted response for this question. "
                                                       "Configure LLM_PROVIDER=anthropic or openai for free-form questions.")
        return GenerationOutput.model_validate(gen)

    def repair(self, system: str, user: str, *, question: str, attempt: int) -> GenerationOutput:
        self.calls.append("repair")
        rep = self._fixture(question).get("repair")
        if not rep:
            return GenerationOutput(status="unsupported", unsupported_reason="No scripted repair available.")
        return GenerationOutput.model_validate(rep)

    def alternative(self, system: str, user: str, *, question: str) -> GenerationOutput:
        self.calls.append("alternative")
        alt = self._fixture(question).get("alternative")
        if not alt:
            return GenerationOutput(status="unsupported", unsupported_reason="No scripted alternative available.")
        alt = {"status": "sql", **alt}
        return GenerationOutput.model_validate(alt)

    def back_translate(self, system: str, user: str, *, question: str, sql: str) -> str | None:
        return None

    def judge_alignment(self, system: str, user: str, *, question: str) -> BackTranslation | None:
        return None
