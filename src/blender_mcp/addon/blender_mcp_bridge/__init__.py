"""MCP Bridge - expose a running Blender instance to an MCP server over TCP.

Install: Edit > Preferences > Add-ons > Install..., pick the
``blender_mcp_bridge`` folder (or use scripts/install_addon.py).
"""

bl_info = {
    "name": "MCP Bridge",
    "author": "local",
    "version": (4, 5, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > MCP",
    "description": "Loopback socket bridge so an MCP server can drive this Blender instance",
    "category": "System",
}

import os

# ``bpy`` only exists inside Blender. The MCP server imports the pure-data
# ``quality_guidelines`` module from this same package, so a hard ``import bpy``
# here would stop the server from starting at all. Import the Blender-only parts
# lazily and fail loudly only when someone actually tries to register.
try:  # pragma: no cover - exercised inside Blender
    import bpy  # noqa: F401
    _IN_BLENDER = True
except ModuleNotFoundError:  # pragma: no cover - exercised outside Blender
    bpy = None
    _IN_BLENDER = False

if _IN_BLENDER:
    from .bridge import SERVER
    from .commands import HANDLERS
    from .ui import register as register_ui
    from .ui import unregister as unregister_ui
else:  # pragma: no cover
    SERVER = None
    HANDLERS: dict = {}
    register_ui = unregister_ui = None


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
    if not _IN_BLENDER:
        raise RuntimeError(
            "blender_mcp_bridge.register() only runs inside Blender; it needs bpy."
        )
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
    if not _IN_BLENDER:
        return
    unregister_ui()
    SERVER.stop()
