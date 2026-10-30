from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str | None = field(repr=False)
    model: str

    @property
    def key_configured(self) -> bool:
        return bool(self.api_key)

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "Gemini API key is missing. Set GEMINI_API_KEY in the process "
                "environment or the project .env file; see .env.example."
            )
        return self.api_key


def load_gemini_config(*, env_file: Path | None = None) -> GeminiConfig:
    load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env", override=False)
    api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    return GeminiConfig(api_key=api_key, model=model)
