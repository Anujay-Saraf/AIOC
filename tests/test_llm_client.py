import asyncio

import pytest

from agents.llm import (
    OLLAMA_DEFAULT_MODEL,
    OllamaConfig,
    ProviderConfig,
    complete_json,
    complete_text,
    get_ollama_config,
    get_online_config,
)


class DummyConfig(ProviderConfig):
    pass


@pytest.mark.asyncio
async def test_complete_json_prefers_local_ollama(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_LOCAL_FALLBACK_SECONDS", "0.5")

    monkeypatch.setattr(
        "agents.llm.get_ollama_config",
        lambda: OllamaConfig(base_url="http://localhost:11434", model=OLLAMA_DEFAULT_MODEL, enabled=True, source="env"),
    )
    monkeypatch.setattr(
        "agents.llm.complete_local_json",
        lambda system, prompt, schema: asyncio.sleep(0.01, result={"answer": "local", "confidence": 0.9}),
    )
    monkeypatch.setattr(
        "agents.llm.get_online_config",
        lambda: DummyConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test", base_url="", source="env"),
    )

    result = await complete_json(system="Sys", prompt="Prompt", schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})
    assert result["answer"] == "local"
    assert result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_complete_json_falls_back_to_online_after_ollama_timeout(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_LOCAL_FALLBACK_SECONDS", "0.01")

    monkeypatch.setattr(
        "agents.llm.get_ollama_config",
        lambda: OllamaConfig(base_url="http://localhost:11434", model=OLLAMA_DEFAULT_MODEL, enabled=True, source="env"),
    )

    async def slow_local(system, prompt, schema):
        await asyncio.sleep(0.1)
        return {"answer": "slow"}

    monkeypatch.setattr("agents.llm.complete_local_json", slow_local)
    async def online_json(config, system, prompt, schema, schema_name, strict):
        return {"answer": "online"}

    monkeypatch.setattr(
        "agents.llm._openai_compatible_json",
        online_json,
    )
    monkeypatch.setattr(
        "agents.llm.get_online_config",
        lambda: DummyConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test", base_url="", source="env"),
    )

    result = await complete_json(system="Sys", prompt="Prompt", schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})
    assert result == {"answer": "online"}


@pytest.mark.asyncio
async def test_complete_text_prefers_local_ollama(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_LOCAL_FALLBACK_SECONDS", "0.5")

    monkeypatch.setattr(
        "agents.llm.get_ollama_config",
        lambda: OllamaConfig(base_url="http://localhost:11434", model=OLLAMA_DEFAULT_MODEL, enabled=True, source="env"),
    )
    monkeypatch.setattr(
        "agents.llm.complete_local_text",
        lambda system, prompt: asyncio.sleep(0.01, result="local-text"),
    )
    monkeypatch.setattr(
        "agents.llm.get_online_config",
        lambda: DummyConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test", base_url="", source="env"),
    )

    result = await complete_text(system="Sys", prompt="Prompt")
    assert result == "local-text"


@pytest.mark.asyncio
async def test_complete_text_falls_back_to_online_after_ollama_timeout(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_LOCAL_FALLBACK_SECONDS", "0.01")

    monkeypatch.setattr(
        "agents.llm.get_ollama_config",
        lambda: OllamaConfig(base_url="http://localhost:11434", model=OLLAMA_DEFAULT_MODEL, enabled=True, source="env"),
    )

    async def slow_local(system, prompt):
        await asyncio.sleep(0.1)
        return "slow-text"

    monkeypatch.setattr("agents.llm.complete_local_text", slow_local)
    async def online_text(config, system, prompt):
        return "online-text"

    monkeypatch.setattr(
        "agents.llm._openai_compatible_text",
        online_text,
    )
    monkeypatch.setattr(
        "agents.llm.get_online_config",
        lambda: DummyConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test", base_url="", source="env"),
    )

    result = await complete_text(system="Sys", prompt="Prompt")
    assert result == "online-text"
