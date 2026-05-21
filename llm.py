"""LLM providers: Claude (Anthropic API) and Ollama (local).

Both providers expose the same async API:

    complete(system, history, user_msg, on_tool=None) -> str

They share a tool-use loop: when the model asks to call a tool, the provider
executes it via `tools.run_tool`, feeds the result back, and re-asks the model
until it produces a plain assistant message (or until MAX_TOOL_ITER is hit).
History is a list of {"role": "user"|"assistant", "content": str} dicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Protocol

import anthropic
import ollama

from config import Config
from tools import TOOLS, run_tool, to_anthropic, to_ollama

log = logging.getLogger("llm")

Message = dict[str, str]
# Hook fired after each tool execution. Receives (tool_name, arguments, result).
OnTool = Callable[[str, dict, str], Awaitable[None] | None]

# Safety bound on the tool-use loop. A well-behaved model should converge in
# 1-2 iterations; this just prevents an infinite loop in pathological cases.
MAX_TOOL_ITER = 5


async def _emit_tool(on_tool: OnTool | None, name: str, args: dict, result: str) -> None:
    if on_tool is None:
        return
    out = on_tool(name, args, result)
    if asyncio.iscoroutine(out):
        await out


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        system: str,
        history: list[Message],
        user_msg: str,
        on_tool: OnTool | None = None,
    ) -> str: ...


class ClaudeProvider:
    name = "claude"

    def __init__(self, cfg: Config) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key)
        self._model = cfg.anthropic_model
        self._max_tokens = cfg.claude_max_tokens
        self._tools = to_anthropic(TOOLS)

    async def complete(
        self,
        system: str,
        history: list[Message],
        user_msg: str,
        on_tool: OnTool | None = None,
    ) -> str:
        # Messages must keep the structured content blocks returned by the API
        # across iterations of the tool loop, so we type this as `list[dict]`.
        messages: list[dict] = [*history, {"role": "user", "content": user_msg}]

        for _ in range(MAX_TOOL_ITER):
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                # Cache the system prompt: stable across turns, large enough to amortize.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=self._tools,
                messages=messages,
            )

            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text")

            # Append the assistant turn (containing tool_use blocks) verbatim,
            # then a user turn carrying the matching tool_result blocks.
            messages.append({"role": "assistant", "content": resp.content})
            tool_results: list[dict] = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                args = dict(block.input) if block.input else {}
                result = await run_tool(block.name, args)
                await _emit_tool(on_tool, block.name, args, result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({"role": "user", "content": tool_results})

        log.warning("Claude tool loop hit MAX_TOOL_ITER=%d", MAX_TOOL_ITER)
        return "Sorry, I couldn't complete the request."


class OllamaProvider:
    name = "ollama"

    def __init__(self, cfg: Config) -> None:
        # ollama python client is sync; we run calls in a worker thread.
        self._client = ollama.Client(host=cfg.ollama_host)
        self._model = cfg.ollama_model
        self._tools = to_ollama(TOOLS)

    async def complete(
        self,
        system: str,
        history: list[Message],
        user_msg: str,
        on_tool: OnTool | None = None,
    ) -> str:
        messages: list[dict] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": user_msg},
        ]

        for _ in range(MAX_TOOL_ITER):
            resp = await asyncio.to_thread(
                self._client.chat,
                model=self._model,
                messages=messages,
                tools=self._tools,
            )
            msg = resp["message"]
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                return msg.get("content") or ""

            # Record the assistant turn so the model sees its own tool_calls on
            # the next iteration, then append one "tool" message per call.
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or {}
                # Some Ollama backends serialize arguments as a JSON string;
                # parse here so the on_tool hook sees a real dict.
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        parsed_args = {}
                else:
                    parsed_args = dict(raw_args)
                result = await run_tool(name, parsed_args)
                await _emit_tool(on_tool, name, parsed_args, result)
                messages.append({"role": "tool", "name": name, "content": result})

        log.warning("Ollama tool loop hit MAX_TOOL_ITER=%d", MAX_TOOL_ITER)
        return "Sorry, I couldn't complete the request."


def build_providers(cfg: Config) -> dict[str, LLMProvider]:
    return {
        "claude": ClaudeProvider(cfg),
        "ollama": OllamaProvider(cfg),
    }
