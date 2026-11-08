from __future__ import annotations

from ..config import get_settings
from .base import LLM, LLMError, parse_json

_override: LLM | None = None


def set_llm(llm: LLM | None) -> None:
    """Install a specific provider (tests use a fake)."""
    global _override
    _override = llm


def get_llm() -> LLM | None:
    """The configured provider, or None when no API key is set (the app then runs offline
    from its question bank)."""
    if _override is not None:
        return _override
    s = get_settings()
    if not s.llm_configured:
        return None
    if s.llm_provider == "claude":
        from .claude import ClaudeLLM

        return ClaudeLLM(s.anthropic_api_key, s.claude_model, s.claude_check_model)
    from .gemini import GeminiLLM

    return GeminiLLM(s.gemini_api_key, s.gemini_model)


__all__ = ["LLM", "LLMError", "get_llm", "set_llm", "parse_json"]
