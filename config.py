"""Centralized configuration loaded from environment variables.

No defaults: every value must be present in the .env file. Missing variables
raise KeyError at load() time, so misconfiguration fails fast at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent


def _parse_admin_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Persona:
    name: str
    short_description: str
    welcome: str
    help_user: tuple[str, ...]
    help_admin: tuple[str, ...]
    prompt: str


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_ids: set[int]
    default_model: str  # "claude" or "ollama"

    anthropic_api_key: str
    anthropic_model: str
    claude_max_tokens: int

    ollama_host: str
    ollama_model: str

    persona: Persona
    db_path: Path
    history_max_turns: int  # number of user+assistant pairs replayed to the LLM

    embedding_model: str
    rag_path: Path
    rag_top_k: int
    rag_chunk_size: int
    rag_chunk_overlap: int
    rag_embed_num_ctx: int

    rock_bottom_window: int       # rolling window of latest scores averaged for alerting
    rock_bottom_threshold: float  # avg below this (on 0..10) triggers admin alert
    rock_bottom_alert_low_msgs: int  # how many lowest-scoring user msgs attach to an alert

    valid_models: tuple[str, ...] = field(default_factory=lambda: VALID_MODELS)


VALID_MODELS: tuple[str, ...] = ("claude", "ollama")


def _load_persona(path: Path) -> Persona:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Persona(
        name=data["name"],
        short_description=data["short_description"],
        welcome=data["welcome"].rstrip("\n"),
        help_user=tuple(data["help_user"]),
        help_admin=tuple(data["help_admin"]),
        prompt=data["prompt"],
    )


def load() -> Config:
    default_model = os.environ["DEFAULT_MODEL"].lower().strip()
    if default_model not in VALID_MODELS:
        raise ValueError(
            f"DEFAULT_MODEL must be one of {VALID_MODELS}, got {default_model!r}"
        )

    return Config(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        admin_ids=_parse_admin_ids(os.environ["ADMIN_IDS"]),
        default_model=default_model,
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        anthropic_model=os.environ["ANTHROPIC_MODEL"],
        claude_max_tokens=int(os.environ["CLAUDE_MAX_TOKENS"]),
        ollama_host=os.environ["OLLAMA_HOST"],
        ollama_model=os.environ["OLLAMA_MODEL"],
        persona=_load_persona(ROOT_DIR / os.environ["PERSONA_FILE"]),
        db_path=ROOT_DIR / os.environ["DB_PATH"],
        history_max_turns=int(os.environ["HISTORY_MAX_TURNS"]),
        embedding_model=os.environ["EMBEDDING_MODEL"],
        rag_path=ROOT_DIR / os.environ["RAG_PATH"],
        rag_top_k=int(os.environ["RAG_TOP_K"]),
        rag_chunk_size=int(os.environ["RAG_CHUNK_SIZE"]),
        rag_chunk_overlap=int(os.environ["RAG_CHUNK_OVERLAP"]),
        rag_embed_num_ctx=int(os.environ["RAG_EMBED_NUM_CTX"]),
        rock_bottom_window=int(os.environ["ROCK_BOTTOM_WINDOW"]),
        rock_bottom_threshold=float(os.environ["ROCK_BOTTOM_THRESHOLD"]),
        rock_bottom_alert_low_msgs=int(os.environ["ROCK_BOTTOM_ALERT_LOW_MSGS"]),
    )
