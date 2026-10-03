"""Model string resolution: "provider/model" -> LLMProvider instance."""

from __future__ import annotations

import os

from pi.llm.base import LLMProvider

DEFAULT_MODEL = os.environ.get("PI_MODEL", "openai/gpt-4o")


def resolve(model: str, **kwargs) -> LLMProvider:
    """Resolve a 'provider/model' string into a streaming provider.

    Extra kwargs (api_key, base_url) are forwarded where supported.
    OpenAI-compatible third-party endpoints: set OPENAI_BASE_URL / OPENAI_API_KEY.
    OpenAI-compatible third-party endpoints: set OPENAI_BASE_URL / OPENAI_API_KEY.
    """
    provider_name, _, model_id = model.partition("/") #"openai/gpt-4o".partition("/") → ("openai", "/", "gpt-4o")
    if not model_id:
        raise ValueError(f"model must look like 'provider/model', got: {model!r}")

    if provider_name == "openai":
        from pi.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(
            model=model_id,
            api_key=kwargs.get("api_key") or os.environ.get("OPENAI_API_KEY"),
            base_url=kwargs.get("base_url") or os.environ.get("OPENAI_BASE_URL"),
            enable_search=bool(kwargs.get("enable_search")),
            builtin_tools=kwargs.get("builtin_tools"),
        )
    if provider_name == "anthropic":
        from pi.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=model_id,
            api_key=kwargs.get("api_key") or os.environ.get("ANTHROPIC_API_KEY"),
        )
    if provider_name == "fake":
        from pi.llm.fake import FakeProvider

        return FakeProvider(model=model_id)

    raise ValueError(
        f"unknown provider {provider_name!r} (expected openai / anthropic / fake)"
    )


def resolve_chain(
    model: str, chain: str | None = None, on_fallback=None, **kwargs
) -> LLMProvider:
    """Resolve a model, optionally wrapped in a FallbackProvider.

    chain: comma-separated model list, e.g.
    "openai/qwen3.8-max,openai/qwen3.8-flash,openai/deepseek-v4-flash-0731".
    Falls back to $PI_FALLBACK_CHAIN when chain is None; empty string disables.
    on_fallback: async (from_model, to_model, reason) callback, exceptions
    swallowed by FallbackProvider (metrics wiring).
    """
    if chain is None:
        chain = os.environ.get("PI_FALLBACK_CHAIN", "")
    models = [m.strip() for m in chain.split(",") if m.strip()]
    if not models:
        return resolve(model, **kwargs)

    from pi.llm.fallback import FallbackProvider

    # the explicitly requested model is always primary; the chain provides the
    # degradation order (any chain entry equal to it is skipped)
    primary = resolve(model, **kwargs)
    fallbacks = [resolve(m, **kwargs) for m in models if m != model]
    if not fallbacks:
        return primary
    return FallbackProvider(primary, fallbacks, on_fallback=on_fallback)
