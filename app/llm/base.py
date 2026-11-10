"""Provider-agnostic LLM interface with schema-constrained (structured) outputs."""
from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from app.models import BackTranslation, GenerationOutput

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class _BackTranslationText(BaseModel):
    question: str


def inline_schema(model: type[BaseModel]) -> dict:
    """Return the model's JSON schema with $defs references inlined (portable across providers)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


class LLMProvider(ABC):
    name: str = "base"
    is_live: bool = True

    @abstractmethod
    def structured(self, system: str, user: str, tool_name: str, model: type[T]) -> T:
        """Call the model and return an instance of `model`, validated."""

    # Each method receives the original question so the mock provider can look up scripted fixtures.
    def generate(self, system: str, user: str, *, question: str) -> GenerationOutput:
        return self.structured(system, user, "submit_sql", GenerationOutput)

    def repair(self, system: str, user: str, *, question: str, attempt: int) -> GenerationOutput:
        return self.structured(system, user, "submit_sql", GenerationOutput)

    def alternative(self, system: str, user: str, *, question: str) -> GenerationOutput:
        return self.structured(system, user, "submit_sql", GenerationOutput)

    def back_translate(self, system: str, user: str, *, question: str, sql: str) -> str | None:
        return self.structured(system, user, "submit_question", _BackTranslationText).question

    def judge_alignment(self, system: str, user: str, *, question: str) -> BackTranslation | None:
        return self.structured(system, user, "submit_alignment", BackTranslation)
