# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-09-26

Model validation, procedural texture generation and batching. 46 tools -> 69.

### Added

- **Model validation.** `blender_validate` audits scale, real-world dimensions
  against a supplied spec, normals, topology, intersections, symmetry, naming,
  materials, UVs, transforms, pivots, poly budget, LODs, lighting and orphan
  datablocks into a scored report. `blender_find_problems` returns only the
  failures, each paired with the tool call that fixes it. A check that itself
  crashes is reported as an `error` entry rather than aborting the audit, so a
  partial report is always better than a traceback.
- **Mesh analysis and measurement.** `blender_analyze_mesh` gives a full shell
  census: manifold / boundary / wire edge counts, whether the shell is closed,
  signed volume, surface area, min/max/zero-area faces, loose verts and edges,
  duplicate vertices within 1e-5, UV layers, vertex groups, shape keys and the
  modifier stack. `blender_measure` covers bounding boxes, centre-to-centre
  distance and assembly extents.
- **Procedural texture generation** writing real PNG files.
  `blender_generate_texture` covers 20 patterns: solid, noise, fbm, voronoi,
  checker, grid, stripes, gradient, radial, brushed_metal, rust, scratches,
  grunge, concrete, asphalt, wood, leather, carbon, hazard and plate (with a
  built-in 5x7 font for stencilled registration plates).
  `blender_generate_pbr_set` emits a matched
  BaseColor/Roughness/Metallic/Normal/AO set from a single seed and can wire
  the maps into an existing Principled BSDF, including an AO multiply onto Base
  Color. Plus `blender_bake_texture`, `blender_pack_textures` and
  `blender_list_images`.
- **Batching.** `blender_batch` runs many commands in a single round trip with
  per-step results, so a forty-step build is one call instead of forty. One
  failing step does not hide the steps that worked; `stop_on_error` is opt-in.
- **Context and predicate selection.** `blender_set_context` sets mode, active
  object, selection and active collection atomically, so an operator never runs
  against the wrong selection. `blender_select_by` finds objects by loose
  geometry, missing UVs, missing materials, size, collection, type or glob
  instead of by name.
- **Geometry, UV, rig and physics.** `blender_geometry` (mirror, array,
  solidify, screw, spin, weld, recalc/flip normals, triangulate, decimate,
  remesh, wireframe, shrink/fatten, bevel-all), `blender_modifiers`
  (list/mute/move/remove/apply), `blender_uv` (report, smart project, unwrap,
  pack islands, weld, scale, centre), `blender_rig` (armature, bones, automatic
  weights), `blender_pose`, `blender_physics` (rigid body, cloth, soft body,
  collision, force fields, bake) and `blender_scene_ops` (duplicate hierarchy,
  instance/realise collections, purge orphans, depsgraph, add-ons, memory).
- **Render extras.** `blender_render_extras` adds turntables, a near-instant
  Workbench clay preview, render pass toggles and contact sheets assembled from
  existing images.

### Fixed

- `blender_undo` now refuses to rewind across an Open File. Blender's undo stack
  does not survive a file load; rewinding past one restores handles into freed
  datablocks and crashes the process instead of raising. Undo also reports its
  window-context requirement as an actionable error rather than a bare
  `RuntimeError: context is incorrect`.

### Known limits

- `blender_undo` cannot be provoked from the automated test suite, because doing
  so needs a file load and `wm.open_mainfile` / `wm.read_homefile` driven from
  the bridge timer can block Blender indefinitely. The guard is exercised
  manually instead.
- `blender_file_op` with `action="new"` carries the same risk; prefer building a
  fresh scene with `blender_new_file` from a clean Blender, or delete objects
  individually.

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
