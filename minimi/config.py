from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    model: str = "gpt-5.4-mini"
    reasoning: str = "none"
    database_url: str = ""
    db_path: Path = ROOT / ".data/minimi.sqlite"
    max_daily_turns: int = 200
    demo_password: str = ""
    hosted: bool = False
    semantic_memory: bool = True
    embedding_model: str = "text-embedding-3-small"

    @classmethod
    def load(cls):
        hosted = os.environ.get("VERCEL") == "1"
        values = {} if hosted else dotenv_values(ROOT / ".env")

        def get(name, default=""):
            return os.environ.get(name) or values.get(name) or default

        return cls(
            api_key=get("OPENAI_API_KEY"),
            model=get("OPENAI_MODEL", "gpt-5.4-mini"),
            reasoning=get("REASONING_EFFORT", "none"),
            database_url=get("DATABASE_URL"),
            max_daily_turns=int(get("MAX_DAILY_TURNS", "200")),
            demo_password=get("DEMO_PASSWORD"),
            hosted=hosted,
            semantic_memory=get("SEMANTIC_MEMORY", "true").lower()
            in ("true", "1", "yes"),
            embedding_model=get("EMBEDDING_MODEL", "text-embedding-3-small"),
        )
