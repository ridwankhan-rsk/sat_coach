from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data" / "seed"

load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    llm_provider: str
    gemini_api_key: str
    gemini_model: str
    anthropic_api_key: str
    claude_model: str
    claude_check_model: str
    data_dir: Path

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'sat_coach.db'}"

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "claude":
            return bool(self.anthropic_api_key)
        return bool(self.gemini_api_key)


def get_settings() -> Settings:
    data_dir = Path(os.getenv("SAT_DATA_DIR", "data/private"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5").strip(),
        claude_check_model=os.getenv("CLAUDE_CHECK_MODEL", "claude-haiku-5-5").strip(),
        data_dir=data_dir,
    )
