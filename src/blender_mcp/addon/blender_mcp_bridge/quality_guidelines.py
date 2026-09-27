"""The agent-facing system prompt lives in `blender_mcp.prompts`.

This module is the addon's view of it. It used to carry a second, separately
edited copy of the same rules, which is how the Array property name drifted out
of date in one copy and not the other. Now it resolves the single shared text
and degrades to a short pointer if the server package is not importable from
inside Blender.
"""

from __future__ import annotations

_FALLBACK = (
    "The full modelling-quality guidelines ship with the MCP server package "
    "(blender_mcp/prompts.py) and are normally delivered to you in this "
    "server's startup instructions. They could not be imported from inside "
    "Blender, so ask for them again once the MCP server is running."
)


def _load() -> str:
    # Inside Blender the server package is not on sys.path; add the repository
    # layout next to the addon so the import resolves.
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    # .../src/blender_mcp/addon/blender_mcp_bridge/  ->  .../src
    src_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    if src_root not in sys.path:
        sys.path.append(src_root)
    try:
        from blender_mcp.prompts import SYSTEM_PROMPT
    except Exception:  # noqa: BLE001
        try:
            from src.blender_mcp.prompts import SYSTEM_PROMPT
        except Exception:  # noqa: BLE001
            return _FALLBACK
    return SYSTEM_PROMPT


QUALITY_SYSTEM_PROMPT = _load()
