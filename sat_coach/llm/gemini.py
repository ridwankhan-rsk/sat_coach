from __future__ import annotations

from typing import Any

from .base import LLMError, parse_json, with_backoff


class GeminiLLM:
    """Google Gemini via the free tier of the Gemini API (key from Google AI Studio)."""

    def __init__(self, api_key: str, model: str):
        from google import genai

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.name = f"gemini:{model}"

    def generate_json(self, system: str, prompt: str, *, check: bool = False) -> Any:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.2 if check else 0.9,
        )

        def call():
            return self.client.models.generate_content(model=self.model, contents=prompt, config=config)

        def is_rate_limit(e: Exception) -> bool:
            return isinstance(e, errors.APIError) and getattr(e, "code", None) in (429, 503)

        resp = with_backoff(call, is_rate_limit=is_rate_limit)
        if not resp.text:
            raise LLMError("Gemini returned an empty response (possibly blocked).")
        return parse_json(resp.text)
