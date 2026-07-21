"""Fetch and cache provider model lists."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from mira_engine.config.loader import get_config_path
from mira_engine.config.schema import Config
from mira_engine.providers.factory import resolve_provider_proxy
from mira_engine.providers.registry import find_by_name

DEFAULT_MODEL_CACHE_TTL_SECONDS = 24 * 60 * 60
MODEL_CACHE_SCHEMA_VERSION = 1

_DEFAULT_MODEL_API_BASES = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qianfan": "https://qianfan.baidubce.com/v2",
}


class ModelFetchError(RuntimeError):
    """Model fetch failed with a user-actionable message."""


@dataclass(slots=True)
class ModelInfo:
    """A provider model entry normalized for MIRA config usage."""

    id: str
    config_model: str
    display_name: str | None = None
    context_window_tokens: int | None = None
    capabilities: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ModelCache:
    """Cached model list for one provider."""

    provider: str
    fetched_at: str
    models: list[ModelInfo]
    api_base: str | None = None
    account_id: str | None = None
    ttl_seconds: int = DEFAULT_MODEL_CACHE_TTL_SECONDS
    schema_version: int = MODEL_CACHE_SCHEMA_VERSION


def model_cache_dir(config_path: Path | None = None) -> Path:
    """Return the cache directory for fetched model lists."""
    base = (config_path or get_config_path()).expanduser().parent
    return base / "models"


def model_cache_path(provider_name: str, config_path: Path | None = None) -> Path:
    """Return the cache file path for a provider."""
    return model_cache_dir(config_path) / f"{_normalize_provider_name(provider_name)}.json"


def read_model_cache(provider_name: str, config_path: Path | None = None) -> ModelCache | None:
    """Read a provider model cache if present and valid."""
    path = model_cache_path(provider_name, config_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _cache_from_payload(payload)
    except Exception:
        return None


def read_all_model_caches(config_path: Path | None = None) -> list[ModelCache]:
    """Read all model cache files."""
    directory = model_cache_dir(config_path)
    if not directory.exists():
        return []
    caches: list[ModelCache] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            caches.append(_cache_from_payload(payload))
        except Exception:
            continue
    return caches


def write_model_cache(cache: ModelCache, config_path: Path | None = None) -> Path:
    """Write a model cache and return its path."""
    path = model_cache_path(cache.provider, config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_cache_to_payload(cache), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def clear_model_cache(provider_name: str | None = None, config_path: Path | None = None) -> list[Path]:
    """Clear one provider cache, or all model caches when provider is omitted."""
    if provider_name:
        paths = [model_cache_path(provider_name, config_path)]
    else:
        directory = model_cache_dir(config_path)
        paths = sorted(directory.glob("*.json")) if directory.exists() else []

    removed: list[Path] = []
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def is_cache_stale(cache: ModelCache, now: datetime | None = None) -> bool:
    """Return True when a cache has exceeded its TTL."""
    fetched_at = _parse_datetime(cache.fetched_at)
    if fetched_at is None:
        return True
    current = now or datetime.now(UTC)
    return (current - fetched_at).total_seconds() > cache.ttl_seconds


def current_provider_name(config: Config) -> str | None:
    """Resolve the provider currently selected by config."""
    configured = (config.agents.defaults.provider or "auto").replace("-", "_")
    if configured != "auto":
        spec = find_by_name(configured)
        return spec.name if spec else configured
    return config.get_provider_name(config.agents.defaults.model)


async def fetch_models_to_cache(
    config: Config,
    provider_name: str | None = None,
    config_path: Path | None = None,
) -> tuple[ModelCache, Path]:
    """Fetch models for a provider, cache them, and return the cache plus path."""
    resolved_provider = _normalize_provider_name(provider_name or current_provider_name(config) or "")
    if not resolved_provider:
        raise ModelFetchError("No provider configured. Set agents.defaults.provider first.")

    cache = await fetch_provider_models(config, resolved_provider)
    path = write_model_cache(cache, config_path)
    return cache, path


async def fetch_provider_models(config: Config, provider_name: str) -> ModelCache:
    """Fetch models from a provider without writing cache."""
    provider_name = _normalize_provider_name(provider_name)
    spec = find_by_name(provider_name)
    if not spec:
        raise ModelFetchError(f"Unknown provider: {provider_name}")

    if provider_name == "github_copilot":
        return await _fetch_github_copilot_models(config)
    if provider_name == "ollama":
        return await _fetch_ollama_models(config)
    if provider_name == "openai_codex":
        raise ModelFetchError("OpenAI Codex model fetch is not supported yet.")

    return await _fetch_openai_compatible_models(config, provider_name)


def models_for_provider(provider_name: str, config_path: Path | None = None) -> list[ModelInfo]:
    """Return cached models for a provider."""
    cache = read_model_cache(provider_name, config_path)
    return cache.models if cache else []


def all_cached_models(config_path: Path | None = None) -> list[ModelInfo]:
    """Return all cached models across providers."""
    models: list[ModelInfo] = []
    for cache in read_all_model_caches(config_path):
        models.extend(cache.models)
    return models


def config_model_for_provider(model_id: str, provider_name: str) -> str:
    """Return the model string that should be written to config.json."""
    value = model_id.strip()
    if not value:
        return value
    spec = find_by_name(_normalize_provider_name(provider_name))
    if not spec:
        return value
    if "/" not in value and spec.litellm_prefix:
        return f"{spec.litellm_prefix}/{value}"
    return value


def _cache_to_payload(cache: ModelCache) -> dict[str, Any]:
    payload = asdict(cache)
    payload["models"] = [asdict(model) for model in cache.models]
    return payload


def _cache_from_payload(payload: dict[str, Any]) -> ModelCache:
    models = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        config_model = str(item.get("config_model") or item.get("configModel") or "").strip()
        if not model_id or not config_model:
            continue
        context = item.get("context_window_tokens", item.get("contextWindowTokens"))
        models.append(
            ModelInfo(
                id=model_id,
                config_model=config_model,
                display_name=item.get("display_name") or item.get("displayName") or None,
                context_window_tokens=context if isinstance(context, int) else None,
                capabilities=[str(v) for v in item.get("capabilities") or []],
            )
        )
    return ModelCache(
        provider=str(payload.get("provider") or ""),
        api_base=payload.get("api_base") or payload.get("apiBase") or None,
        account_id=payload.get("account_id") or payload.get("accountId") or None,
        fetched_at=str(payload.get("fetched_at") or payload.get("fetchedAt") or ""),
        ttl_seconds=int(payload.get("ttl_seconds") or payload.get("ttlSeconds") or DEFAULT_MODEL_CACHE_TTL_SECONDS),
        schema_version=int(payload.get("schema_version") or payload.get("schemaVersion") or MODEL_CACHE_SCHEMA_VERSION),
        models=models,
    )


async def _fetch_github_copilot_models(config: Config) -> ModelCache:
    from mira_engine.providers.github_copilot_provider import (
        GitHubCopilotProvider,
        get_github_copilot_login_status,
    )

    provider = GitHubCopilotProvider(
        default_model=config.agents.defaults.model or "github_copilot/gpt-4.1",
        api_base=config.get_api_base("github_copilot/gpt-4.1"),
        proxy=resolve_provider_proxy(config),
    )
    token = await provider._get_copilot_access_token()
    api_base = provider.api_base.rstrip("/")
    payload = await _get_json(
        f"{api_base}/models",
        headers={
            **provider.extra_headers,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        proxy=provider.proxy,
    )
    models = _parse_openai_compatible_models(payload, "github_copilot")
    return _new_cache(
        provider="github_copilot",
        models=models,
        api_base=provider.api_base,
        account_id=getattr(get_github_copilot_login_status(), "account_id", None),
    )


async def _fetch_ollama_models(config: Config) -> ModelCache:
    provider_cfg = config.providers.ollama
    base = provider_cfg.api_base or "http://localhost:11434/v1"
    tags_url = f"{_ollama_root_url(base)}/api/tags"
    payload = await _get_json(tags_url, headers={}, proxy=resolve_provider_proxy(config))
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        raise ModelFetchError("Ollama returned an invalid model list.")

    models: list[ModelInfo] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("model") or item.get("name") or "").strip()
        if not model_id:
            continue
        models.append(
            ModelInfo(
                id=model_id,
                display_name=model_id,
                config_model=config_model_for_provider(model_id, "ollama"),
                capabilities=["chat"],
            )
        )
    return _new_cache(provider="ollama", models=_dedupe_models(models), api_base=base)


async def _fetch_openai_compatible_models(config: Config, provider_name: str) -> ModelCache:
    provider_cfg = getattr(config.providers, provider_name, None)
    spec = find_by_name(provider_name)
    api_base = (
        getattr(provider_cfg, "api_base", None)
        or (spec.default_api_base if spec else None)
        or _DEFAULT_MODEL_API_BASES.get(provider_name)
    )
    if not api_base:
        raise ModelFetchError(f"{provider_name} does not have an API base URL for model fetch.")

    api_key = getattr(provider_cfg, "api_key", None) if provider_cfg is not None else None
    if not api_key and not (provider_name == "custom" or (spec and spec.is_local)):
        raise ModelFetchError(f"{provider_name} requires an API key before fetching models.")

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = await _get_json(
        f"{_normalize_openai_base(api_base)}/models",
        headers=headers,
        proxy=resolve_provider_proxy(config),
    )
    models = _parse_openai_compatible_models(payload, provider_name)
    return _new_cache(provider=provider_name, models=models, api_base=api_base)


async def _get_json(url: str, headers: dict[str, str], proxy: str | None = None) -> dict[str, Any]:
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0, connect=20.0),
        "follow_redirects": True,
        "trust_env": True,
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        raise ModelFetchError(f"Model fetch failed with HTTP {status}: {e.response.text[:300]}") from e
    except httpx.HTTPError as e:
        raise ModelFetchError(f"Model fetch failed: {e}") from e
    if not isinstance(payload, dict):
        raise ModelFetchError("Provider returned a non-object model list.")
    return payload


def _parse_openai_compatible_models(payload: dict[str, Any], provider_name: str) -> list[ModelInfo]:
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if raw_models is None and isinstance(payload.get("models"), list):
        raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ModelFetchError("Provider returned an invalid OpenAI-compatible model list.")

    models: list[ModelInfo] = []
    for item in raw_models:
        model = _model_from_payload_item(item, provider_name)
        if model:
            models.append(model)
    if not models:
        raise ModelFetchError("Provider returned no usable models.")
    return _dedupe_models(models)


def _model_from_payload_item(item: Any, provider_name: str) -> ModelInfo | None:
    if isinstance(item, str):
        model_id = item.strip()
        payload: dict[str, Any] = {}
    elif isinstance(item, dict):
        if provider_name == "github_copilot" and not _is_supported_copilot_chat_model(item):
            return None
        model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        payload = item
    else:
        return None
    if not model_id:
        return None

    context = _extract_context_window(payload)
    display = payload.get("name") or payload.get("display_name") or payload.get("displayName") or model_id
    capabilities = payload.get("capabilities") or payload.get("supported_generation_methods") or ["chat"]
    if isinstance(capabilities, dict):
        capability_type = capabilities.get("type")
        supports = capabilities.get("supports")
        capability_names = [str(capability_type)] if capability_type else []
        if isinstance(supports, dict):
            capability_names.extend(key for key, enabled in supports.items() if enabled is True)
        capabilities = capability_names or ["chat"]
    if not isinstance(capabilities, list):
        capabilities = ["chat"]

    return ModelInfo(
        id=model_id,
        display_name=str(display) if display else model_id,
        config_model=config_model_for_provider(model_id, provider_name),
        context_window_tokens=context,
        capabilities=[str(v) for v in capabilities],
    )


def _extract_context_window(payload: dict[str, Any]) -> int | None:
    for key in (
        "context_window_tokens",
        "contextWindowTokens",
        "context_length",
        "contextLength",
        "max_context_length",
        "maxContextLength",
        "input_token_limit",
        "inputTokenLimit",
    ):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
    top_provider = payload.get("top_provider")
    if isinstance(top_provider, dict):
        context = top_provider.get("context_length")
        if isinstance(context, int) and context > 0:
            return context
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, dict):
        limits = capabilities.get("limits")
        if isinstance(limits, dict):
            context = limits.get("max_context_window_tokens") or limits.get("max_prompt_tokens")
            if isinstance(context, int) and context > 0:
                return context
    return None


def _is_supported_copilot_chat_model(payload: dict[str, Any]) -> bool:
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("type") != "chat":
        return False
    if payload.get("model_picker_enabled") is False:
        return False
    policy = payload.get("policy")
    if isinstance(policy, dict) and policy.get("state") == "disabled":
        return False
    endpoints = payload.get("supported_endpoints")
    if isinstance(endpoints, list):
        return "/chat/completions" in endpoints
    return True


def _new_cache(
    provider: str,
    models: list[ModelInfo],
    api_base: str | None = None,
    account_id: str | None = None,
) -> ModelCache:
    return ModelCache(
        provider=provider,
        api_base=api_base,
        account_id=account_id,
        fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        models=_dedupe_models(models),
    )


def _normalize_openai_base(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base


def _ollama_root_url(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def _normalize_provider_name(provider_name: str) -> str:
    return (provider_name or "").strip().replace("-", "_").lower()


def _dedupe_models(models: list[ModelInfo]) -> list[ModelInfo]:
    seen: set[str] = set()
    result: list[ModelInfo] = []
    for model in sorted(models, key=lambda item: item.config_model.lower()):
        key = model.config_model
        if key in seen:
            continue
        seen.add(key)
        result.append(model)
    return result


def _parse_datetime(value: str) -> datetime | None:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
