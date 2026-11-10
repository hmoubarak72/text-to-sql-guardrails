"""Anthropic provider: structured output via forced tool use with a JSON schema."""
from __future__ import annotations

from pydantic import ValidationError

from app.llm.base import LLMError, LLMProvider, T, inline_schema


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_s: float = 45.0, temperature: float = 0.0) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=2)
        self.model = model
        self.temperature = temperature

    def structured(self, system: str, user: str, tool_name: str, model: type[T]) -> T:
        tool = {"name": tool_name, "description": f"Return the result as {model.__name__}.",
                "input_schema": inline_schema(model)}
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=2048, temperature=self.temperature, system=system,
                messages=[{"role": "user", "content": user}], tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except Exception as exc:  # network / auth / rate-limit errors
            raise LLMError(f"Anthropic call failed: {type(exc).__name__}") from exc
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                try:
                    return model.model_validate(block.input)
                except ValidationError as exc:
                    raise LLMError(f"Structured output failed validation: {exc.error_count()} errors") from exc
        raise LLMError("Model did not return the required tool call")
