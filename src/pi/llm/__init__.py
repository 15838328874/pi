"""Unified streaming LLM provider interface (the pi-ai layer)."""

from pi.llm.registry import DEFAULT_MODEL, resolve

__all__ = ["DEFAULT_MODEL", "resolve"]
