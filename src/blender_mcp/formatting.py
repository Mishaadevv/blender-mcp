"""Response rendering helpers (markdown / json) for the Blender MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Image
from mcp.types import CallToolResult, TextContent

MAX_TABLE_ROWS = 60
MAX_LIST_ITEMS = 40
MAX_DEPTH = 6


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _fmt_scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt_scalar(v) for v in value) if len(value) <= 8 else f"[{len(value)} values]"
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "..."


def _is_table(rows: list) -> bool:
    if not rows or not all(isinstance(r, dict) for r in rows):
        return False
    keys: list[str] = []
    for row in rows:
        for key, value in row.items():
            if not _scalar(value) and not (isinstance(value, (list, tuple)) and all(_scalar(v) for v in value)):
                return False
            if key not in keys:
                keys.append(key)
    return 0 < len(keys) <= 8


def _table(rows: list) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    out = ["| " + " | ".join(keys) + " |",
           "| " + " | ".join("---" for _ in keys) + " |"]
    for row in rows[:MAX_TABLE_ROWS]:
        out.append("| " + " | ".join(_fmt_scalar(row.get(k)) for k in keys) + " |")
    if len(rows) > MAX_TABLE_ROWS:
        out.append(f"| ... {len(rows) - MAX_TABLE_ROWS} more rows |" + " |" * (len(keys) - 1))
    return out


def _render(value: Any, indent: int = 0, depth: int = 0) -> list[str]:
    pad = "  " * indent
    if depth > MAX_DEPTH:
        return [f"{pad}..."]
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            if isinstance(item, dict) and item:
                out.append(f"{pad}- **{key}**:")
                out.extend(_render(item, indent + 1, depth + 1))
            elif isinstance(item, list) and item and _is_table(item):
                out.append(f"{pad}- **{key}**:")
                out.extend("  " * (indent + 1) + line for line in _table(item))
            elif isinstance(item, list) and item:
                out.append(f"{pad}- **{key}**: " + ", ".join(_fmt_scalar(v) for v in item[:MAX_LIST_ITEMS]))
            else:
                out.append(f"{pad}- **{key}**: {_fmt_scalar(item)}")
        return out
    if isinstance(value, list):
        if _is_table(value):
            return _table(value)
        if all(_scalar(v) for v in value):
            return [f"{pad}" + ", ".join(_fmt_scalar(v) for v in value[:MAX_LIST_ITEMS])]
        out = []
        for item in value[:MAX_LIST_ITEMS]:
            if isinstance(item, dict):
                out.append(f"{pad}- " + ", ".join(f"{k}={_fmt_scalar(v)}" for k, v in item.items()))
            else:
                out.append(f"{pad}- {item}")
        if len(value) > MAX_LIST_ITEMS:
            out.append(f"{pad}- ... {len(value) - MAX_LIST_ITEMS} more")
        return out
    return [f"{pad}{_fmt_scalar(value)}"]


def to_markdown(data: Any, title: str | None = None) -> str:
    lines = [f"# {title}"] if title else []
    lines.extend(_render(data))
    return "\n".join(lines)


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def respond(data: Any, response_format: str = "markdown", title: str | None = None,
            image_path: str | None = None, note: str | None = None) -> CallToolResult:
    """Build a tool result, optionally attaching a rendered image."""
    if response_format == "json":
        text = to_json(data)
    else:
        text = to_markdown(data, title)
    if note:
        text = f"{text}\n\n{note}"
    content: list = [TextContent(type="text", text=text)]
    if image_path:
        content.append(Image(path=image_path))
    return CallToolResult(content=content, structured_content=None)


def failure(message: str, hint: str | None = None) -> CallToolResult:
    text = f"Error: {message}"
    if hint:
        text += f"\n\n{hint}"
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
