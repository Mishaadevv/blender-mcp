# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.4.0] - 2026-09-26

### Fixed
- **The MCP server no longer crashes on startup.** `server.py` pulled the quality
  guidelines prompt through the Blender addon package, whose `__init__.py` imports
  `bpy`. Outside Blender that raised `ModuleNotFoundError: No module named 'bpy'`
  and the server could not start at all, so every tool was unreachable. The addon
  package now imports cleanly without `bpy` and registers the Blender-only parts
  lazily, and the server degrades gracefully if the prompt is ever unavailable
  again.
- `camera_focus` no longer resolves to the literal string `"None"`. An operator
  precedence mistake in the camera lookup meant a missing camera produced a
  truthy `"None"` that slipped past the guard and failed deep in the scene
  lookup. It now falls back to the active scene camera, accepts a name, an
  object or a `{"name": ...}` dict, and reports a clear error when there is no
  camera or no target.
- Version numbers are no longer inconsistent across `pyproject.toml`,
  `server.py` and the addon's `bl_info`.

### Added
- 13 tools for the v4.0-4.3 handlers, which existed in the addon but were never
  exposed and therefore could not be called by an agent: `blender_auto_uv`,
  `blender_fix_uv_mapping`, `blender_generate_lods`, `blender_fix_topology`,
  `blender_auto_validate`, `blender_quality_guidelines`,
  `blender_auto_light_scene`, `blender_camera_focus`,
  `blender_download_textures`, `blender_download_animations`,
  `blender_create_animation`, `blender_paint_texture` and
  `blender_list_installed_addons`. Tool count is now 122.
- 16 end-to-end checks covering the v4 automation surface, plus explicit
  failure cases for `camera_focus`. Selftest is now 142 checks.

## [4.3.0] - 2026-09-26

### Added
- `download_animations` and `create_animation` for motion work.
- `paint_texture` for writing image datablocks to disk.
- `fix_uv_mapping` and `list_installed_addons`.

## [4.2.0] - 2026-09-26

### Added
- Multi-library model search and download.

## [4.1.0] - 2026-09-26

### Added
- Quality guidelines module and the `auto_uv`, `generate_lods`,
  `auto_light_scene`, `camera_focus`, `fix_topology` and `auto_validate`
  handlers.

## [4.0.0] - 2026-09-26

Quality improvements for AI-generated 3D models: automatic UV unwrapping,
topology auto-repair, LOD generation, smart three-point lighting, camera
auto-focus, triplanar mapping, procedural normals, ambient occlusion, and
batch rollback.

### Added

- **Automatic UV unwrapping.** `blender_auto_uv` generates UVs for any mesh
  that lacks them (smart_project or cube_project). `blender_add_primitive` and
  `blender_create_mesh` now accept `auto_uv=true` (default) to make every new
  mesh texture-ready out of the box.
- **Topology auto-repair.** `blender_fix_topology` dissolves non-manifold edges,
  removes loose vertices/edges and zero-area faces, flips inverted normals on
  closed shells, and welds duplicate vertices. Run it after any mesh edit to
  guarantee manifold geometry.
- **LOD generation.** `blender_generate_lods` creates decimated variants of any
  mesh (default ratios 0.5, 0.25, 0.1) as separate objects with Decimate
  modifiers, ready for game engines.
- **Smart three-point lighting.** `blender_auto_light_scene` adds a key/fill/rim
  rig aimed at the scene centroid, with warm key, cool fill, and bright rim.
- **Camera auto-focus.** `blender_camera_focus` adds a Track To constraint so
  the camera always looks at a target object.
- **Triplanar mapping.** `blender_build_shader` now supports triplanar
  projection for distortion-free texturing without UVs.
- **Procedural normals.** `blender_procedural_material` can add micro-surface
  detail via noise-driven bump nodes.
- **Ambient occlusion.** Materials can now include AO nodes for contact
  shadows and depth cues.
- **Batch rollback.** `blender_batch` now supports `rollback=true` (default)
  to undo the last step when `stop_on_error` is set and a step fails.
- **Auto-validation.** `blender_auto_validate` runs a quick topology/normals/UV
  check after mesh edits. `blender_add_primitive` and `blender_create_mesh`
  accept `auto_validate=true` to validate immediately after creation.

## [3.0.0] - 2026-09-26

Full control over Blender: shader authoring, internet assets, add-on and package
management, complete animation, and project-wide inspection. 69 tools -> 109.

### Added

- **Material and shader authoring.** `blender_make_material` creates PBR
  materials from 24 physically-set presets (car paint gets metallic plus a clear
  coat, glass gets transmission and IOR 1.52, leather and fabric get high
  roughness), with per-socket overrides and optional assignment in one call.
  `blender_build_shader` builds an arbitrary node graph from a declarative
  description of nodes, sockets and links, reporting bad sockets per node rather
  than aborting. `blender_shader_info`, `blender_set_shader_input`,
  `blender_connect_shader`, `blender_procedural_material` (noise/voronoi/wave/
  checker driving colour, roughness and bump), `blender_world_shader` (colour,
  gradient, physical sky, HDRI), `blender_paint_vertex_colors` and
  `blender_material_report`.
- **Assets from the internet.** `blender_download` fetches a URL with a size cap
  and SHA-256 verification. `blender_search_library` and `blender_fetch_asset`
  reach CC0 HDRIs, textures and models on Poly Haven with no API key, and can set
  a fetched HDRI as the world in the same call. `blender_import_asset` and
  `blender_export_asset` handle fbx, obj, gltf, glb, stl, ply, usd, usdz, abc,
  dae and blend with sensible per-format defaults.
- **Add-ons and Python packages.** `blender_list_addons` and
  `blender_manage_addon` list, enable, disable and install add-ons;
  `blender_list_packages` and `blender_install_package` manage Python packages;
  `blender_append_node_group` reuses shader and geometry node groups from another
  .blend. Installs are refused without `confirm=true`, and pip targets a
  project-local directory rather than Blender's own site-packages.
- **Complete animation.** `blender_list_actions`, `blender_manage_action`,
  `blender_keyframe_channel` (any data path, including modifier levels, over a
  frame range), `blender_remove_keyframes`, `blender_curves` (interpolation,
  easing, handle types, noise/cyclic/limit modifiers, retiming, value scaling),
  `blender_nla`, `blender_drivers` with real typed variables, `blender_shape_keys`,
  `blender_simulate` (step, bake, reset), `blender_sequencer` and
  `blender_camera_move` (orbit turntable, follow constraint, dolly),
  `blender_timeline`.
- **Project-wide inspection.** `blender_settings_report` returns the scene, the
  full render configuration including Cycles and EEVEE, datablock counts,
  preferences, file paths, linked libraries and registered handlers.
  `blender_set_setting` writes one setting by dotted path and reports the old and
  new value. `blender_blend_contents` inventories every datablock with users and
  orphans, `blender_scripts_and_texts` finds embedded texts and script files,
  `blender_filesystem` browses files inside safe roots, `blender_python_env`
  reports the interpreter and module availability, `blender_render_report` shows
  what a render would actually use, and `blender_diagnose` gives a quick health
  report.

### Fixed

- `view3d_override` took a 3D viewport area from `bpy.data.screens` when no
  screen was in context, which raised "Area set with window & screen set to
  None". It now only ever pairs an area with the window and screen that own it.
- `bpy.context.selected_objects` does not exist in the bridge's timer context, so
  selection helpers now track state through the view layer.
- Shader and group sockets such as `NodeSocketShader` have no `default_value`;
  writing one raised. Reads and writes now go through helpers that tolerate them.
- Building a shader no longer leaves two Material Output nodes: the default one
  left by `use_nodes` is removed, which previously left the real output
  disconnected from the surface.
- `bpy.ops.object.keyframe_insert` and `driver_add` wanted -1 rather than None to
  mean "all components".
- Settings and preference reports are now version-tolerant: a field renamed
  between Blender releases yields null instead of aborting the whole report.
- Exporting to a `.glb` reported the format as `GLTF`; the binary container is
  now reported as `GLB`.

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
