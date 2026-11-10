"""Provider adapters tested against STUBBED SDK clients (no network). These verify request shape and structured-
output parsing only; they are not live-model tests."""
import json
from types import SimpleNamespace as NS

import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMError
from app.llm.openai_provider import OpenAIProvider
from app.models import GenerationOutput

pytestmark = pytest.mark.unit
OUT = {"status": "sql", "sql": "SELECT 1", "explanation": "x", "tables_used": [], "confidence": 0.7}


class _Anth:
    def __init__(self, content):
        self.kw, self._content = None, content
        self.messages = self

    def create(self, **kw):
        self.kw = kw
        return NS(content=self._content)


def _anthropic(content):
    p = AnthropicProvider.__new__(AnthropicProvider)
    p._client, p.model, p.temperature = _Anth(content), "m", 0.0
    return p


def test_anthropic_forced_tool_use_parsed():
    p = _anthropic([NS(type="tool_use", name="submit_sql", input=OUT)])
    out = p.generate("sys", "user", question="q")
    assert isinstance(out, GenerationOutput) and out.sql == "SELECT 1"
    kw = p._client.kw
    assert kw["tool_choice"] == {"type": "tool", "name": "submit_sql"} and kw["system"] == "sys"
    assert "$defs" not in json.dumps(kw["tools"][0]["input_schema"])


def test_anthropic_invalid_or_missing_tool_raises():
    with pytest.raises(LLMError):
        _anthropic([NS(type="text", text="hi")]).generate("s", "u", question="q")
    with pytest.raises(LLMError):
        _anthropic([NS(type="tool_use", name="submit_sql", input={"status": "nope"})]).generate("s", "u", question="q")


class _OAI:
    def __init__(self, args):
        self.kw, self._args = None, args
        self.chat = NS(completions=self)

    def create(self, **kw):
        self.kw = kw
        call = NS(function=NS(name="submit_sql", arguments=self._args))
        return NS(choices=[NS(message=NS(tool_calls=[call]))])


def test_openai_function_call_parsed():
    p = OpenAIProvider.__new__(OpenAIProvider)
    p._client, p.model, p.temperature = _OAI(json.dumps(OUT)), "m", 0.0
    assert p.generate("s", "u", question="q").sql == "SELECT 1"
    assert p._client.kw["tool_choice"]["function"]["name"] == "submit_sql"
    p._client = _OAI("{not json")
    with pytest.raises(LLMError):
        p.generate("s", "u", question="q")
