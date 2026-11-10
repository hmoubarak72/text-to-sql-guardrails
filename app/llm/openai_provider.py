"""OpenAI provider: structured output via forced function calling with a JSON schema."""
from __future__ import annotations

import json

from pydantic import ValidationError

from app.llm.base import LLMError, LLMProvider, T, inline_schema


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout_s: float = 45.0, temperature: float = 0.0) -> None:
        import openai

        self._client = openai.OpenAI(api_key=api_key, timeout=timeout_s, max_retries=2)
        self.model = model
        self.temperature = temperature

    def structured(self, system: str, user: str, tool_name: str, model: type[T]) -> T:
        fn = {"type": "function", "function": {"name": tool_name, "description": f"Return {model.__name__}.",
                                               "parameters": inline_schema(model)}}
        try:
            resp = self._client.chat.completions.create(
                model=self.model, temperature=self.temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=[fn], tool_choice={"type": "function", "function": {"name": tool_name}},
            )
        except Exception as exc:
            raise LLMError(f"OpenAI call failed: {type(exc).__name__}") from exc
        calls = resp.choices[0].message.tool_calls or []
        for call in calls:
            if call.function.name == tool_name:
                try:
                    return model.model_validate(json.loads(call.function.arguments))
                except (ValidationError, json.JSONDecodeError) as exc:
                    raise LLMError("Structured output failed validation") from exc
        raise LLMError("Model did not return the required function call")
