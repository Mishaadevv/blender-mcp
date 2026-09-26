"""Panel and operators for controlling the bridge from inside Blender."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel

from .bridge import SERVER, view3d_override

CATEGORY = "MCP"


class BRIDGE_OT_start(Operator):
    bl_idname = "bridge.start"
    bl_label = "Start MCP Bridge"
    bl_description = "Start the local socket server the MCP client connects to"
    bl_options = {"REGISTER"}

    host: StringProperty(name="Host", default="127.0.0.1")
    port: IntProperty(name="Port", default=9876, min=1024, max=65535)

    def execute(self, context):
        SERVER.host = self.host
        SERVER.port = int(self.port)
        try:
            SERVER.start()
        except OSError as exc:
            self.report({"ERROR"}, f"Could not bind {self.host}:{self.port} - {exc}")
            return {"CANCELLED"}
        context.preferences.addons["blender_mcp_bridge"].preferences.port = SERVER.port
        self.report({"INFO"}, f"MCP bridge listening on {SERVER.host}:{SERVER.port}")
        return {"FINISHED"}


class BRIDGE_OT_stop(Operator):
    bl_idname = "bridge.stop"
    bl_label = "Stop MCP Bridge"
    bl_description = "Stop the local socket server"
    bl_options = {"REGISTER"}

    def execute(self, context):
        SERVER.stop()
        self.report({"INFO"}, "MCP bridge stopped")
        return {"FINISHED"}


class BRIDGE_OT_test(Operator):
    bl_idname = "bridge.test"
    bl_label = "Ping Client"
    bl_description = "Check that an MCP client is connected right now"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if SERVER.is_running():
            self.report({"INFO"}, f"Listening on {SERVER.host}:{SERVER.port}")
        else:
            self.report({"WARNING"}, "Bridge is not running")
        return {"FINISHED"}


class BRIDGE_OT_viewport(Operator):
    bl_idname = "bridge.capture_viewport"
    bl_label = "Capture Viewport"
    bl_description = "Save an offscreen render of the 3D viewport to a PNG"
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH", default="//mcp_viewport.png")
    width: IntProperty(default=1280, min=64, max=8192)
    height: IntProperty(default=720, min=64, max=8192)

    def execute(self, context):
        from . import commands

        try:
            result = commands.cmd_capture(
                {"output_path": bpy.path.abspath(self.filepath),
                 "width": self.width, "height": self.height, "mode": "viewport"}
            )
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Saved viewport capture to {result['path']}")
        return {"FINISHED"}


class BRIDGE_PT_panel(Panel):
    bl_label = "MCP Bridge"
    bl_idname = "BRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY

    def draw(self, context):
        layout = self.layout
        prefs = context.preferences.addons.get("blender_mcp_bridge")
        running = SERVER.is_running()

        box = layout.box()
        row = box.row()
        row.alert = not running
        row.label(text=f"{'ONLINE' if running else 'OFFLINE'}  {SERVER.host}:{SERVER.port}")
        if running:
            box.operator("bridge.stop", icon="PAUSE")
        else:
            box.operator("bridge.start", icon="PLAY")
        if prefs:
            col = box.column(align=True)
            col.prop(prefs.preferences, "port")
            col.prop(prefs.preferences, "autostart")

        info = layout.box()
        info.label(text="Loopback only - no authentication", icon="LOCKED")
        info.label(text="Anyone with access to this socket can script Blender")
        layout.operator("bridge.capture_viewport", icon="RENDER_STILL")
        layout.operator("bridge.test", icon="OUTLINER_OB_GROUP_INSTANCE")


class BRIDGE_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = "blender_mcp_bridge"

    port: IntProperty(
        name="Port", default=9876, min=1024, max=65535,
        description="TCP port the bridge listens on (loopback only)",
    )
    autostart: BoolProperty(
        name="Start on load", default=True,
        description="Start the bridge automatically when Blender opens a file",
    )

    def draw(self, context):
        col = self.layout.column(align=True)
        col.prop(self, "port")
        col.prop(self, "autostart")


CLASSES = (
    BRIDGE_OT_start,
    BRIDGE_OT_stop,
    BRIDGE_OT_test,
    BRIDGE_OT_viewport,
    BRIDGE_PT_panel,
    BRIDGE_AddonPreferences,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if bpy.app.handlers.load_post is not None:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    if bpy.app.handlers.load_post is not None and _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    SERVER.stop()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


def _on_load_post(_dummy):
    try:
        prefs = bpy.context.preferences.addons["blender_mcp_bridge"].preferences
    except (KeyError, AttributeError):
        return
    if not getattr(prefs, "autostart", True) or SERVER.is_running():
        return
    import os

    SERVER.port = int(os.environ.get("BLENDER_MCP_PORT") or getattr(prefs, "port", SERVER.port))
    try:
        SERVER.start()
    except (OSError, RuntimeError) as exc:
        print(f"[MCP Bridge] could not start after load: {exc}")


__all__ = ["register", "unregister", "view3d_override"]
