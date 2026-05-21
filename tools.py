"""Provider-agnostic tool layer.

Each tool is declared once as a `ToolSpec` (name, description, JSON-Schema
parameters, async handler). Provider-specific adapters convert the spec into
the shape required by Anthropic / Ollama tool calling. A single `run_tool()`
entry point dispatches a call by name and returns a string result that can be
fed back to any LLM as a tool result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ddgs import DDGS

log = logging.getLogger("tools")

ToolHandler = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema for the input arguments
    handler: ToolHandler


# ---- search_web -------------------------------------------------------------

_SEARCH_WEB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "The web search query in the user's language.",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results to return (1-10). Default 5.",
            "minimum": 1,
            "maximum": 10,
        },
    },
    "required": ["query"],
}


async def _search_web(query: str, max_results: int = 5) -> str:
    """DuckDuckGo text search via the `ddgs` library.

    Runs in a worker thread because ddgs is synchronous. Returns a plain-text
    digest (title, url, snippet) suitable to hand back to an LLM as tool output.
    """
    max_results = max(1, min(int(max_results or 5), 10))

    def _run() -> list[dict]:
        # `with` ensures the underlying HTTP session is closed promptly.
        with DDGS() as d:
            return list(d.text(query, max_results=max_results))

    try:
        results = await asyncio.to_thread(_run)
    except Exception as exc:
        log.exception("search_web failed: %s", exc)
        return f"Search failed: {type(exc).__name__}: {exc}"

    if not results:
        return f"No results for query: {query!r}"

    lines = [f"Web search results for: {query}", ""]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(no title)"
        url = r.get("href") or r.get("url") or ""
        snippet = (r.get("body") or "").strip()
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


# ---- Registry ---------------------------------------------------------------

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="search_web",
        description=(
            "Search the web for fresh or factual information (news, recent events, "
            "facts that may have changed). Use only when the answer is not common "
            "knowledge or may be out of date. Returns a list of titles, URLs and "
            "snippets."
        ),
        parameters=_SEARCH_WEB_SCHEMA,
        handler=_search_web,
    ),
]

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


async def run_tool(name: str, arguments: dict | str | None) -> str:
    """Dispatch a tool call by name.

    `arguments` may arrive as a dict (Anthropic) or as a JSON string (some
    Ollama models). Unknown tools and bad JSON both return a non-empty error
    string so the LLM can recover gracefully.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return f"Invalid JSON arguments for {name}: {exc}"
    arguments = arguments or {}

    spec = _BY_NAME.get(name)
    if spec is None:
        return f"Unknown tool: {name}"

    try:
        return await spec.handler(**arguments)
    except TypeError as exc:
        return f"Bad arguments for {name}: {exc}"
    except Exception as exc:
        log.exception("Tool %s crashed: %s", name, exc)
        return f"Tool {name} failed: {type(exc).__name__}: {exc}"


# ---- Provider adapters ------------------------------------------------------

def to_anthropic(specs: list[ToolSpec]) -> list[dict]:
    """Adapt tool specs to Anthropic Messages API format."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "input_schema": s.parameters,
        }
        for s in specs
    ]


def to_ollama(specs: list[ToolSpec]) -> list[dict]:
    """Adapt tool specs to Ollama / OpenAI-style function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in specs
    ]
