# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-26

First public release.

### Added

- **46 MCP tools** over a live Blender 4.5 LTS session, on stdio transport.
- **Timer-driven socket bridge** (`blender_mcp_bridge` addon). No worker threads:
  the socket is polled from a `bpy.app.timers` callback so every command runs on
  the main thread with a fully populated `bpy.context`.
- **`blender_setup`** tool and `blender-mcp-setup` command: install the bundled
  addon into Blender's user addons directory and let Blender itself enable it,
  leaving other add-ons untouched.
- **Image feedback loop**: `blender_capture_viewport` and `blender_render` return
  the result as an MCP image content block, so the agent can review its own work.
- **Escape hatches**: `blender_execute_python`, `blender_run_operator`, and
  `blender_list_operators` / `blender_search_api` for introspecting the exact
  operators and properties of the connected Blender build.
- **~30 bmesh operators** through `blender_edit_mesh` (extrude, bevel, inset,
  subdivide, dissolve, bridge, spin, merge, normal recalculation, selection).
- Import/export for `.blend`, `.glb`, `.gltf`, `.fbx`, `.obj`, `.stl`, `.ply`,
  `.usd`, `.abc`.
- `BLENDER_MCP_AUTOLAUNCH` to start Blender automatically when none is running.
- `BLENDER_MCP_ALLOWED_ROOTS` to confine which paths the agent may write to.

### Notes

- A real Blender window is required. `blender --background` has no event loop,
  so the bridge timers never fire there.
- The loopback socket has no authentication by design. See the security section
  of the README.
