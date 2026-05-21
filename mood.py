"""Mood scoring: classify user mood on a 0..10 scale via the active LLM.

The bot calls `MoodScorer.score()` after each user turn and persists the result
through `Database.add_mood`. A separate routine in `bot.py` computes a rolling
average over the last `MOOD_WINDOW` scores and triggers an admin alert when it
drops below `MOOD_THRESHOLD`.

This module deliberately does not reuse `LLMProvider.complete()` because that
path enables tool use and replays the full chat history, neither of which is
appropriate for a single-message classification task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import anthropic
import ollama

from config import Config

log = logging.getLogger("mood")

# Italian scoring rubric -- persona.yaml expects Italian conversation, so the
# scorer is briefed in the same language to stay aligned with user messages.
_SCORING_SYSTEM = (
    "Classifichi lo stato d'animo espresso in un messaggio utente "
    "su una scala numerica da 0 a 10.\n"
    "0 = grave sofferenza emotiva (disperazione, autolesionismo, "
    "ideazione suicidaria).\n"
    "3 = tristezza, ansia o frustrazione marcate.\n"
    "5 = neutro o ambiguo.\n"
    "7 = sereno, leggermente positivo.\n"
    "10 = chiaramente positivo, gioioso, entusiasta.\n"
    "Rispondi SOLO con JSON valido nella forma {\"score\": <numero>} "
    "senza alcun altro testo, spiegazione o markdown."
)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _clamp(score: float) -> float:
    if score < 0:
        return 0.0
    if score > 10:
        return 10.0
    return score


def _parse_score(raw: str) -> float | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "score" in obj:
            return _clamp(float(obj["score"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # Fallback: pick the first numeric token in the response.
    m = _NUM_RE.search(raw)
    if m is None:
        return None
    try:
        return _clamp(float(m.group(0)))
    except ValueError:
        return None


class MoodScorer:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._claude = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
        self._ollama = ollama.Client(host=cfg.ollama_host)

    async def score(self, model_name: str, user_msg: str) -> float | None:
        """Return a 0..10 mood score for `user_msg`, or None on failure."""
        if not user_msg.strip():
            return None
        try:
            if model_name == "claude":
                raw = await self._score_claude(user_msg)
            else:
                raw = await self._score_ollama(user_msg)
        except Exception:
            log.exception("Mood scoring failed via %s", model_name)
            return None
        return _parse_score(raw)

    async def _score_claude(self, user_msg: str) -> str:
        resp = await self._claude.messages.create(
            model=self._cfg.anthropic_model,
            max_tokens=32,
            system=_SCORING_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    async def _score_ollama(self, user_msg: str) -> str:
        resp = await asyncio.to_thread(
            self._ollama.chat,
            model=self._cfg.ollama_model,
            messages=[
                {"role": "system", "content": _SCORING_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            format="json",
        )
        return resp["message"].get("content") or ""
