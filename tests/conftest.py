from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_external_local_llm_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests opt in to Ollama explicitly instead of contacting the host."""
    monkeypatch.setenv("OLLAMA_ENABLED", "false")
