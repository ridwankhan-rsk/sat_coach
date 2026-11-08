from __future__ import annotations

import json
import re
import time
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def generate_json(self, system: str, prompt: str, *, check: bool = False) -> Any:
        """Return parsed JSON. `check=True` asks for the cheaper model used for blind re-solves."""
        ...


def parse_json(text: str) -> Any:
    """Parse model output that should be JSON, tolerating ``` fences or stray prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Model did not return valid JSON: {text[:200]!r}")


def with_backoff(fn, *, is_rate_limit, attempts: int = 5, base: float = 4.0):
    """Retry fn() on rate-limit errors (free tiers allow only a few requests per minute)."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - provider SDKs raise different types
            if not is_rate_limit(e) or i == attempts - 1:
                raise
            time.sleep(base * (2**i))
