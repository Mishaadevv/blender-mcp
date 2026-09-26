"""MCP Bridge - expose a running Blender instance to an MCP server over TCP.

Install: Edit > Preferences > Add-ons > Install..., pick the
``blender_mcp_bridge`` folder (or use scripts/install_addon.py).
"""

bl_info = {
    "name": "MCP Bridge",
    "author": "local",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > MCP",
    "description": "Loopback socket bridge so an MCP server can drive this Blender instance",
    "category": "System",
}

import os

import bpy

from .bridge import SERVER
from .commands import HANDLERS
from .ui import register as register_ui
from .ui import unregister as unregister_ui


def _autostart():
    try:
        prefs = bpy.context.preferences.addons["blender_mcp_bridge"].preferences
    except (KeyError, AttributeError):
        return
    if not getattr(prefs, "autostart", True):
        return
    SERVER.port = int(os.environ.get("BLENDER_MCP_PORT") or getattr(prefs, "port", SERVER.port))
    if SERVER.is_running():
        return
    try:
        SERVER.start()
    except (OSError, RuntimeError) as exc:
        print(f"[MCP Bridge] autostart failed: {exc}")


def register():
    SERVER.register_handlers(HANDLERS)
    register_ui()
    _autostart()
    if SERVER.is_running():
        print(f"[MCP Bridge] listening on {SERVER.host}:{SERVER.port}", flush=True)
    else:
        print(
            "[MCP Bridge] loaded but not listening. Start it from "
            "3D View > Sidebar > MCP.",
            flush=True,
        )


def unregister():
    unregister_ui()
    SERVER.stop()
