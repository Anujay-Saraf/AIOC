"""Unified async LLM clients for local and configured online providers.

Agent workflows continue to use the selected online provider through
``complete_json``/``complete_text``.  User-query routing may first call the
separate Ollama helpers and only use the online provider as a fallback.

Token-budget enforcement and prompt chunking are layered on top so that each
incident consumes a bounded number of tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama


OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
CLAUDE_DEFAULT_MODEL = "claude-3-5-haiku-latest"
OLLAMA_DEFAULT_MODEL = "qwen2.5:1b"

PROVIDER_CONNECTOR_TYPES = {
    "llm_openai": "openai",
    "llm_gemini": "gemini",
    "llm_groq": "groq",
    "llm_claude": "claude",
}

PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "openai": {"model": OPENAI_DEFAULT_MODEL, "base_url": ""},
    "gemini": {
        "model": GEMINI_DEFAULT_MODEL,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "groq": {
        "model": GROQ_DEFAULT_MODEL,
        "base_url": "https://api.groq.com/openai/v1",
    },
    "claude": {
        "model": CLAUDE_DEFAULT_MODEL,
        "base_url": "https://api.anthropic.com",
    },
}

PROVIDER_KEY_ENVS = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
}

PROVIDER_MODEL_ENVS = {
    "openai": ("OPENAI_MODEL",),
    "gemini": ("GEMINI_MODEL",),
    "groq": ("GROQ_MODEL",),
    "claude": ("ANTHROPIC_MODEL", "CLAUDE_MODEL"),
}

# --- Token budget enforcement -------------------------------------------------
TOKEN_BUDGET_PER_INCIDENT = 50000
_SPEND: Dict[str, int] = {}

# Cached tiktoken encoder (lazily resolved) so repeated calls are cheap.
_TIKTOKEN_ENCODER = None
_TIKTOKEN_RESOLVED = False

# Sentence boundary splitter used by ``chunk_prompt``.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str
    base_url: str = ""
    source: str = "environment"


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    model: str
    enabled: bool
    source: str


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on", "active"}


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _runtime_connectors(*types: str) -> list[Dict[str, Any]]:
    try:
        from agents.connector_registry import runtime_connectors

        return runtime_connectors(*types)
    except Exception:
        # A broken or not-yet-created registry must not disable env-based LLMs.
        return []


def _connector_secret(config: Dict[str, Any]) -> str:
    env_name = str(config.get("api_key_env") or config.get("credential_env") or "").strip()
    if env_name and os.getenv(env_name):
        return str(os.getenv(env_name))
    direct = str(config.get("api_key") or "").strip()
    return "" if direct == "********" else direct


def _selected_connector_config() -> Optional[ProviderConfig]:
    records = _runtime_connectors(*PROVIDER_CONNECTOR_TYPES)
    if not records:
        return None
    connector_id = os.getenv("ONLINE_LLM_CONNECTOR_ID", "").strip()
    requested_provider = os.getenv("LLM_PROVIDER", "").strip().casefold()

    def rank(item: Dict[str, Any]) -> tuple[int, int, str]:
        provider = PROVIDER_CONNECTOR_TYPES.get(str(item.get("type")), "")
        preferred = int(bool(connector_id and str(item.get("id")) == connector_id))
        preferred += int(bool(requested_provider and provider == requested_provider))
        active = int(_truthy((item.get("config") or {}).get("active")))
        return preferred, active, str(item.get("updated_at") or "")

    for record in sorted(records, key=rank, reverse=True):
        provider = PROVIDER_CONNECTOR_TYPES.get(str(record.get("type")))
        if not provider:
            continue
        config = record.get("config") or {}
        api_key = _connector_secret(config)
        if not api_key:
            continue
        defaults = PROVIDER_DEFAULTS[provider]
        return ProviderConfig(
            provider=provider,
            model=str(config.get("model") or defaults["model"]).strip(),
            api_key=api_key,
            base_url=str(config.get("base_url") or defaults["base_url"]).strip(),
            source=f"connector:{record.get('id')}",
        )
    return None


def _environment_provider_config() -> Optional[ProviderConfig]:
    requested = os.getenv("LLM_PROVIDER", "").strip().casefold()
    candidates = [requested] if requested in PROVIDER_DEFAULTS else []
    candidates += [name for name in ("openai", "gemini", "groq", "claude") if name not in candidates]
    for provider in candidates:
        api_key = _first_env(PROVIDER_KEY_ENVS[provider])
        if not api_key:
            continue
        model = _first_env(PROVIDER_MODEL_ENVS[provider]) or PROVIDER_DEFAULTS[provider]["model"]
        base_env = os.getenv(f"{provider.upper()}_BASE_URL", "").strip()
        return ProviderConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_env or PROVIDER_DEFAULTS[provider]["base_url"],
        )
    return None


def get_online_config() -> Optional[ProviderConfig]:
    return _selected_connector_config() or _environment_provider_config()


def get_provider() -> Optional[str]:
    config = get_online_config()
    return config.provider if config else None


def llm_available() -> bool:
    return get_online_config() is not None


def get_model() -> str:
    config = get_online_config()
    return config.model if config else "heuristic"


def get_ollama_config() -> OllamaConfig:
    enabled = os.getenv("OLLAMA_ENABLED", "true").strip().casefold() not in {"0", "false", "no", "off"}
    records = _runtime_connectors("ollama")
    if records:
        record = sorted(records, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[0]
        config = record.get("config") or {}
        return OllamaConfig(
            base_url=str(config.get("endpoint") or os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/"),
            model=str(config.get("model") or os.getenv("OLLAMA_MODEL") or OLLAMA_DEFAULT_MODEL),
            enabled=enabled,
            source=f"connector:{record.get('id')}",
        )
    return OllamaConfig(
        base_url=str(os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/"),
        model=os.getenv("OLLAMA_MODEL", OLLAMA_DEFAULT_MODEL).strip() or OLLAMA_DEFAULT_MODEL,
        enabled=enabled,
        source="environment",
    )


# --- Token helpers ------------------------------------------------------------
def count_tokens(text: str, model: str | None = None) -> int:
    """Return an approximate token count for ``text``.

    Uses tiktoken when importable (with a model-specific or fallback encoder),
    otherwise falls back to a simple ``len(text) // 4`` heuristic.
    """
    global _TIKTOKEN_ENCODER, _TIKTOKEN_RESOLVED
    if _TIKTOKEN_RESOLVED and _TIKTOKEN_ENCODER is not None:
        try:
            if model:
                return len(_TIKTOKEN_ENCODER.encode(text))
            return len(_TIKTOKEN_ENCODER.encode(text))
        except Exception:
            return max(1, len(text) // 4)
    if not _TIKTOKEN_RESOLVED:
        try:
            import tiktoken  # type: ignore

            try:
                _TIKTOKEN_ENCODER = tiktoken.encoding_for_model(model or get_model())
            except Exception:
                _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _TIKTOKEN_ENCODER = None
        _TIKTOKEN_RESOLVED = True
    if _TIKTOKEN_ENCODER is not None:
        try:
            return len(_TIKTOKEN_ENCODER.encode(text))
        except Exception:
            return max(1, len(text) // 4)
    return max(1, len(text) // 4)


def chunk_prompt(prompt: str, max_tokens: int, reserve_for_response: int = 1000) -> List[str]:
    """Split a large prompt into chunks that fit within ``max_tokens``.

    Splits on sentence boundaries so chunks never break mid-sentence. Each
    chunk respects ``max_tokens - reserve_for_response`` as its token ceiling.
    A contiguous run of sentences that together still exceed the limit is then
    hard-split at the character level as a last resort.
    """
    budget = max(1, max_tokens - reserve_for_response)
    if count_tokens(prompt) <= budget:
        return [prompt] if prompt.strip() else []

    sentences = _SENTENCE_SPLIT_RE.split(prompt)
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            chunks.append(" ".join(current).strip())
            current = []
            current_tokens = 0

    for sentence in sentences:
        piece = sentence.strip()
        if not piece:
            continue
        piece_tokens = count_tokens(piece)
        if piece_tokens > budget:
            # Single sentence too big: hard split on characters.
            flush()
            chars_per_token = max(1, len(piece) / max(1, piece_tokens))
            limit = int(budget * chars_per_token)
            for i in range(0, len(piece), limit):
                sub = piece[i : i + limit].strip()
                if sub:
                    chunks.append(sub)
            continue
        if current_tokens + piece_tokens > budget and current:
            flush()
        current.append(piece)
        current_tokens += piece_tokens

    flush()
    return chunks


class LLMUnavailableError(RuntimeError):
    """Raised when the primary LLM (and any configured fallback) cannot be reached.

    Wraps provider/billing/quota/rate-limit/402 failures so callers can degrade
    gracefully instead of surfacing a raw provider traceback.
    """


# Tracks whether the most recent online provider call succeeded.  Callers can
# consult ``llm_healthy()`` to decide whether to skip optional LLM enrichment.
_LLM_LAST_CALL_FAILED = False


def llm_healthy() -> bool:
    """Return ``False`` if the most recent online LLM call failed.

    Lets callers skip speculative LLM enrichment when the provider is
    unhealthy (e.g. quota/402 errors) rather than paying for repeated failures.
    """
    return not _LLM_LAST_CALL_FAILED


def _mark_llm_failed() -> None:
    global _LLM_LAST_CALL_FAILED
    _LLM_LAST_CALL_FAILED = True


def _mark_llm_ok() -> None:
    global _LLM_LAST_CALL_FAILED
    _LLM_LAST_CALL_FAILED = False


def _secondary_config(config: ProviderConfig) -> Optional[ProviderConfig]:
    """Return a fallback ``ProviderConfig`` using a secondary model, or ``None``.

    Resolution order:
      1. ``LLM_FALLBACK_MODEL`` env var (generic).
      2. ``<PROVIDER>_FALLBACK_MODEL`` env var (e.g. ``OPENAI_FALLBACK_MODEL``).
      3. A hardcoded cheaper default per provider, only when it differs from the
         primary model.

    Returns ``None`` when no distinct fallback model is available so the caller
    raises ``LLMUnavailableError`` instead of retrying the same model.
    """
    provider = config.provider
    fallback_model = (
        os.getenv("LLM_FALLBACK_MODEL", "").strip()
        or os.getenv(f"{provider.upper()}_FALLBACK_MODEL", "").strip()
    )
    if not fallback_model:
        cheap_defaults = {
            "openai": "gpt-4o-mini",
            "gemini": "gemini-2.5-flash",
            "groq": "llama-3.3-70b-versatile",
            "claude": "claude-3-5-haiku-latest",
        }
        candidate = cheap_defaults.get(provider)
        if candidate and candidate != config.model:
            fallback_model = candidate
    if not fallback_model or fallback_model == config.model:
        return None
    return ProviderConfig(
        provider=provider,
        model=fallback_model,
        api_key=config.api_key,
        base_url=config.base_url,
        source=f"{config.source}+fallback",
    )


async def _online_json(
    config: ProviderConfig,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    schema_name: str,
) -> Dict[str, Any]:
    """Single online-provider JSON call (no fallback handling)."""
    if config.provider in {"openai", "gemini", "groq"}:
        return await _openai_compatible_json(
            config,
            system,
            prompt,
            schema,
            schema_name,
            strict=(config.provider == "openai"),
        )
    text = await _claude_text(config, system, _json_prompt(prompt, schema))
    return _parse_json_object(text)


async def _openrouter_json(
    model: str,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    schema_name: str,
    api_key: str,
) -> Dict[str, Any]:
    """JSON completion via OpenRouter's OpenAI-compatible API (last-resort fallback).

    Skips the connector/env resolution used elsewhere and builds a ``ChatOpenAI``
    client directly against ``https://openrouter.ai/api/v1``.  Should only be
    reached when ``OPENROUTER_API_KEY`` is set, so the API key is never hardcoded.
    """
    client = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=float(os.getenv("OPENROUTER_TEMPERATURE", "0.1")),
    )
    response = await asyncio.to_thread(
        client.generate,
        [[HumanMessage(content=system), HumanMessage(content=_json_prompt(prompt, schema))]],
    )
    return _parse_json_object(_extract_generation_text(response))


def _openrouter_fallback_model() -> str:
    """Sensible default OpenRouter model, overridable via ``OPENROUTER_FALLBACK_MODEL``."""
    return os.getenv("OPENROUTER_FALLBACK_MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"


async def complete_json(
    system: str, prompt: str, schema: Dict[str, Any], schema_name: str = "response"
) -> Dict[str, Any]:
    """Ask the global LLM client for JSON conforming to ``schema``.

    Local Ollama is preferred and will fall back to the configured online
    provider if it fails or does not return within ``OLLAMA_LOCAL_FALLBACK_SECONDS``.

    Online provider calls are wrapped in try/except.  On a provider/quota/402/
    rate-limit error the call is retried once against a secondary model (when
    one is configured).  If both attempts fail, ``LLMUnavailableError`` is raised
    instead of a raw provider traceback, and ``llm_healthy()`` returns ``False``.
    """
    local = get_ollama_config()
    if local.enabled:
        try:
            fallback_seconds = float(os.getenv("OLLAMA_LOCAL_FALLBACK_SECONDS", "15"))
            return await asyncio.wait_for(
                complete_local_json(system=system, prompt=prompt, schema=schema),
                timeout=fallback_seconds,
            )
        except Exception as exc:
            print(f"[llm] local Ollama JSON failed or timed out, falling back to online provider: {exc}")
    config = get_online_config()
    if not config:
        raise RuntimeError(
            "No online LLM configured (add an Admin LLM connector or set a provider API key)"
        )
    try:
        result = await _online_json(config, system, prompt, schema, schema_name)
    except Exception as exc:
        # Provider/quota/402/rate-limit failure from the primary model. Walk the
        # fallback chain: (1) a configured secondary model, then (2) OpenRouter.
        # Only raise LLMUnavailableError once every option has been exhausted.
        _mark_llm_failed()
        secondary = _secondary_config(config)
        if secondary is not None:
            try:
                print(
                    f"[llm] primary '{config.provider}:{config.model}' failed "
                    f"({type(exc).__name__}: {exc}); retrying with fallback model "
                    f"'{secondary.model}'"
                )
                result = await _online_json(secondary, system, prompt, schema, schema_name)
            except Exception as fallback_exc:
                exc = fallback_exc
            else:
                _mark_llm_ok()
                return result
        # Last-resort fallback: OpenRouter (OpenAI-compatible), only if a key exists.
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if openrouter_key:
            or_model = _openrouter_fallback_model()
            try:
                print(
                    f"[llm] primary/fallback LLM unavailable; trying OpenRouter model "
                    f"'{or_model}'"
                )
                result = await _openrouter_json(
                    or_model, system, prompt, schema, schema_name, openrouter_key
                )
            except Exception as or_exc:
                exc = or_exc
            else:
                _mark_llm_ok()
                return result
        raise LLMUnavailableError(
            f"All LLM providers unavailable "
            f"(primary={config.provider}:{config.model}"
            + (f", fallback={secondary.model}" if secondary else "")
            + (f", openrouter={or_model}" if openrouter_key else "")
            + f"): {type(exc).__name__}: {exc}"
        ) from exc
    _mark_llm_ok()
    return result


async def complete_text(system: str, prompt: str) -> str:
    local = get_ollama_config()
    if local.enabled:
        try:
            fallback_seconds = float(os.getenv("OLLAMA_LOCAL_FALLBACK_SECONDS", "15"))
            return await asyncio.wait_for(
                complete_local_text(system=system, prompt=prompt),
                timeout=fallback_seconds,
            )
        except Exception as exc:
            print(f"[llm] local Ollama text failed or timed out, falling back to online provider: {exc}")
    config = get_online_config()
    if not config:
        raise RuntimeError(
            "No online LLM configured (add an Admin LLM connector or set a provider API key)"
        )
    if config.provider in {"openai", "gemini", "groq"}:
        return await _openai_compatible_text(config, system, prompt)
    return await _claude_text(config, system, prompt)


async def complete_local_text(system: str, prompt: str) -> str:
    config = get_ollama_config()
    if not config.enabled:
        raise RuntimeError("Ollama query tier is disabled")
    model = ChatOllama(
        model=config.model,
        base_url=config.base_url,
        temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0.1")),
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "45")),
    )
    response = await asyncio.to_thread(
        model.generate,
        [[HumanMessage(content=system), HumanMessage(content=prompt)]],
    )
    return _extract_generation_text(response)


async def complete_local_json(
    system: str,
    prompt: str,
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    text = await complete_local_text(system, _json_prompt(prompt, schema))
    return _parse_json_object(text)


async def ollama_health() -> Dict[str, Any]:
    config = get_ollama_config()
    result: Dict[str, Any] = {
        "enabled": config.enabled,
        "reachable": False,
        "model": config.model,
        "model_available": False,
        "feasible": False,
        "source": config.source,
    }
    if not config.enabled:
        return result
    try:
        timeout = min(5.0, float(os.getenv("OLLAMA_HEALTH_TIMEOUT_SECONDS", "2")))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{config.base_url}/api/tags")
            response.raise_for_status()
        models = response.json().get("models") or []
        names = {
            str(item.get("name") or item.get("model") or "")
            for item in models
            if isinstance(item, dict)
        }
        result["reachable"] = True
        result["model_available"] = config.model in names
        result["feasible"] = result["reachable"] and result["model_available"]
        result["available_models"] = sorted(name for name in names if name)[:20]
        if not result["model_available"]:
            result["detail"] = f"Local Ollama model {config.model} is not installed on the host."
    except Exception as exc:
        result["detail"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return result


def public_routing_config() -> Dict[str, Any]:
    online = get_online_config()
    local = get_ollama_config()
    return {
        "order": ["graph_query_memory", "ollama", "online_provider", "heuristic"],
        "local": {
            "enabled": local.enabled,
            "provider": "ollama",
            "model": local.model,
            "source": local.source,
        },
        "online": {
            "configured": online is not None,
            "provider": online.provider if online else None,
            "model": online.model if online else None,
            "source": online.source if online else None,
        },
    }


def _extract_generation_text(result: Any) -> str:
    generations = getattr(result, "generations", None) or []
    if not generations or not generations[0]:
        return ""
    first = generations[0][0]
    content = getattr(first, "text", None)
    if content is not None:
        return str(content)
    message = getattr(first, "message", None)
    if message is not None:
        return getattr(message, "content", "") or str(message)
    return str(first)


def _build_langchain_chat_model(config: ProviderConfig) -> Any:
    if config.provider == "openai":
        return ChatOpenAI(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or None,
            temperature=float(os.getenv("OPENAI_TEMPERATURE", "0.1")),
        )
    if config.provider == "gemini":
        return ChatGoogleGenerativeAI(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or None,
            temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.1")),
            convert_system_message_to_human=True,
        )
    if config.provider == "groq":
        return ChatGroq(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or None,
            temperature=float(os.getenv("GROQ_TEMPERATURE", "0.7")),
        )
    raise ValueError(f"Unsupported online LLM provider: {config.provider}")


async def _langchain_text(config: ProviderConfig, system: str, prompt: str) -> str:
    model = _build_langchain_chat_model(config)
    response = await asyncio.to_thread(
        model.generate,
        [[HumanMessage(content=system), HumanMessage(content=prompt)]],
    )
    return _extract_generation_text(response)


async def _langchain_json(
    config: ProviderConfig,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    schema_name: str,
    *,
    strict: bool,
) -> Dict[str, Any]:
    text = await _langchain_text(config, system, _json_prompt(prompt, schema))
    return _parse_json_object(text)


async def _openai_compatible_json(
    config: ProviderConfig,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    schema_name: str,
    *,
    strict: bool,
) -> Dict[str, Any]:
    return await _langchain_json(config, system, prompt, schema, schema_name, strict=strict)


async def _openai_compatible_text(config: ProviderConfig, system: str, prompt: str) -> str:
    return await _langchain_text(config, system, prompt)


async def _claude_text(config: ProviderConfig, system: str, prompt: str) -> str:
    base = (config.base_url or PROVIDER_DEFAULTS["claude"]["base_url"]).rstrip("/")
    endpoint = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    timeout = float(os.getenv("ONLINE_LLM_TIMEOUT_SECONDS", "60"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            endpoint,
            headers={
                "x-api-key": config.api_key,
                "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
                "content-type": "application/json",
            },
            json={
                "model": config.model,
                "max_tokens": int(os.getenv("ANTHROPIC_MAX_TOKENS", "2048")),
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
    content = response.json().get("content") or []
    return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))


def _json_prompt(prompt: str, schema: Dict[str, Any]) -> str:
    return (
        f"{prompt}\n\nReturn only one valid JSON object matching this JSON Schema. "
        f"Do not use Markdown fences.\n{json.dumps(schema, ensure_ascii=False)}"
    )


def _parse_json_object(value: str) -> Dict[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").strip()
        cleaned = cleaned.removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM did not return a JSON object")
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


# --- Budgeted JSON completion -------------------------------------------------
def _record_spend(incident_id: Optional[str], tokens: int) -> None:
    if not incident_id:
        return
    _SPEND[incident_id] = _SPEND.get(incident_id, 0) + tokens


def get_incident_spend(incident_id: str) -> int:
    """Return tokens spent so far for ``incident_id``."""
    return _SPEND.get(incident_id, 0)


def reset_incident_spend(incident_id: str) -> None:
    """Clear recorded spend for ``incident_id`` (e.g. at incident close)."""
    _SPEND.pop(incident_id, None)


async def complete_json_with_budget(
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    schema_name: str = "response",
    incident_id: Optional[str] = None,
    max_tokens: int = 4000,
) -> Dict[str, Any]:
    """Complete a JSON task while enforcing a per-incident token budget.

    Computes prompt tokens before calling the model. When the prompt exceeds
    ``max_tokens`` it is chunked (preserving sentence boundaries) and each chunk
    is summarized, then a final synthesis call produces the JSON. The estimated
    spend is subtracted from the incident budget; a ``RuntimeError`` is raised
    when the budget would be exceeded. Otherwise the call delegates to
    ``complete_json`` and the spend is recorded in ``_spend``.
    """
    model = get_model()
    prompt_tokens = count_tokens(system) + count_tokens(prompt)

    if incident_id is not None:
        remaining = TOKEN_BUDGET_PER_INCIDENT - _SPEND.get(incident_id, 0)
        if prompt_tokens + max_tokens > remaining:
            raise RuntimeError(
                f"Token budget exceeded for incident {incident_id}: "
                f"need ~{prompt_tokens + max_tokens} tokens but only {remaining} remain "
                f"of {TOKEN_BUDGET_PER_INCIDENT}."
            )

    if prompt_tokens <= max_tokens:
        result = await complete_json(system, prompt, schema, schema_name)
        _record_spend(incident_id, prompt_tokens + max_tokens)
        return result

    # Prompt too large: chunk, summarize each chunk, then synthesize.
    chunks = chunk_prompt(prompt, max_tokens)
    summarized_parts: List[str] = []
    synthesis_prompt_parts: List[str] = []
    for idx, chunk in enumerate(chunks):
        chunk_tokens = count_tokens(chunk)
        if incident_id is not None:
            remaining = TOKEN_BUDGET_PER_INCIDENT - _SPEND.get(incident_id, 0)
            if chunk_tokens + max_tokens > remaining:
                raise RuntimeError(
                    f"Token budget exceeded while chunking incident {incident_id}: "
                    f"need ~{chunk_tokens + max_tokens} tokens but only {remaining} remain."
                )
        try:
            summary = await complete_text(
                system,
                f"Summarize the following incident context concisely, preserving "
                f"every concrete fact, identifier, timestamp, and error message. "
                f"Part {idx + 1}/{len(chunks)}.\n\n{chunk}",
            )
        except LLMUnavailableError:
            raise
        except Exception as exc:
            _mark_llm_failed()
            raise LLMUnavailableError(
                f"Chunk summarization failed during budgeted completion: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        summarized_parts.append(summary)
        synthesis_prompt_parts.append(f"[Part {idx + 1}/{len(chunks)}]\n{summary}")
        _record_spend(incident_id, chunk_tokens + max_tokens)

    synthesis_prompt = (
        "Synthesize the following summarized incident context into a single "
        "coherent view, then answer the original request.\n\n"
        + "\n\n".join(synthesis_prompt_parts)
    )
    result = await complete_json(system, synthesis_prompt, schema, schema_name)
    _record_spend(incident_id, count_tokens(synthesis_prompt) + max_tokens)
    return result
