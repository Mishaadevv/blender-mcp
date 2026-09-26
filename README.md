<div align="center">

# Blender MCP

**Let an AI agent actually use your Blender.** Not a toy wrapper — a bridge into
a live Blender 4.5 LTS session: modelling, materials, lighting, cameras, and
renders handed back to the model as images it can look at.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## What it does

Point any MCP client at it and the agent can:

- **Audit** the model before and after it works: `blender_validate` returns a
  scored report across scale, real-world dimensions, normals, topology,
  intersections, symmetry, naming, materials, UVs, transforms, pivots, poly
  budget, LODs, lighting and orphan datablocks. `blender_find_problems` returns
  only the failures, each paired with the tool call that fixes it.
- **Measure** instead of guessing: bounding boxes, centre distances, assembly
  extents, and a full per-mesh census (manifold/boundary/wire edges, closed
  shell, signed volume, loose and duplicate geometry).
- **Survey** your open scene: objects, collections, materials, meshes, render settings
- **Model**: primitives, custom meshes, ~30 bmesh operators (extrude, bevel,
  inset, subdivide, bridge, spin, merge, recalc normals), plus mirror, array,
  screw, weld, remesh, decimate and wireframe
- **Auto-fix topology**: `blender_fix_topology` repairs non-manifold edges,
  flipped normals, loose geometry, and duplicate vertices in one call
- **Auto-UV**: every new mesh gets UVs automatically (`auto_uv=true` by default)
  via smart_project, so it is texture-ready immediately
- **LOD generation**: `blender_generate_lods` creates decimated variants for game
  engines with one call
- **Shade** with 30 non-destructive modifiers, applied or left live
- **Unwrap** with smart project, angle/conformal unwrap, island packing and
  welding, and report UV ranges before they become an atlas problem
- **Generate textures** procedurally as real PNG files: 20 patterns from fbm and
  voronoi to rust, brushed metal, leather, carbon weave and stencilled
  registration plates, plus matched BaseColor/Roughness/Metallic/Normal/AO sets
  that can be wired straight into a material. Bake procedural nodes to disk and
  pack everything into the .blend.
- **Rig and simulate**: armatures, bones, automatic weights, pose bones, rigid
  bodies, cloth, soft bodies, collision and force fields
- **Light and frame**: sun/area/spot lights, three-point setups, world gradients,
  physical Nishita sky, HDRI, cameras that aim themselves
- **Smart lighting**: `blender_auto_light_scene` adds a key/fill/rim rig aimed at
  the scene centroid; `blender_camera_focus` adds a Track To constraint so the
  camera always looks at a target
- **See its own work**: `blender_capture_viewport` and `blender_render` return
  the image as an MCP content block, so the agent reviews the render and iterates.
  `blender_render_extras` adds turntables, a near-instant Workbench clay preview,
  render passes and contact sheets
- **Round-trip** `.blend`, `.glb`, `.gltf`, `.fbx`, `.obj`, `.stl`, `.ply`, `.usd`, `.abc`
- **Batch safety**: `blender_batch` supports `rollback=true` (default) to undo
  the last step when `stop_on_error` is set and a step fails

Two things that matter on a big scene:

- **`blender_batch`** runs many commands in a single round trip, with per-step
  results. A forty-step build is one call instead of forty.
- **`blender_select_by`** finds objects by predicate - loose geometry, missing
  UVs, missing materials, too large, by collection, by glob - because names are
  what an agent gets wrong first.

Plus three escape hatches for everything else:
`blender_execute_python`, `blender_run_operator`, and
`blender_list_operators(include_properties=True)` — which reports the exact
operator ids and property names of the Blender build it is talking to, instead of
relying on the model's memory.

- **Author shaders**: 24 physically-set PBR presets (car paint with clear coat,
  glass with transmission, leather, brushed steel, neon), a declarative
  node-graph builder for anything the presets miss, one-call procedural
  surfaces, world shaders including physical sky and HDRI, and vertex-colour masks
- **Advanced shading**: triplanar mapping for distortion-free texturing without
  UVs, procedural micro-surface normals, and ambient occlusion for contact
  shadows
- **Pull assets from the internet**: download any URL with a size cap and
  checksum, or search and fetch CC0 HDRIs, textures and models from Poly Haven
  with no API key. Import fbx, obj, gltf, glb, stl, ply, usd, abc, dae and blend
  as separate editable objects, and export the same set back out
- **Animate**: actions, F-curve shaping (interpolation, easing, handle types,
  noise and cyclic modifiers, retiming), NLA tracks, drivers with real
  variables, shape keys, physics stepping and baking, the video sequencer, and
  camera orbits, follow constraints and dollies
- **Camera tools**: `blender_camera_focus` adds a Track To constraint so the
  camera always looks at a target object; `blender_auto_light_scene` adds a
  key/fill/rim rig aimed at the scene centroid
- **See and change everything**: a full settings report, single-setting writes by
  dotted path, a datablock inventory with orphans and linked libraries, script
  discovery, a sandboxed filesystem browser, the Python environment, a render
  report and a `blender_diagnose` health check
- **Quality workflow**: after any mesh edit, run `blender_fix_topology` to
  auto-repair non-manifold edges, flipped normals, and loose geometry, then
  `blender_auto_validate` to verify the result
- **Manage the environment**: list, enable and disable add-ons, install them from
  a zip, pip-install packages into a project-local directory, and append node
  groups from other .blend files
- **Auto-validation**: `blender_auto_validate` runs a quick topology/normals/UV
  check after mesh edits; `blender_add_primitive` and `blender_create_mesh` accept
  `auto_validate=true` to validate immediately after creation
  groups from another .blend

**109 tools.** See [Tools](#tools).

## Install

Requires **Python 3.11+** and **Blender 4.2+** (developed and tested against
4.5 LTS). No `git clone` needed if you use `uvx`:

```bash
uvx --from git+https://github.com/Mishaadevv/blender-mcp blender-mcp
```

or with pip:

```bash
pip install git+https://github.com/Mishaadevv/blender-mcp
```

### 1. Install the Blender addon

The addon ships inside the Python package, so one command does it:

```bash
blender-mcp-setup
```

It finds `blender.exe` (or `blender` on macOS/Linux), copies the addon into
Blender's user addons folder, then lets **Blender itself** enable it and save the
preference — so your other add-ons are left alone. Set `BLENDER_EXE` if Blender
lives somewhere unusual.

You can also skip this and let the agent do it: it will call `blender_setup` for
you when `blender_status` reports the addon is missing.

### 2. Point your client at it

<details open>
<summary><b>opencode</b> — <code>~/.config/opencode/opencode.jsonc</code></summary>

```jsonc
{
  "mcp": {
    "blender": {
      "type": "local",
      "command": ["uvx", "--from", "git+https://github.com/misakolot6-netizen/blender-mcp", "blender-mcp"],
      "enabled": true
    }
  }
}
```
</details>

<details>
<summary><b>Claude Code</b> — <code>.mcp.json</code> in your project</summary>

```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/misakolot6-netizen/blender-mcp", "blender-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>Claude Desktop</b> — <code>claude_desktop_config.json</code></summary>

```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/misakolot6-netizen/blender-mcp", "blender-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>Cline / Roo Code / Continue / any stdio client</b></summary>

Any client that spawns a stdio command works. Use the `uvx` line above, or if you
prefer a plain interpreter path:

```json
{
  "command": "/path/to/venv/bin/python",
  "args": ["-m", "blender_mcp"]
}
```
</details>

### 3. Open Blender

The bridge starts with Blender automatically. You should see
`3D View > Sidebar (N) > MCP Bridge` reporting `ONLINE`.

## How it works

```
your agent ──stdio──▶ blender-mcp (python) ──TCP 127.0.0.1:9876──▶ Blender
                       one request per          newline-delimited     bpy.app.timers
                       connection               JSON
```

The addon has **no worker threads at all**. `bpy` is only valid on Blender's main
thread, and from a background thread `bpy.context` is stripped down enough that
most operators fail. So the socket is polled from a `bpy.app.timers` callback
which accepts, reads and answers inline, on the main thread, with a fully
populated context.

Consequences worth knowing:

- Commands can never interleave with a render or a modal dialog — the timer
  simply does not run while the UI is busy, so a client gets a clean timeout
  instead of a corrupted file.
- **`blender --background` is not supported.** It has no event loop, so timers
  never fire. Use a normal window.
- Set `BLENDER_MCP_AUTOLAUNCH=1` and the server will start a Blender window
  itself if none is open.

## Tools

| Group | Tools |
|---|---|
| Setup & status | `blender_setup`, `blender_status` |
| **Validation** | `blender_validate`, `blender_find_problems`, `blender_analyze_mesh`, `blender_measure` |
| **Materials & shaders** | `blender_make_material`, `blender_procedural_material`, `blender_build_shader`, `blender_shader_info`, `blender_set_shader_input`, `blender_connect_shader`, `blender_world_shader`, `blender_paint_vertex_colors`, `blender_material_report` |
| **Assets from the net** | `blender_download`, `blender_import_asset`, `blender_export_asset`, `blender_list_libraries`, `blender_search_library`, `blender_fetch_asset` |
| **Add-ons & libraries** | `blender_list_addons`, `blender_manage_addon`, `blender_list_packages`, `blender_install_package`, `blender_append_node_group` |
| **Animation** | `blender_list_actions`, `blender_manage_action`, `blender_keyframe_channel`, `blender_remove_keyframes`, `blender_curves`, `blender_nla`, `blender_drivers`, `blender_shape_keys`, `blender_simulate`, `blender_sequencer`, `blender_camera_move`, `blender_timeline` |
| **Project inspection** | `blender_settings_report`, `blender_set_setting`, `blender_blend_contents`, `blender_scripts_and_texts`, `blender_filesystem`, `blender_python_env`, `blender_render_report`, `blender_diagnose` |
| **Textures** | `blender_generate_texture`, `blender_generate_pbr_set`, `blender_bake_texture`, `blender_pack_textures`, `blender_list_images` |
| **Context & history** | `blender_set_context`, `blender_select_by`, `blender_checkpoint`, `blender_undo`, `blender_redo` |
| **Geometry & UV** | `blender_geometry`, `blender_modifiers`, `blender_uv` |
| **Rig & physics** | `blender_rig`, `blender_pose`, `blender_physics` |
| **Scene & render** | `blender_scene_ops`, `blender_render_extras` |
| **Batching** | `blender_batch` |
| Scene | `blender_get_scene`, `blender_execute_python`, `blender_list_operators`, `blender_run_operator`, `blender_search_api` |
| Objects | `blender_list_objects`, `blender_get_object`, `blender_add_primitive`, `blender_create_mesh`, `blender_duplicate_objects`, `blender_delete_objects`, `blender_rename_object`, `blender_set_transform`, `blender_apply_transform`, `blender_select_objects`, `blender_join_objects`, `blender_parent_objects`, `blender_shade_smooth` |
| Mesh & modifiers | `blender_edit_mesh`, `blender_add_modifier`, `blender_apply_modifier` |
| Collections | `blender_create_collection`, `blender_assign_to_collection` |
| Materials | `blender_list_materials`, `blender_create_material`, `blender_assign_material`, `blender_set_material_node`, `blender_load_image_texture` |
| Camera & light | `blender_add_camera`, `blender_look_at`, `blender_set_active_camera`, `blender_add_light`, `blender_set_world` |
| Output | `blender_set_render_settings`, `blender_capture_viewport`, `blender_render` |
| Files | `blender_save_blend`, `blender_open_blend`, `blender_new_file`, `blender_import_model`, `blender_export_model` |
| Animation | `blender_set_frame`, `blender_insert_keyframe`, `blender_animation_info` |

Every tool takes `response_format: "markdown" | "json"`. In markdown mode object
lists come back as tables; in json mode you get the raw structure.

Tools that delete objects or overwrite files are annotated `destructiveHint`, so
clients that support it will ask the user first.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BLENDER_MCP_HOST` | `127.0.0.1` | bridge address |
| `BLENDER_MCP_PORT` | `9876` | bridge port |
| `BLENDER_MCP_TIMEOUT` | `300` | per-command timeout, seconds |
| `BLENDER_MCP_ROOT` | home directory | base for relative file paths |
| `BLENDER_MCP_ALLOWED_ROOTS` | — | `;`-separated roots; import/export/save outside them is refused |
| `BLENDER_MCP_AUTOLAUNCH` | — | `1` starts Blender if none is running |
| `BLENDER_EXE` | auto-detected | full path to the Blender executable |

## Security

The socket binds to `127.0.0.1` only and has **no authentication**: any process
on your machine can drive your Blender. That is a deliberate trade-off — the addon
exists precisely so an agent can control your Blender, and
`blender_execute_python` intentionally grants full Python access.

Practical measures:

- Never expose the port and never run the addon in a shared environment.
- Set `BLENDER_MCP_ALLOWED_ROOTS` to confine where the agent may write files.
- Only run it against Blender instances you are happy to have scripted.

## Development

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"

# 126 checks against a real Blender: geometry, bmesh, materials, render, export,
.venv/Scripts/python scripts/selftest.py

#   plus validation, textures, context, geometry, UV, batching and undo safety
.venv/Scripts/python scripts/mcp_test.py
```

`selftest.py` launches its own Blender if none is listening. Changes to the addon
only take effect after reinstalling it and restarting Blender:

```bash
.venv/Scripts/python -m blender_mcp.setup
```

Layout:

```
src/blender_mcp/
├── server.py       46 MCP tools
├── client.py       socket client, Blender discovery, autolaunch
├── setup.py        addon installer (also the blender-mcp-setup command)
├── formatting.py   markdown/json rendering, MCP image blocks
└── addon/blender_mcp_bridge/
    ├── bridge.py   timer-driven socket transport
    ├── commands.py the bpy/bmesh command layer
    └── ui.py       sidebar panel and operators
```

## Known limitations

- Add-on installs and `pip install` both execute third-party code, so they are
  **refused unless you pass `confirm=true`**. Packages go to a project-local
  directory, never into Blender's bundled `site-packages`.
- `blender_filesystem` is confined to your home directory, Blender's script
  folders and the asset cache. `allow_anywhere` drops that, and should only be
  used when you have actually asked for it.
- `blender_undo` refuses to rewind across an Open File, because Blender's undo
  stack does not survive a file load and attempting it crashes Blender. For the
  same reason `blender_file_op` with `action="new"` can block: `wm.open_mainfile`
  and `wm.read_homefile` driven from the bridge's timer may not return.
- `blender_download` only accepts http and https, caps the size, and never
  executes what it fetches. Poly Haven is wired up because it is CC0 and needs no
  API key; sources that need credentials are deliberately not included.
- The undo guard described above is not covered by the automated suite, because
  provoking it requires a file load.
- `blender_render` on Cycles at 1080p can take minutes; the timeout is raised to
  3600 s for renders, but prefer `eevee` while iterating.
- `blender_capture_viewport` rasterises the current workspace, so it needs a 3D
  viewport on screen. Switch to Layout, or use `mode="camera"`.
- `blender_new_file` / `blender_open_blend` rebuild the bpy context; if one of
  them is interrupted the addon may need a manual restart.
- Add-ons that change the evaluation pipeline (Geometry Nodes generators) can
  behave differently, since they own their own logic.

## License

MIT — see [LICENSE](LICENSE).
