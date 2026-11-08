from __future__ import annotations

from typing import Any

from .base import LLMError, parse_json


class ClaudeLLM:
    """Optional provider: Anthropic Claude (paid API key)."""

    def __init__(self, api_key: str, model: str, check_model: str):
        import anthropic

        # The SDK retries 429/5xx itself (max_retries).
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4)
        self.model = model
        self.check_model = check_model
        self.name = f"claude:{model}"

    def generate_json(self, system: str, prompt: str, *, check: bool = False) -> Any:
        resp = self.client.messages.create(
            model=self.check_model if check else self.model,
            max_tokens=16000,
            system=system + "\nRespond with JSON only, no prose and no code fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            raise LLMError("Claude declined the request.")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return parse_json(text)
