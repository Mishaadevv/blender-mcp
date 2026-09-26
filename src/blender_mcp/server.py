"""Blender MCP server - exposes a live Blender 4.5 instance to AI agents.

Requires the `blender_mcp_bridge` addon inside Blender (see README).
"""

from __future__ import annotations

import functools
import json
import os
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer, Image
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import BRIDGE, BlenderError
from .formatting import failure, respond

INSTRUCTIONS = """
Control a running Blender 4.5 LTS instance over its local MCP bridge.

Start here:
1. `blender_status`  - confirm the bridge is online, note the open .blend file.
   If it says not connected, call `blender_setup` ONCE, then ask the user to
   restart Blender (a --background Blender has no event loop and cannot work).
2. `blender_get_scene` - survey what already exists before touching it.
3. `blender_find_problems` - audit the model you are about to work on, or the
   one you just built. Every finding comes with the tool call that fixes it.
4. `blender_validate` - the full scored report (scale, dimensions vs a real
   spec, normals, topology, intersections, symmetry, naming, materials, UVs,
   transforms, pivots, budget, LODs, lighting, orphans).
5. Build with `blender_add_primitive` / `blender_create_mesh` /
   `blender_edit_mesh` / `blender_geometry` / `blender_add_modifier` /
   `blender_create_material` / `blender_generate_pbr_set`.
6. `blender_look_at` + `blender_set_render_settings` + `blender_render`, or
   `blender_capture_viewport` for a fast visual check, or
   `blender_render_extras` action='clay' for a near-instant preview.
7. `blender_save_blend` to persist.

Speed:
- `blender_batch` runs many commands in ONE round trip. Use it to build
  anything repetitive; a 40-step scene is one call instead of forty.
- `blender_select_by` finds objects by predicate (loose geometry, no UVs,
  too large, by material) instead of by name, which is what you want on a
  scene with hundreds of objects.
- `blender_set_context` sets active object, selection, collection and mode
  atomically, so an operator never runs against the wrong selection.
- `blender_checkpoint` / `blender_undo` / `blender_redo` make experimentation
  safe.

Escape hatches when no dedicated tool fits:
- `blender_execute_python` runs arbitrary Python on Blender's main thread
  (bpy, bmesh, mathutils, math, json, os are pre-imported).
- `blender_run_operator` invokes any bpy.ops operator.
- `blender_list_operators` / `blender_search_api` discover the exact
  operator names and properties available in this Blender build.

Always look at the result of a change before the next step - `blender_capture_viewport`
returns an image you can actually see. Units are Blender units (1.0 = 1 m).
Coordinates are Z-up, rotation is in degrees.
""".strip()

mcp = MCPServer(
    name="blender",
    title="Blender 4.5 LTS",
    version="3.0.0",
    instructions=INSTRUCTIONS,
)

FORMAT = Annotated[
    Literal["markdown", "json"],
    Field(description="'markdown' for readable output, 'json' for raw structured data."),
]

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
SLOW = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                       open_world_hint=True)


def _revive_nested_json(value, depth: int = 0):
    """Some MCP clients stringify nested objects such as ``parameters``.

    Blender then reports confusing type errors like "expected a float type,
    not str". Only values *inside* a dict or list are touched, so top level
    string arguments (paths, Python code, operator ids) are never altered.
    """
    if depth and isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return _revive_nested_json(json.loads(text), depth)
            except ValueError:
                return value
        return value
    if isinstance(value, dict):
        return {key: _revive_nested_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_revive_nested_json(item, depth + 1) for item in value]
    return value


def call(command: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict:
    return BRIDGE.call(command, _revive_nested_json(params or {}), timeout=timeout)


def with_image(data: dict, response_format: str, title: str, key: str = "path"):
    """Render a result and attach the produced image as an MCP content block."""
    result = respond(data, response_format, title=title)
    path = data.get(key)
    if path and os.path.isfile(path):
        result.content.append(Image(path=path).to_image_content())
    return result


def guard(function):
    """Turn transport/blender errors into is_error tool results.

    ``functools.wraps`` matters: the SDK derives each tool's JSON schema from
    the decorated function's signature and type hints.
    """
    from .formatting import failure

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BlenderError as exc:
            return failure(str(exc))

    return wrapper


# =========================================================================== #
# connection & scene
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_setup(enable_addon: bool = True, response_format: FORMAT = "markdown") -> Any:
    """Install the bundled Blender addon and enable it, so the bridge can start.

    Call this ONCE on a machine where `blender_status` reports not connected.
    It copies the addon into Blender's user addons directory and then lets
    Blender itself enable it and save the preference, leaving any other add-ons
    the user has enabled untouched.

    Afterwards the user must restart Blender (or launch it if it is closed) -
    the bridge starts automatically with the addon.
    """
    from .setup import SetupError, addon_installed, blender_executable, install_addon

    executable = blender_executable()
    if not executable:
        return failure(
            "Could not find Blender. Set the BLENDER_EXE environment variable to "
            "the full path of your Blender executable, then call this again."
        )
    if addon_installed(executable) and not enable_addon:
        from .setup import user_addons_dir

        return respond(
            {"blender": executable,
             "addons_directory": str(user_addons_dir(executable) / "blender_mcp_bridge"),
             "already_installed": True,
             "next_step": "Restart Blender so the bridge starts."},
            response_format, title="Blender Addon")
    try:
        report = install_addon(executable, enable=enable_addon)
    except SetupError as exc:
        return failure(str(exc))
    report["next_step"] = (
        "Ask the user to restart Blender. The bridge listens on "
        f"{BRIDGE.host}:{BRIDGE.port} automatically once it is open."
    )
    return respond(report, response_format, title="Blender Addon Installed")


@mcp.tool(annotations=READ)
@guard
def blender_status(response_format: FORMAT = "markdown") -> Any:
    """Check whether the Blender bridge is reachable and what it is running.

    Start with this. If `connected` is false, either the addon is not installed
    yet (call `blender_setup` once) or Blender is not running. Set
    BLENDER_MCP_AUTOLAUNCH=1 to let the server start Blender itself.
    """
    launch_note = None
    if not BRIDGE.is_available():
        try:
            BRIDGE.maybe_launch_instance()
        except BlenderError as exc:
            launch_note = str(exc)
    status = BRIDGE.status()
    if launch_note:
        status["autolaunch_failed"] = launch_note
    if not status.get("connected"):
        from .setup import addon_installed, blender_executable

        executable = blender_executable()
        status["blender_executable_found"] = executable
        if executable:
            status["addon_installed"] = addon_installed(executable)
            if not status["addon_installed"]:
                status["hint"] = (
                    "The MCP Bridge addon is not installed yet. Call blender_setup "
                    "once, then ask the user to restart Blender."
                )
    return respond(status, response_format, title="Blender Bridge Status")


@mcp.tool(annotations=READ)
@guard
def blender_get_scene(detailed: bool = False, response_format: FORMAT = "markdown") -> Any:
    """Get an overview of the open scene: objects, collections, materials, meshes,
    images, render settings, frame range and the active object.

    Args:
        detailed: Include per-object modifiers, constraints, light and camera data.
    """
    data = call("get_scene", {"detailed": detailed})
    return respond(data, response_format, title=f"Scene: {data.get('name')}")


@mcp.tool(annotations=READ)
@guard
def blender_execute_python(
    code: Annotated[str, Field(description="Python source. A single expression returns its value; "
                                           "statements run as a script. Runs on Blender's main thread.")],
    capture_output: bool = True,
    response_format: FORMAT = "markdown",
) -> Any:
    """Execute arbitrary Python inside Blender and return stdout, the expression
    value, or the traceback.

    This is the general escape hatch: use it for anything the dedicated tools do
    not cover (node graphs, drivers, custom rigging, numpy-style math, bmesh
    work). Available names: bpy, bmesh, math, mathutils (Vector/Euler/Matrix/
    Quaternion), json, os, sys, random, time, view3d (a context manager that
    injects a 3D viewport area so bpy.ops viewport calls work).

    Example:
        ```python
        import bmesh
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=2)
        me = bpy.data.meshes.new("C")
        bm.to_mesh(me)
        ob = bpy.data.objects.new("C", me)
        bpy.context.scene.collection.objects.link(ob)
        Result(ob.name)
        ```
    """
    data = call(
        "execute",
        {"code": code, "context": {"stdout": "capture" if capture_output else "none"}},
        timeout=900.0,
    )
    if data.get("error"):
        return respond(data, response_format, title="Python Execution Failed")
    return respond(data, response_format, title="Python Execution Result")


@mcp.tool(annotations=READ)
@guard
def blender_list_operators(
    query: str = "",
    category: str = "",
    include_properties: bool = False,
    limit: int = 100,
    response_format: FORMAT = "json",
) -> Any:
    """Discover the bpy.ops operators available in this Blender build, with
    their descriptions and (optionally) their exact property names, types,
    enum options and defaults.

    Use this to find the right operator before calling blender_run_operator.
    """
    data = call("list_operators", {
        "query": query, "category": category,
        "include_properties": include_properties, "limit": limit,
    })
    return respond(data, response_format, title="Blender Operators")


@mcp.tool(annotations=WRITE)
@guard
def blender_run_operator(
    operator: Annotated[str, Field(description="Operator id such as 'mesh.primitive_torus_add' or "
                                               "'object.shade_smooth'.")],
    properties: Annotated[dict | None, Field(description="Operator keyword arguments, e.g. "
                                                          "{'major_segments': 32}. Unknown keys are ignored.")] = None,
    area_type: str = "",
    response_format: FORMAT = "json",
) -> Any:
    """Run any bpy.ops operator by name with automatic 3D viewport context
    fallback and validation of the property names.

    Call blender_list_operators first if you are unsure of the exact id or
    property names.
    """
    data = call("run_operator", {
        "operator": operator, "properties": properties or {},
        "area_type": area_type or None,
    })
    return respond(data, response_format, title=f"Operator: {operator}")


@mcp.tool(annotations=READ)
@guard
def blender_search_api(query: str, limit: int = 40, response_format: FORMAT = "json") -> Any:
    """Search Blender's operator namespace by keyword. Faster than
    blender_list_operators when you only know part of a name."""
    data = call("search_api", {"query": query, "limit": limit})
    return respond(data, response_format, title=f"API search: {query}")


# =========================================================================== #
# objects
# =========================================================================== #


@mcp.tool(annotations=READ)
@guard
def blender_list_objects(
    collection: str = "",
    type: str = "",
    name_pattern: str = "",
    parent: str = "",
    detailed: bool = False,
    limit: int = 200,
    offset: int = 0,
    response_format: FORMAT = "markdown",
) -> Any:
    """List objects in the scene with transforms, dimensions, materials and
    modifiers. Supports filtering and offset/limit pagination.

    Args:
        collection: Only objects in this collection (includes children).
        type: Comma separated Blender types, e.g. 'MESH,LIGHT,CAMERA'.
        name_pattern: Regular expression matched against the object name.
        parent: A parent object name, or 'none' for top-level objects.
        detailed: Also include world matrix, shape keys and vertex groups.
    """
    data = call("list_objects", {
        "collection": collection or None, "type": type or None,
        "name_pattern": name_pattern or None, "parent": parent or None,
        "detailed": detailed, "limit": limit, "offset": offset,
    })
    return respond(data, response_format, title="Objects")


@mcp.tool(annotations=READ)
@guard
def blender_get_object(name: str, response_format: FORMAT = "json") -> Any:
    """Get everything known about one object: transforms, hierarchy, materials,
    modifiers, constraints, mesh statistics, custom properties and world matrix."""
    return respond(call("get_object", {"name": name}), response_format, title=f"Object: {name}")


@mcp.tool(annotations=WRITE)
@guard
def blender_add_primitive(
    type: Annotated[str, Field(description="cube, uv_sphere, ico_sphere, cylinder, cone, plane, "
                                            "circle, grid, torus or monkey.")] = "cube",
    name: str = "",
    location: Annotated[list[float] | None, Field(description="[x, y, z]")] = None,
    rotation: Annotated[list[float] | None, Field(description="[x, y, z] in DEGREES")] = None,
    scale: Annotated[list[float] | None, Field(description="[x, y, z]")] = None,
    parameters: Annotated[dict | None, Field(description="Operator specifics, e.g. "
                                                          "{'major_radius': 1, 'major_segments': 48} for a torus.")] = None,
    collection: str = "",
    material: str = "",
    shade_smooth: bool = False,
    response_format: FORMAT = "json",
) -> Any:
    """Add a mesh primitive to the scene and return its full description.

    Example: a smooth torus at the origin ->
    `type='torus', parameters={'major_radius': 1, 'minor_radius': 0.25, 'major_segments': 48, 'minor_segments': 16}, shade_smooth=True`
    """
    data = call("add_primitive", {
        "type": type, "name": name or None,
        "location": location, "rotation": rotation, "scale": scale,
        "parameters": parameters, "collection": collection or None,
        "material": material or None, "shade_smooth": shade_smooth,
    })
    return respond(data, response_format, title=f"Created {data.get('name')}")


@mcp.tool(annotations=WRITE)
@guard
def blender_create_mesh(
    name: str,
    vertices: list[list[float]],
    faces: list[list[int]],
    location: list[float] | None = None,
    collection: str = "",
    material: str = "",
    response_format: FORMAT = "json",
) -> Any:
    """Create a mesh object from explicit vertex and face data.

    `faces` entries are zero-based vertex index loops; a face with 3 indices
    is a triangle, 4 a quad.
    """
    data = call("create_mesh", {
        "name": name, "vertices": vertices, "faces": faces,
        "location": location, "collection": collection or None, "material": material or None,
    })
    return respond(data, response_format, title=f"Created mesh: {name}")


@mcp.tool(annotations=WRITE)
@guard
def blender_duplicate_objects(
    objects: list[str], collection: str = "", response_format: FORMAT = "json"
) -> Any:
    """Duplicate objects together with their mesh data (an independent copy)."""
    data = call("duplicate_objects", {"objects": objects, "collection": collection or None})
    return respond(data, response_format, title="Duplicated")


@mcp.tool(annotations=DESTRUCTIVE)
@guard
def blender_delete_objects(objects: list[str], response_format: FORMAT = "json") -> Any:
    """Permanently delete objects from the .blend file. This cannot be undone
    from the MCP server - save first if the scene matters."""
    return respond(call("delete_objects", {"objects": objects}), response_format,
                   title="Deleted")


@mcp.tool(annotations=WRITE)
@guard
def blender_rename_object(
    object: str, name: str, rename_data: bool = True, response_format: FORMAT = "json"
) -> Any:
    """Rename an object and, by default, its mesh/light/camera data block too."""
    return respond(call("rename_object", {"object": object, "name": name,
                                          "rename_data": rename_data}),
                   response_format, title="Renamed")


@mcp.tool(annotations=WRITE)
@guard
def blender_set_transform(
    objects: list[str],
    location: Annotated[list[float] | None, Field(description="Absolute [x, y, z]")] = None,
    rotation: Annotated[list[float] | None, Field(description="Absolute [x, y, z] in DEGREES")] = None,
    scale: Annotated[list[float] | None, Field(description="Absolute [x, y, z]")] = None,
    relative: bool = False,
    rotation_mode: str = "",
    response_format: FORMAT = "json",
) -> Any:
    """Set or offset the transform of one or more objects.

    With `relative=True` the values are added/multiplied onto the current
    transform instead of replacing it.
    """
    data = call("transform_objects", {
        "objects": objects, "location": location, "rotation": rotation,
        "scale": scale, "relative": relative, "rotation_mode": rotation_mode or None,
    })
    return respond(data, response_format, title="Transform updated")


@mcp.tool(annotations=WRITE)
@guard
def blender_apply_transform(
    objects: list[str], location: bool = True, rotation: bool = True,
    scale: bool = True, response_format: FORMAT = "json"
) -> Any:
    """Bake transforms into mesh data. Use this before exporting or when a
    modifier must see real geometry rather than an object transform."""
    return respond(call("apply_transform", {
        "objects": objects, "location": location, "rotation": rotation, "scale": scale
    }), response_format, title="Transforms applied")


@mcp.tool(annotations=WRITE)
@guard
def blender_select_objects(
    objects: Annotated[list[str] | str, Field(description="Names, or 'all' / 'none'.")] = "all",
    active: str = "", response_format: FORMAT = "json"
) -> Any:
    """Set the selection and active object. Many operators operate on the
    selection, so this is how you target them."""
    return respond(call("select_objects", {"objects": objects, "active": active or None}),
                   response_format, title="Selection")


@mcp.tool(annotations=WRITE)
@guard
def blender_join_objects(
    objects: list[str], response_format: FORMAT = "json"
) -> Any:
    """Join several mesh objects into the first one, merging their geometry."""
    return respond(call("join_objects", {"objects": objects}), response_format,
                   title="Joined")


@mcp.tool(annotations=WRITE)
@guard
def blender_parent_objects(
    child: str, parent: str, keep_transform: bool = True, response_format: FORMAT = "json"
) -> Any:
    """Parent one object to another (optionally keeping its world position)."""
    return respond(call("parent_objects", {
        "child": child, "parent": parent, "keep_transform": keep_transform
    }), response_format, title="Parented")


@mcp.tool(annotations=WRITE)
@guard
def blender_shade_smooth(
    objects: list[str],
    smooth: bool = True,
    auto_smooth_angle: Annotated[float | None, Field(description="Degrees. When set, adds a "
                                                                 "Smooth-by-Angle modifier so hard edges stay sharp.")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Set smooth or flat shading on meshes. Pass `auto_smooth_angle` (for
    example 30) to keep sharp creases on a smooth surface."""
    return respond(call("shade_smooth", {
        "objects": objects, "smooth": smooth, "auto_smooth_angle": auto_smooth_angle
    }), response_format, title="Shading updated")


# =========================================================================== #
# mesh editing & modifiers
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_edit_mesh(
    action: Annotated[str, Field(description="One of: extrude_faces, extrude_edges, extrude_verts, "
                                              "bevel, inset, inset_individual, subdivide, poke, triangulate, "
                                              "dissolve_edges, dissolve_faces, dissolve_verts, recalc_normals, "
                                              "flip_normals, symmetrize, connect, split_edges, spin, bridge, "
                                              "holes_fill, contextual_create, remove_doubles, delete, "
                                              "delete_verts, delete_edges, delete_faces, merge_verts, collapse, "
                                              "select_all, select_none, select_invert, set_shade_smooth, set_shade_flat.")],
    objects: list[str] | None = None,
    region: Annotated[str, Field(description="'selected' (default) or 'all'.")] = "selected",
    parameters: Annotated[dict | None, Field(description="Action specific arguments, e.g. "
                                                          "{'offset': 0.05, 'segments': 3} for bevel.")] = None,
    distance: float = 0.0001,
    response_format: FORMAT = "json",
) -> Any:
    """Edit mesh topology: extrude, bevel, inset, subdivide, dissolve, delete,
    merge, recalculate normals and more.

    Operates on the current edit-mode selection inside the object unless
    `region='all'`. Vertex/edge/face counts before and after are returned.
    """
    data = call("edit_mesh", {
        "action": action, "objects": objects, "region": region,
        "parameters": parameters, "distance": distance,
    })
    return respond(data, response_format, title=f"Mesh edit: {action}")


@mcp.tool(annotations=WRITE)
@guard
def blender_add_modifier(
    object: str,
    type: Annotated[str, Field(description="Modifier id, e.g. BEVEL, SUBSURF, ARRAY, SOLIDIFY, "
                                            "MIRROR, DISPLACE, SIMPLE_DEFORM, SHRINKWRAP, WIREFRAME, NODES.")],
    name: str = "",
    properties: Annotated[dict | None, Field(description="Modifier properties such as "
                                                          "{'width': 0.05, 'segments': 3}. Unknown keys are skipped.")] = None,
    flags: Annotated[dict | None, Field(description="Boolean switches such as "
                                                       "{'show_viewport': false, 'show_render': true}.")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Add a modifier to an object. Non-destructive: use blender_apply_modifier
    or leave it in place for a live, tweakable result."""
    return respond(call("add_modifier", {
        "object": object, "type": type, "name": name or None,
        "properties": properties, "flags": flags,
    }), response_format, title="Modifier added")


@mcp.tool(annotations=WRITE)
@guard
def blender_apply_modifier(
    object: str, modifier: Annotated[str, Field(description="Modifier name, or 'all' to apply repeatedly.")] = "",
    response_format: FORMAT = "json",
) -> Any:
    """Bake a modifier's result into the mesh, making it permanent."""
    return respond(call("apply_modifier", {"object": object, "modifier": modifier or None}),
                   response_format, title="Modifier applied")


# =========================================================================== #
# collections
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_create_collection(
    name: str, parent: str = "", objects: list[str] | None = None,
    response_format: FORMAT = "json"
) -> Any:
    """Create a collection, optionally nested under `parent` and optionally
    moving existing objects into it."""
    return respond(call("create_collection", {
        "name": name, "parent": parent or None, "objects": objects
    }), response_format, title="Collection created")


@mcp.tool(annotations=WRITE)
@guard
def blender_assign_to_collection(
    objects: list[str], collection: str, response_format: FORMAT = "json"
) -> Any:
    """Add objects to an collection that already exists in the scene.

    Use blender_create_collection first if the collection is missing. Objects
    stay linked to their other collections, so this adds a reference rather
    than moving them.
    """
    return respond(call("assign_to_collection", {"objects": objects, "collection": collection}),
                   response_format, title="Assigned to collection")


# =========================================================================== #
# materials
# =========================================================================== #


@mcp.tool(annotations=READ)
@guard
def blender_list_materials(detailed: bool = False, response_format: FORMAT = "markdown") -> Any:
    """List all materials. With `detailed=True` also returns the node types and
    the current Principled BSDF input values of each material."""
    return respond(call("list_materials", {"detailed": detailed}), response_format,
                   title="Materials")


@mcp.tool(annotations=WRITE)
@guard
def blender_create_material(
    name: str,
    base_color: Annotated[list[float] | None, Field(description="RGBA, linear 0-1, e.g. [0.8, 0.1, 0.1, 1]")] = None,
    metallic: float | None = None,
    roughness: float | None = None,
    ior: float | None = None,
    emission_color: list[float] | None = None,
    emission_strength: float | None = None,
    assign_to: list[str] | None = None,
    overwrite: bool = False,
    response_format: FORMAT = "json",
) -> Any:
    """Create a Principled BSDF material and optionally assign it to objects.

    Example - a glowing red metal ->
    `name='NeonRed', base_color=[0.8, 0.02, 0.02, 1], metallic=0.9, roughness=0.25, emission_color=[1,0,0,1], emission_strength=5`
    """
    data = call("create_material", {
        "name": name, "base_color": base_color, "metallic": metallic,
        "roughness": roughness, "ior": ior, "emission_color": emission_color,
        "emission_strength": emission_strength, "assign_to": assign_to,
        "overwrite": overwrite,
    })
    return respond(data, response_format, title=f"Material: {name}")


@mcp.tool(annotations=WRITE)
@guard
def blender_assign_material(
    objects: list[str], material: str, response_format: FORMAT = "json"
) -> Any:
    """Assign an existing material to objects, replacing their current slots."""
    return respond(call("assign_material", {"objects": objects, "material": material}),
                   response_format, title="Material assigned")


@mcp.tool(annotations=WRITE)
@guard
def blender_set_material_node(
    material: str,
    node_type: Annotated[str, Field(description="noise, voronoi, gradient, wave, checker, magic, "
                                                 "brick, musgrave, white_noise, image or environment.")] = "noise",
    node_name: str = "",
    properties: Annotated[dict | None, Field(description="Node settings, e.g. {'Scale': 8, "
                                                          "'Detail': 6} for noise, or {'Scale': 5, "
                                                          "'Randomness': 1} for voronoi.")] = None,
    image: str = "",
    output: str = "",
    input: str = "Base Color",
    location: list[float] | None = None,
    response_format: FORMAT = "json",
) -> Any:
    """Add a procedural or image texture node to a material and wire it into the
    Principled BSDF. This is the fast path to interesting surfaces without
    hand-building node trees.

    Example - marbled stone ->
    `material='Stone', node_type='noise', properties={'Scale': 6, 'Detail': 8, 'Roughness': 0.6}, input='Base Color'`
    """
    return respond(call("set_material_node", {
        "material": material, "node_type": node_type, "node_name": node_name or None,
        "properties": properties, "image": image or None, "output": output or None,
        "input": input, "location": location,
    }), response_format, title="Material node added")


@mcp.tool(annotations=WRITE)
@guard
def blender_load_image_texture(
    path: str, material: str = "", response_format: FORMAT = "json"
) -> Any:
    """Load an image from disk (including .hdr/.exr) and plug it into a material
    as Base Color. Creates the material if you do not name one."""
    return respond(call("load_image_texture", {"path": path, "material": material or None}),
                   response_format, title="Image texture loaded")


# =========================================================================== #
# camera, lights, world
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_add_camera(
    name: str = "Camera",
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    lens: float | None = None,
    fov_degrees: float | None = None,
    point_at: Annotated[str, Field(description="Object name or 'x,y,z' the camera should look at.")] = "",
    active: bool = True,
    collection: str = "",
    response_format: FORMAT = "json",
) -> Any:
    """Add a camera. `lens` in millimetres (50 = normal, 24 = wide, 85 = portrait)
    or `fov_degrees` as an alternative. Usually also pass `point_at` so it is
    actually aimed at something."""
    return respond(call("add_camera", {
        "name": name, "location": location, "rotation": rotation, "lens": lens,
        "fov_degrees": fov_degrees, "point_at": point_at or None, "active": active,
        "collection": collection or None,
    }), response_format, title="Camera added")


@mcp.tool(annotations=WRITE)
@guard
def blender_look_at(
    object: str,
    target: Annotated[str, Field(description="Object name or 'x,y,z' coordinates.")],
    response_format: FORMAT = "json",
) -> Any:
    """Aim any object along its local -Z axis at a target (its local +Y becomes
    'up'). Works for cameras and spot lights."""
    return respond(call("look_at", {"object": object, "target": target}),
                   response_format, title="Aimed at target")


@mcp.tool(annotations=WRITE)
@guard
def blender_set_active_camera(name: str, response_format: FORMAT = "json") -> Any:
    """Choose which camera the render uses. Call this before blender_render or
    blender_capture_viewport with mode='camera' if the scene has several."""
    return respond(call("set_active_camera", {"name": name}), response_format,
                   title="Active camera")


@mcp.tool(annotations=WRITE)
@guard
def blender_add_light(
    type: Annotated[str, Field(description="POINT, SUN, SPOT or AREA.")] = "AREA",
    name: str = "",
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    energy: float = 1000.0,
    color: Annotated[list[float] | None, Field(description="RGB 0-1")] = None,
    size: float | None = None,
    size_y: float | None = None,
    point_at: str = "",
    collection: str = "",
    response_format: FORMAT = "json",
) -> Any:
    """Add a light. Typical energies: SUN 2-5, AREA 100-1000 W, POINT 50-500 W,
    SPOT 100-1000 W. `size` softens shadows on AREA/POINT lights."""
    return respond(call("add_light", {
        "type": type, "name": name or None, "location": location, "rotation": rotation,
        "energy": energy, "color": color, "size": size, "size_y": size_y,
        "point_at": point_at or None, "collection": collection or None,
    }), response_format, title="Light added")


@mcp.tool(annotations=WRITE)
@guard
def blender_set_world(
    preset: Annotated[str, Field(description="'color' (default), 'studio_gray', 'sky' (physical "
                                               "Nishita sky) or 'hdri'.")] = "color",
    color: list[float] | None = None,
    strength: float = 1.0,
    hdri: str = "",
    gradient: Annotated[list[list[float]] | None, Field(description="[[top_rgb], [bottom_rgb]] "
                                                                      "for a vertical gradient.")] = None,
    sun_elevation: float | None = None,
    sun_rotation: float | None = None,
    response_format: FORMAT = "json",
) -> Any:
    """Set the world/environment: flat colour, a neutral studio grey, a physical
    sky, an HDRI image, or a vertical gradient. The world is what you see through
    the background and what ambient light comes from."""
    return respond(call("set_world", {
        "preset": preset, "color": color, "strength": strength, "hdri": hdri or None,
        "gradient": gradient, "sun_elevation": sun_elevation, "sun_rotation": sun_rotation,
    }), response_format, title="World updated")


# =========================================================================== #
# render & capture
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_set_render_settings(
    engine: Annotated[str, Field(description="'eevee' (fast), 'cycles' (physically correct) or "
                                            "'workbench' (fastest, solid preview).")] = "",
    resolution: Annotated[list[int] | None, Field(description="[width, height]")] = None,
    resolution_percentage: int | None = None,
    samples: int | None = None,
    fps: int | None = None,
    frame_range: Annotated[list[int] | None, Field(description="[start, end]")] = None,
    view_transform: Annotated[str, Field(description="'AgX' (default filmic), 'Filmic' or 'Standard'.")] = "",
    film_transparent: bool | None = None,
    output_format: str = "",
    output_path: str = "",
    use_motion_blur: bool | None = None,
    denoise: bool | None = None,
    max_bounces: int | None = None,
    gpu_device: str = "",
    dof: Annotated[dict | None, Field(description="Depth of field: {'focus_object': 'Cube', "
                                                    "'aperture_fstop': 2.8}.")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Configure the renderer: engine, resolution, samples, colour management,
    transparency, motion blur, depth of field and output settings."""
    return respond(call("set_render_settings", {
        "engine": engine or None, "resolution": resolution,
        "resolution_percentage": resolution_percentage, "samples": samples, "fps": fps,
        "frame_range": frame_range, "view_transform": view_transform or None,
        "film_transparent": film_transparent, "output_format": output_format or None,
        "output_path": output_path or None, "use_motion_blur": use_motion_blur,
        "denoise": denoise, "max_bounces": max_bounces, "gpu_device": gpu_device or None,
        "dof": dof,
    }), response_format, title="Render settings")


@mcp.tool(annotations=SLOW)
@guard
def blender_capture_viewport(
    mode: Annotated[str, Field(description="'viewport' (fast offscreen 3D view), "
                                            "'camera' (OpenGL through the scene camera), "
                                            "'window' (whole Blender window) or 'render' (full render).")] = "viewport",
    width: int = 1280,
    height: int = 720,
    view: Annotated[str, Field(description="For mode='viewport': 'front', 'back', 'left', 'right', "
                                            "'top', 'bottom', 'camera' or 'left'/'right' variants.")] = "",
    output_path: str = "",
    response_format: FORMAT = "json",
) -> Any:
    """Take a picture of what Blender is showing and return it as an image.

    This is how you check your work - call it after a change and look at the
    result. 'viewport' is nearly instant, 'render' is the real deal.
    """
    data = call("capture", {
        "mode": mode, "width": width, "height": height, "view": view or None,
        "output_path": output_path or None,
    })
    return with_image(data, response_format, "Viewport capture")


@mcp.tool(annotations=SLOW)
@guard
def blender_render(
    output_path: Annotated[str, Field(description="Where to write the image. Relative paths "
                                                  "resolve against BLENDER_MCP_ROOT (your home directory).")] = "",
    mode: Annotated[str, Field(description="'still' for one frame, 'animation' for the whole range.")] = "still",
    format: Annotated[str, Field(description="PNG, JPEG, WEBP, OPEN_EXR ...")] = "",
    response_format: FORMAT = "json",
) -> Any:
    """Render with the current engine and return the resulting image.

    Cycles at 1080p can take minutes; check samples and resolution with
    blender_get_scene first, and consider the 'eevee' engine for iteration.
    """
    data = call("render", {
        "output_path": output_path or None, "mode": mode, "format": format or None,
    }, timeout=3600.0)
    return with_image(data, response_format, "Render", key="output")


# =========================================================================== #
# files
# =========================================================================== #


@mcp.tool(annotations=DESTRUCTIVE)
@guard
def blender_save_blend(
    path: Annotated[str, Field(description="Target .blend path. Empty saves over the "
                                            "currently open file.")] = "",
    response_format: FORMAT = "json",
) -> Any:
    """Save the scene to a .blend file. Pass a path for Save As, omit it to save
    in place. Unwritten work is lost if Blender crashes, so save as you go."""
    return respond(call("file_op", {"action": "save_as" if path else "save", "path": path or None}),
                   response_format, title="Saved")


@mcp.tool(annotations=DESTRUCTIVE)
@guard
def blender_open_blend(
    path: str, response_format: FORMAT = "json"
) -> Any:
    """Open a .blend file, discarding the current scene. Save first."""
    return respond(call("file_op", {"action": "open", "path": path}), response_format,
                   title="Opened")


@mcp.tool(annotations=DESTRUCTIVE)
@guard
def blender_new_file(empty: bool = True, response_format: FORMAT = "json") -> Any:
    """Start a new empty scene, discarding everything currently open."""
    return respond(call("file_op", {"action": "new", "empty": empty}), response_format,
                   title="New file")


@mcp.tool(annotations=SLOW)
@guard
def blender_import_model(
    path: str,
    collection: str = "",
    properties: Annotated[dict | None, Field(description="Importer options, e.g. {'scale': 0.01} "
                                                          "for FBX or {'import_pack_images': True} for glTF.")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Import a 3D model from disk: .glb/.gltf, .fbx, .obj, .stl, .ply, .usd,
    .abc, .dae or .blend. Returns the names of the objects that were created."""
    return respond(call("import_model", {
        "path": path, "collection": collection or None, "properties": properties
    }), response_format, title="Imported")


@mcp.tool(annotations=SLOW)
@guard
def blender_export_model(
    path: str,
    selection: Annotated[str, Field(description="'selected_objects' or 'all'.")] = "all",
    properties: Annotated[dict | None, Field(description="Exporter options, e.g. "
                                                          "{'export_format': 'GLB', 'use_selection': True}.")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Export the scene to a model file: .glb, .gltf, .fbx, .obj, .stl, .ply,
    .usd, .abc or .blend."""
    return respond(call("export_model", {
        "path": path, "selection": selection, "properties": properties
    }), response_format, title="Exported")


# =========================================================================== #
# animation
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_set_frame(
    frame: int | None = None, start: int | None = None, end: int | None = None,
    fps: int | None = None, response_format: FORMAT = "json"
) -> Any:
    """Jump to a frame and/or set the animation range and playback speed."""
    return respond(call("set_frame", {
        "frame": frame, "start": start, "end": end, "fps": fps
    }), response_format, title="Frame settings")


@mcp.tool(annotations=WRITE)
@guard
def blender_insert_keyframe(
    objects: list[str] | None = None,
    frame: int | None = None,
    location: bool = True,
    rotation: bool = False,
    scale: bool = False,
    data_paths: list[str] | None = None,
    group: str = "",
    response_format: FORMAT = "json",
) -> Any:
    """Insert keyframes on the current or given frame.

    Defaults to keyframing the location of the active object. `data_paths` lets
    you key anything addressable, e.g. ['location', 'rotation_euler', 'data.energy'].
    """
    return respond(call("insert_keyframe", {
        "objects": objects, "frame": frame,
        "location": location, "rotation": rotation, "scale": scale,
        "data_paths": data_paths, "group": group or None,
    }), response_format, title="Keyframes inserted")


@mcp.tool(annotations=READ)
@guard
def blender_animation_info(response_format: FORMAT = "json") -> Any:
    """Report the frame range, all actions, and which objects are animated."""
    return respond(call("animation_info", {}), response_format, title="Animation")


# =========================================================================== #
# v2: model validation
# =========================================================================== #


@mcp.tool(annotations=READ)
@guard
def blender_validate(
    target: Annotated[dict | None, Field(
        description="Real-world spec to check against, e.g. "
                    "{'length': 3.765, 'width': 1.490, 'height': 1.370}. "
                    "Keys: length, width, height (metres).")] = None,
    objects: Annotated[list[str] | None, Field(
        description="Restrict the audit to these object names.")] = None,
    ignore: Annotated[list[str] | None, Field(
        description="Glob patterns to exclude, e.g. ['Sweep', 'Ground*']. "
                    "Studio props otherwise dominate every size check.")] = None,
    checks: Annotated[list[str] | None, Field(
        description="Run only these checks: scale, dimensions, normals, topology, "
                    "intersections, symmetry, naming, materials, uv, transforms, "
                    "pivots, budget, lods, lighting, orphans.")] = None,
    tolerance_pct: Annotated[float, Field(
        description="Allowed deviation for the dimension check, in percent.")] = 2.0,
    max_faces: Annotated[int, Field(
        description="Face budget above which the budget check warns.")] = 500_000,
    weld_distance: Annotated[float, Field(
        description="Weld distance used when reporting duplicate vertices.")] = 1e-5,
    max_pivot_offset: Annotated[float, Field(
        description="Distance from geometry centre above which a pivot is reported.")] = 0.05,
    orphans: bool = True,
    response_format: FORMAT = "markdown",
) -> Any:
    """Audit the model and return a scored report: scale, dimensions, normals,
    topology, intersections, symmetry, naming, materials, UVs, transforms,
    pivots, poly budget, LODs, lighting and orphan datablocks.

    This is the first tool to reach for after building something, and again
    before exporting. Pass `target` to check real-world proportions against a
    known vehicle or asset spec.

    Each check reports `ok`, `info`, `warn` or `error`; a check that itself
    crashes is reported as an `error` entry rather than aborting the audit, so
    you always get a full picture.
    """
    return respond(call("validate", {
        "target": target, "objects": objects, "ignore": ignore, "checks": checks,
        "tolerance_pct": tolerance_pct, "max_faces": max_faces,
        "weld_distance": weld_distance, "max_distance": max_pivot_offset,
        "orphans": orphans,
    }), response_format, title="Model validation")


@mcp.tool(annotations=READ)
@guard
def blender_find_problems(
    target: Annotated[dict | None, Field(
        description="Real-world spec, same shape as blender_validate.")] = None,
    objects: Annotated[list[str] | None, Field(description="Object names.")] = None,
    ignore: Annotated[list[str] | None, Field(description="Glob patterns to exclude.")] = None,
    max_faces: int = 500_000,
    response_format: FORMAT = "markdown",
) -> Any:
    """Like `blender_validate`, but only the failures, each with a concrete fix.

    Use this when you want to act rather than read: it returns an `actionable`
    list where every entry pairs a problem with the tool call that resolves it.
    """
    return respond(call("find_problems", {
        "target": target, "objects": objects, "ignore": ignore, "max_faces": max_faces,
    }), response_format, title="Actionable problems")


@mcp.tool(annotations=READ)
@guard
def blender_analyze_mesh(
    object: Annotated[str, Field(description="Name of the mesh object to analyse.")],
    response_format: FORMAT = "markdown",
) -> Any:
    """Deep statistics for one mesh.

    Beyond face counts: manifold/boundary/wire edge census, whether the shell is
    closed, signed volume, surface area, min/max/zero-area faces, loose verts and
    edges, duplicate vertices within 1e-5, UV layers, vertex groups, shape keys
    and the modifier stack. Use it to decide between fixing a mesh and
    regenerating it.
    """
    return respond(call("analyze_mesh", {"object": object}),
                   response_format, title="Mesh analysis")


@mcp.tool(annotations=READ)
@guard
def blender_measure(
    mode: Annotated[Literal["bbox", "objects", "assembly"], Field(
        description="'bbox' of one object, 'objects' centre-to-centre distance, "
                    "'assembly' extent of the whole scene.")] = "bbox",
    object: Annotated[str, Field(description="Object name for mode='bbox'.")] = "",
    a: Annotated[str, Field(description="First object for mode='objects'.")] = "",
    b: Annotated[str, Field(description="Second object for mode='objects'.")] = "",
    objects: Annotated[list[str] | None, Field(description="Restrict mode='assembly'.")] = None,
    response_format: FORMAT = "markdown",
) -> Any:
    """Measure real-world distances that are tedious to eyeball: a bounding box,
    the distance between two object centres, or the extent of a whole assembly.
    """
    return respond(call("measure", {
        "mode": mode, "object": object, "a": a, "b": b, "objects": objects,
    }), response_format, title="Measurements")


# =========================================================================== #
# v2: textures
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_generate_texture(
    pattern: Annotated[str, Field(
        description="One of: solid, noise, fbm, voronoi, checker, grid, stripes, "
                    "gradient, radial, brushed_metal, rust, scratches, grunge, "
                    "concrete, asphalt, wood, leather, carbon, hazard, plate.")],
    size: Annotated[int, Field(description="Square resolution, 8..4096.")] = 1024,
    seed: int = 0,
    scale: Annotated[float, Field(description="Feature count across the image.")] = 8.0,
    octaves: Annotated[int, Field(description="Detail layers for fbm-style patterns.")] = 6,
    contrast: Annotated[float, Field(description="1.0 neutral, higher is punchier.")] = 1.0,
    text: Annotated[str, Field(
        description="Characters to stamp, used by pattern='plate'.")] = "",
    also_normal: Annotated[bool, Field(
        description="Also write a derived normal map.")] = False,
    normal_strength: Annotated[float, Field(description="Normal map strength.")] = 2.0,
    name: Annotated[str, Field(description="Image datablock name and file stem.")] = "",
    output_path: Annotated[str, Field(
        description="Explicit PNG path; overrides output_dir.")] = "",
    output_dir: Annotated[str, Field(
        description="Directory for the PNG. Defaults to ~/BlenderMCP_Textures.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Generate a procedural texture and write it to a real PNG on disk.

    Useful for plate text, hazard stripes, carbon weave, rust, brushed metal,
    leather grain and similar. The file is a normal image you can inspect,
    pack and ship - not a viewport-only effect.
    """
    return respond(call("generate_texture", {
        "pattern": pattern, "size": size, "seed": seed, "scale": scale,
        "octaves": octaves, "contrast": contrast, "text": text,
        "also_normal": also_normal, "normal_strength": normal_strength,
        "name": name, "output_path": output_path, "output_dir": output_dir,
    }), response_format, title="Texture generated")


@mcp.tool(annotations=WRITE)
@guard
def blender_generate_pbr_set(
    pattern: str = "fbm",
    size: int = 1024,
    seed: int = 0,
    scale: float = 8.0,
    octaves: int = 6,
    contrast: float = 1.0,
    color: Annotated[str, Field(description="Shadow colour as #rrggbb.")] = "#808080",
    color2: Annotated[str, Field(description="Highlight colour as #rrggbb.")] = "#ffffff",
    metallic: float = 0.0,
    roughness_min: float = 0.25,
    roughness_max: float = 0.85,
    normal_strength: float = 2.0,
    name: Annotated[str, Field(description="File stem for the set.")] = "",
    material: Annotated[str, Field(
        description="Existing material to wire the set into. Optional; when given, "
                    "the maps are connected to its Principled BSDF.")] = "",
    output_dir: Annotated[str, Field(description="Output directory.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Generate a matched BaseColor / Roughness / Metallic / Normal / AO set.

    All five maps come from one height field and one seed, which is what makes
    them read as a single material. BaseColor is written sRGB, the data maps
    Non-Color. Pass `material` to connect everything to a Principled BSDF in
    one step, including an AO multiply onto Base Color.
    """
    return respond(call("generate_pbr_set", {
        "pattern": pattern, "size": size, "seed": seed, "scale": scale,
        "octaves": octaves, "contrast": contrast, "color": color, "color2": color2,
        "metallic": metallic, "roughness_min": roughness_min,
        "roughness_max": roughness_max, "normal_strength": normal_strength,
        "name": name, "material": material, "output_dir": output_dir,
    }), response_format, title="PBR set generated")


@mcp.tool(annotations=SLOW)
@guard
def blender_bake_texture(
    object: Annotated[str, Field(description="Mesh with the material to bake.")] = "",
    target: Annotated[Literal["DIFFUSE", "NORMAL", "ROUGHNESS", "METALLIC", "AO",
                               "SHADOW", "EMIT", "ENVIRONMENT", "DIRECT", "AO_PASS"],
                        Field(description="Bake pass to render.")] = "DIFFUSE",
    size: int = 1024,
    output_dir: Annotated[str, Field(description="Output directory.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Bake a material's procedural node setup down to image files on disk.

    Needs UVs and a node-based material. Cycles is the reliable bake engine; if
    the bake fails the result says so and why instead of raising.
    """
    return respond(call("bake_texture", {
        "object": object, "target": target, "size": size, "output_dir": output_dir,
    }), response_format, title="Texture baked")


@mcp.tool(annotations=WRITE)
@guard
def blender_pack_textures(repack: bool = False, response_format: FORMAT = "markdown") -> Any:
    """Pack every loose image into the .blend so the file is self-contained.

    Do this before handing a .blend to someone else or committing it.
    """
    return respond(call("pack_textures", {"repack": repack}),
                   response_format, title="Textures packed")


@mcp.tool(annotations=READ)
@guard
def blender_list_images(limit: int = 100, response_format: FORMAT = "markdown") -> Any:
    """List every image datablock with size, source, colour space and packed state."""
    return respond(call("list_images", {"limit": limit}),
                   response_format, title="Images")


# =========================================================================== #
# v2: context, selection, history
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_set_context(
    active: Annotated[str, Field(description="Object to make active.")] = "",
    select: Annotated[list[str] | None, Field(description="Objects to select.")] = None,
    collection: Annotated[str, Field(
        description="Collection to make the active layer collection.")] = "",
    mode: Annotated[Literal["OBJECT", "EDIT", "POSE", "SCULPT"], Field(
        description="Mode to switch to once the active object is set.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Set mode, active object, active collection and selection atomically.

    Most operator calls need all four set correctly. Doing it in one round trip
    avoids leaving Blender in a half-applied state when one step fails.
    """
    return respond(call("set_context", {
        "active": active, "select": select, "collection": collection, "mode": mode,
    }), response_format, title="Context set")


@mcp.tool(annotations=READ)
@guard
def blender_select_by(
    by: Annotated[Literal["name", "type", "material", "collection", "size", "loose",
                          "no_material", "no_uv", "empty_parent"], Field(
        description="Predicate to select by.")],
    pattern: Annotated[str, Field(description="Glob pattern for by='name'.")] = "*",
    object_type: Annotated[str, Field(description="For by='type', e.g. MESH.")] = "MESH",
    material: Annotated[str, Field(description="For by='material'.")] = "",
    collection: Annotated[str, Field(description="For by='collection'.")] = "",
    min_dimension: Annotated[float, Field(
        description="For by='size', smallest accepted dimension in metres.")] = 1.0,
    axis: Annotated[int, Field(
        description="For by='size': -1 for the largest dimension, 0/1/2 for one axis.")] = -1,
    limit: int = 500,
    make_active: bool = True,
    response_format: FORMAT = "markdown",
) -> Any:
    """Select objects by a predicate rather than by name.

    Names are what agents get wrong on a large scene. This finds every mesh with
    loose geometry, without a material, without UVs, larger than N metres,
    belonging to a collection, or matching a glob.
    """
    return respond(call("select_by", {
        "by": by, "pattern": pattern, "object_type": object_type,
        "material": material, "collection": collection,
        "min_dimension": min_dimension, "axis": axis, "limit": limit,
        "make_active": make_active,
    }), response_format, title="Selection by predicate")


@mcp.tool(annotations=WRITE)
@guard
def blender_undo(response_format: FORMAT = "markdown") -> Any:
    """Undo the last change pushed onto Blender's undo stack.

    Pair it with `blender_checkpoint` to make a step reversible. Refuses to
    rewind across an Open File, because Blender's undo stack does not survive
    one and attempting it crashes Blender rather than raising.
    """
    return respond(call("undo", {}), response_format, title="Undo")


@mcp.tool(annotations=WRITE)
@guard
def blender_redo(response_format: FORMAT = "markdown") -> Any:
    """Redo the last undone change, restoring the scene to the state it had
    before the matching `blender_undo` call."""
    return respond(call("redo", {}), response_format, title="Redo")


@mcp.tool(annotations=WRITE)
@guard
def blender_checkpoint(
    label: Annotated[str, Field(description="Name shown in Blender's undo history.")] = "mcp checkpoint",
    response_format: FORMAT = "markdown",
) -> Any:
    """Push a named marker onto the undo stack so a later `blender_undo` returns here."""
    return respond(call("checkpoint", {"label": label}),
                   response_format, title="Checkpoint")


# =========================================================================== #
# v2: geometry, modifiers, UV, rig, physics
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_geometry(
    operation: Annotated[Literal["mirror", "array", "solidify", "screw", "spin",
                                   "weld", "recalc_normals", "flip_normals",
                                   "triangulate", "decimate", "remesh", "wireframe",
                                   "shrink_fatten", "bevel_all"], Field(
        description="Geometry operation to apply.")],
    objects: Annotated[list[str] | None, Field(description="Target meshes.")] = None,
    axis: Annotated[Literal["X", "Y", "Z"], Field(description="For mirror/spin.")] = "X",
    count: Annotated[int, Field(description="For array.")] = 3,
    offset: Annotated[list[float], Field(description="For array, constant offset.")] = [1.0, 0.0, 0.0],
    thickness: float = 0.02,
    steps: Annotated[int, Field(description="For screw/spin.")] = 12,
    angle: Annotated[float, Field(description="For screw/spin, radians.")] = 3.141592653589793,
    radius: float = 0.5,
    distance: Annotated[float, Field(description="For weld.")] = 0.0001,
    ratio: Annotated[float, Field(description="For decimate, 0..1.")] = 0.5,
    mode: Annotated[str, Field(description="For remesh: VOXEL, BLOCKS, SMOOTH, SHARP.")] = "VOXEL",
    voxel_size: float = 0.05,
    segments: int = 2,
    width: float = 0.005,
    value: float = 0.0,
    bisect: bool = False,
    clip: bool = True,
    merge: bool = True,
    response_format: FORMAT = "markdown",
) -> Any:
    """Geometry operations that do not fit the generic modifier tool.

    Covers mirror, array, solidify, screw, spin, weld, normal fixes,
    triangulation, decimation, remeshing, wireframe, shrink/fatten and a
    batch bevel. Edit-mode operations restore object mode automatically, even
    if the operator raises.
    """
    return respond(call("geometry", {
        "operation": operation, "objects": objects, "axis": axis, "count": count,
        "offset": offset, "thickness": thickness, "steps": steps, "angle": angle,
        "radius": radius, "distance": distance, "ratio": ratio, "mode": mode,
        "voxel_size": voxel_size, "segments": segments, "width": width,
        "value": value, "bisect": bisect, "clip": clip, "merge": merge,
    }), response_format, title=f"Geometry: {operation}")


@mcp.tool(annotations=WRITE)
@guard
def blender_modifiers(
    action: Annotated[Literal["list", "mute", "remove", "move", "apply"], Field(
        description="What to do with modifiers.")],
    objects: Annotated[list[str] | None, Field(description="Target objects.")] = None,
    modifier: Annotated[str, Field(
        description="Modifier name, or 'all' for remove/apply.")] = "",
    index: Annotated[int, Field(description="Target position for action='move'.")] = 0,
    value: Annotated[bool, Field(description="New state for action='mute'.")] = True,
    response_format: FORMAT = "markdown",
) -> Any:
    """Inspect, mute, reorder, remove or apply modifiers across objects.

    Modifier order changes results, and `list` shows the real evaluated stack
    rather than guessing from the UI.
    """
    return respond(call("modifiers", {
        "action": action, "objects": objects, "modifier": modifier,
        "index": index, "value": value,
    }), response_format, title=f"Modifiers: {action}")


@mcp.tool(annotations=WRITE)
@guard
def blender_uv(
    action: Annotated[Literal["report", "smart_project", "unwrap", "pack_islands",
                              "remove_doubles", "scale", "center"], Field(
        description="UV operation.")],
    objects: Annotated[list[str] | None, Field(description="Target meshes.")] = None,
    angle_limit_deg: float = 66.0,
    margin: float = 0.002,
    scale_to_bounds: bool = False,
    method: Annotated[str, Field(description="For unwrap: ANGLE_BASED or CONFORMAL.")] = "ANGLE_BASED",
    distance: float = 0.0001,
    x: float = 1.0,
    y: float = 1.0,
    response_format: FORMAT = "markdown",
) -> Any:
    """Unwrapping and UV maintenance: report, smart project, unwrap, pack
    islands, weld, scale and centre. A UV layer is created if missing."""
    return respond(call("uv", {
        "action": action, "objects": objects, "angle_limit_deg": angle_limit_deg,
        "margin": margin, "scale_to_bounds": scale_to_bounds, "method": method,
        "distance": distance, "x": x, "y": y,
    }), response_format, title=f"UV: {action}")


@mcp.tool(annotations=WRITE)
@guard
def blender_rig(
    action: Annotated[Literal["create_armature", "add_bone", "skin", "clear"], Field(
        description="Rigging operation.")],
    name: Annotated[str, Field(description="Armature name.")] = "Rig",
    bone: Annotated[str, Field(description="Bone name for add_bone.")] = "Bone",
    head: Annotated[list[float], Field(description="Bone head position.")] = [0.0, 0.0, 0.0],
    tail: Annotated[list[float], Field(description="Bone tail position.")] = [0.0, 0.0, 0.1],
    parent: Annotated[str, Field(description="Parent bone name.")] = "",
    object: Annotated[str, Field(description="Mesh to skin, or delete its groups for clear.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Create an armature, add bones, and bind meshes with automatic weights.

    Also the escape hatch for crash setups: skin the body panels, then simulate.
    """
    return respond(call("rig", {
        "action": action, "name": name, "bone": bone, "head": head, "tail": tail,
        "parent": parent, "object": object,
    }), response_format, title=f"Rig: {action}")


@mcp.tool(annotations=WRITE)
@guard
def blender_pose(
    armature: str,
    bone: str,
    location: Annotated[list[float] | None, Field(description="Local bone offset.")] = None,
    rotation_degrees: Annotated[list[float] | None, Field(
        description="Euler rotation in degrees.")] = None,
    scale: Annotated[list[float] | None, Field(description="Bone scale.")] = None,
    response_format: FORMAT = "markdown",
) -> Any:
    """Set a pose bone's location, rotation and scale. Useful for posing before
    a physics bake or for checking a rig's range of motion."""
    return respond(call("pose", {
        "armature": armature, "bone": bone, "location": location,
        "rotation_degrees": rotation_degrees, "scale": scale,
    }), response_format, title="Pose")


@mcp.tool(annotations=WRITE)
@guard
def blender_physics(
    action: Annotated[Literal["rigid_body", "passive", "bake", "cloth", "collision",
                               "soft_body", "force_field"], Field(
        description="Physics operation.")],
    objects: Annotated[list[str] | None, Field(description="Target meshes.")] = None,
    type: Annotated[str, Field(
        description="'ACTIVE'/'PASSIVE' for rigid_body, or a force field type "
                    "such as FORCE, WIND, VORTEX, TURBULENCE.")] = "ACTIVE",
    mass: Annotated[float | None, Field(description="Rigid body mass.")] = None,
    collision_shape: Annotated[str, Field(
        description="BOX, SPHERE, CAPSULE, CYLINDER, CONVEX_HULL, MESH, COMPOUND.")] = "",
    frames: Annotated[int, Field(description="Frames to bake.")] = 60,
    time_scale: float = 1.0,
    substeps: int = 10,
    solver_iterations: int = 10,
    quality: int = 5,
    preset: Annotated[str, Field(
        description="Cloth preset, e.g. COTTON, DENIM, LEATHER, or a number of "
                    "collision quality steps.")] = "",
    name: Annotated[str, Field(description="Force field object name.")] = "Field",
    location: Annotated[list[float], Field(description="Force field position.")] = [0.0, 0.0, 1.0],
    strength: Annotated[float | None, Field(description="Force field strength.")] = None,
    response_format: FORMAT = "markdown",
) -> Any:
    """Rigid bodies, cloth, soft bodies, collision and force fields.

    Set up separate panels as independent rigid bodies, add collision to the
    chassis, bake, and inspect. Rigid body constraints go through
    `blender_run_operator` with `bpy.ops.rigidbody.constraint_add`.
    """
    return respond(call("physics", {
        "action": action, "objects": objects, "type": type, "mass": mass,
        "collision_shape": collision_shape, "frames": frames,
        "time_scale": time_scale, "substeps": substeps,
        "solver_iterations": solver_iterations, "quality": quality, "preset": preset,
        "name": name, "location": location, "strength": strength,
    }), response_format, title=f"Physics: {action}")


# =========================================================================== #
# v2: scene + render extras
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_scene_ops(
    action: Annotated[Literal["duplicate_hierarchy", "instance_collection",
                               "apply_instances", "purge_orphans", "depsgraph",
                               "addons", "memory"], Field(
        description="Scene operation.")],
    object: Annotated[str, Field(description="Root object for duplicate_hierarchy.")] = "",
    new_name: Annotated[str, Field(description="Name for the duplicate root.")] = "",
    collection: Annotated[str, Field(description="Collection to instance.")] = "",
    location: Annotated[list[float], Field(description="Instance position.")] = [0.0, 0.0, 0.0],
    response_format: FORMAT = "markdown",
) -> Any:
    """Hierarchy and housekeeping: duplicate a parent with all children, instance
    or realise collections, purge orphans, and inspect the depsgraph, enabled
    add-ons or datablock counts."""
    return respond(call("scene_ops", {
        "action": action, "object": object, "new_name": new_name,
        "collection": collection, "location": location,
    }), response_format, title=f"Scene: {action}")


@mcp.tool(annotations=SLOW)
@guard
def blender_render_extras(
    action: Annotated[Literal["turntable", "clay", "passes", "contact_sheet"], Field(
        description="Render helper.")],
    frames: Annotated[int, Field(description="Turntable frame count.")] = 8,
    resolution: int = 960,
    pivot: Annotated[list[float], Field(description="Turntable pivot.")] = [0.0, 0.0, 0.0],
    passes: Annotated[list[str], Field(
        description="Render passes to enable: Z, NORMAL, AO, MIST, COMBINED.")] = ["Z", "NORMAL"],
    images: Annotated[list[str] | None, Field(
        description="Image paths for action='contact_sheet'.")] = None,
    columns: int = 3,
    cell: int = 420,
    output: Annotated[str, Field(description="Output path for contact_sheet.")] = "",
    output_dir: Annotated[str, Field(description="Output directory.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Turntable renders, a fast clay preview, render pass toggles, and contact
    sheets assembled from existing images. The clay preview uses Workbench, so
    it is near-instant even on a heavy scene."""
    return respond(call("render_extras", {
        "action": action, "frames": frames, "resolution": resolution, "pivot": pivot,
        "passes": passes, "images": images, "columns": columns, "cell": cell,
        "output": output, "output_dir": output_dir,
    }), response_format, title=f"Render: {action}")


# =========================================================================== #
# v2: batching
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_batch(
    steps: Annotated[list[dict], Field(
        description="Ordered steps, each {'command': <name>, 'params': {...}}. "
                    "Uses addon command names, e.g. 'add_primitive', 'set_transform', "
                    "'validate'.")],
    stop_on_error: Annotated[bool, Field(
        description="Abort at the first failure instead of continuing.")] = False,
    response_format: FORMAT = "markdown",
) -> Any:
    """Run many bridge commands in a single round trip.

    Each step is `{"command": <addon command>, "params": {...}}`. Results come
    back per step, so one failure does not hide the steps that worked. This is
    the fastest way to build a scene, because it collapses dozens of round trips
    into one.

    Command names are the addon's, not the tool names: `add_primitive`,
    `create_mesh`, `set_transform`, `assign_material`, `add_modifier`,
    `validate`, `geometry`, and so on.
    """
    return respond(call("batch", {"steps": steps, "stop_on_error": stop_on_error}),
                   response_format, title="Batch")


# =========================================================================== #
# v3: materials and shaders
# =========================================================================== #


@mcp.tool(annotations=WRITE)
@guard
def blender_make_material(
    name: Annotated[str, Field(description="Material name.")] = "Material",
    preset: Annotated[str, Field(
        description="Physical preset: plastic, matte, rubber, glass, frosted_glass, "
                    "chrome, brushed_steel, gold, copper, aluminium, car_paint, "
                    "car_paint_white, fabric, leather, wood, concrete, asphalt, "
                    "ceramic, emissive, emissive_red, neon, toon, velvet, sponge.")] = "matte",
    base_color: Annotated[list[float] | None, Field(description="RGB 0-1 override.")] = None,
    metallic: Annotated[float | None, Field(description="0-1 override.")] = None,
    roughness: Annotated[float | None, Field(description="0-1 override.")] = None,
    ior: Annotated[float | None, Field(description="Index of refraction override.")] = None,
    transmission: Annotated[float | None, Field(
        description="0-1; 1 makes the surface glass-like.")] = None,
    coat: Annotated[float | None, Field(
        description="Clear coat weight, for car paint and lacquer.")] = None,
    coat_roughness: Annotated[float | None, Field(description="0-1.")] = None,
    emission_color: Annotated[list[float] | None, Field(description="RGB 0-1.")] = None,
    emission_strength: Annotated[float | None, Field(description="0 and up.")] = None,
    alpha: float = 1.0,
    assign: Annotated[list[str] | None, Field(
        description="Object names to assign the new material to.")] = None,
    response_format: FORMAT = "markdown",
) -> Any:
    """Create a physically sensible PBR material from a named preset.

    Presets set the Principled BSDF correctly for the real substance - car paint
    gets metallic plus a clear coat, glass gets transmission and IOR 1.52,
    leather and fabric get high roughness, emissive presets get emission colour
    and strength. Any socket can be overridden. Pass `assign` to put it on
    objects in the same call.
    """
    return respond(call("create_material_preset", {
        "name": name, "preset": preset, "base_color": base_color,
        "metallic": metallic, "roughness": roughness, "ior": ior,
        "transmission": transmission, "coat": coat,
        "coat_roughness": coat_roughness, "emission_color": emission_color,
        "emission_strength": emission_strength, "alpha": alpha, "assign": assign,
    }), response_format, title="Material created")


@mcp.tool(annotations=WRITE)
@guard
def blender_build_shader(
    nodes: Annotated[list[dict], Field(
        description="Node specs: {'id', 'type', 'location', 'inputs', 'properties'}. "
                    "Type may be a friendly alias (noise, voronoi, ramp, bump, "
                    "mix, mix_rgb, math, mapping, uv, image, fresnel, hsv, "
                    "emission, add_shader, mix_shader, output).")],
    name: Annotated[str, Field(description="Material to build the graph into.")] = "Shader",
    links: Annotated[list[list], Field(
        description="Connections as [from_id, from_socket, to_id, to_socket].")] = [],
    assign: Annotated[list[str] | None, Field(description="Objects to assign to.")] = None,
    response_format: FORMAT = "markdown",
) -> Any:
    """Build an arbitrary shader node graph from a declarative description.

    This is the full-control path when a preset is not enough. Socket names are
    the real Blender ones, so anything from the manual works. Problems are
    reported per node rather than aborting, so one bad socket does not cost you
    the whole graph.
    """
    return respond(call("build_shader", {
        "name": name, "nodes": nodes, "links": links, "assign": assign,
    }), response_format, title="Shader graph built")


@mcp.tool(annotations=READ)
@guard
def blender_shader_info(
    name: Annotated[str, Field(description="Material to inspect.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Dump a material's node graph: every node, its unconnected inputs with
    values, its outputs, and all links. Use it to find out what a material
    actually is before editing it."""
    return respond(call("shader_info", {"name": name}),
                   response_format, title="Shader graph")


@mcp.tool(annotations=WRITE)
@guard
def blender_set_shader_input(
    material: str,
    node: Annotated[str, Field(description="Node name in the graph.")],
    socket: Annotated[str, Field(description="Input socket name, e.g. 'Base Color'.")],
    value: Annotated[Any, Field(description="Value; a list of 3-4 for colours.")],
    response_format: FORMAT = "markdown",
) -> Any:
    """Set one input socket on one node, by name. No guessing at socket indices."""
    return respond(call("set_shader_input", {
        "material": material, "node": node, "socket": socket, "value": value,
    }), response_format, title="Shader input set")


@mcp.tool(annotations=WRITE)
@guard
def blender_connect_shader(
    material: str,
    from_node: str,
    from_socket: str,
    to_node: str,
    to_socket: str,
    response_format: FORMAT = "markdown",
) -> Any:
    """Connect an output socket to an input socket inside a material."""
    return respond(call("connect_shader", {
        "material": material, "from_node": from_node, "from_socket": from_socket,
        "to_node": to_node, "to_socket": to_socket,
    }), response_format, title="Shader connected")


@mcp.tool(annotations=WRITE)
@guard
def blender_procedural_material(
    name: Annotated[str, Field(description="Material name.")] = "Procedural",
    pattern: Annotated[Literal["noise", "fbm", "voronoi", "wave", "checker"], Field(
        description="Driver texture.")] = "fbm",
    scale: Annotated[float, Field(description="Feature size.")] = 6.0,
    detail: float = 8.0,
    distortion: float = 0.0,
    feature: Annotated[str, Field(
        description="Voronoi feature: F1, F2, SMOOTH_F1, DISTANCE_TO_EDGE.")] = "F1",
    wave_type: Annotated[str, Field(
        description="Wave type: BANDS, RINGS, X, Y, Z, DIAGONAL.")] = "BANDS",
    color_a: Annotated[list[float], Field(description="Low colour RGB.")] = [0.05, 0.05, 0.05],
    color_b: Annotated[list[float], Field(description="High colour RGB.")] = [0.6, 0.6, 0.6],
    ramp_low: float = 0.25,
    ramp_high: float = 0.75,
    rough_low: float = 0.25,
    rough_high: float = 0.85,
    metallic: float = 0.0,
    bump_strength: Annotated[float, Field(description="0-1 bump relief.")] = 0.25,
    bump_distance: float = 0.02,
    assign: Annotated[list[str] | None, Field(description="Objects to assign to.")] = None,
    response_format: FORMAT = "markdown",
) -> Any:
    """Build a complete procedural surface in one call.

    Wires a coordinate and mapping node to a noise, voronoi, wave or checker
    texture, then through a colour ramp into Base Color, a map-range into
    Roughness, and the raw field into a bump node. This is the graph most
    hard-surface and natural surfaces actually need, and it is tedious to
    assemble socket by socket.
    """
    return respond(call("procedural_material", {
        "name": name, "pattern": pattern, "scale": scale, "detail": detail,
        "distortion": distortion, "feature": feature, "wave_type": wave_type,
        "color_a": color_a, "color_b": color_b, "ramp_low": ramp_low,
        "ramp_high": ramp_high, "rough_low": rough_low, "rough_high": rough_high,
        "metallic": metallic, "bump_strength": bump_strength,
        "bump_distance": bump_distance, "assign": assign,
    }), response_format, title="Procedural material")


@mcp.tool(annotations=WRITE)
@guard
def blender_world_shader(
    type: Annotated[Literal["color", "gradient", "sky", "image"], Field(
        description="World shader type; 'image' sets an HDRI.")] = "sky",
    color: Annotated[list[float], Field(description="For type='color', RGB.")] = [0.05, 0.05, 0.06],
    top_color: Annotated[list[float], Field(description="Gradient top RGB.")] = [0.25, 0.35, 0.55],
    bottom_color: Annotated[list[float], Field(description="Gradient bottom RGB.")] = [0.02, 0.02, 0.03],
    angle: float = 90.0,
    sky_type: Annotated[Literal["NISHITA", "PREETHAM", "HOSEK_WILKIE"], Field(
        description="Physical sky model.")] = "NISHITA",
    sun_elevation: Annotated[float, Field(description="Degrees above horizon.")] = 25.0,
    sun_rotation: Annotated[float, Field(description="Degrees around Z.")] = 135.0,
    altitude: Annotated[float, Field(description="Metres, Nishita only.")] = 100.0,
    air_density: float = 1.0,
    path: Annotated[str, Field(
        description="For type='image', an .hdr or .exr file.")] = "",
    rotation: Annotated[float, Field(description="HDRI rotation in degrees.")] = 0.0,
    strength: float = 1.0,
    response_format: FORMAT = "markdown",
) -> Any:
    """Build the world shader: flat colour, vertical gradient, physical sky, or
    an HDRI image. Nishita with sun elevation gives a believable daylight
    environment without an HDRI download."""
    return respond(call("world_shader", {
        "type": type, "color": color, "top_color": top_color,
        "bottom_color": bottom_color, "angle": angle, "sky_type": sky_type,
        "sun_elevation": sun_elevation, "sun_rotation": sun_rotation,
        "altitude": altitude, "air_density": air_density, "path": path,
        "rotation": rotation, "strength": strength,
    }), response_format, title="World shader")


@mcp.tool(annotations=WRITE)
@guard
def blender_paint_vertex_colors(
    object: str,
    axis: Annotated[Literal["X", "Y", "Z"], Field(
        description="Axis to ramp along.")] = "Z",
    color_a: Annotated[list[float], Field(description="Colour at the low end.")] = [0, 0, 0],
    color_b: Annotated[list[float], Field(description="Colour at the high end.")] = [1, 1, 1],
    layer: Annotated[str, Field(description="Colour attribute name.")] = "Col",
    response_format: FORMAT = "markdown",
) -> Any:
    """Write per-vertex colours as a gradient along an axis. Useful as a mask,
    for toon shading, or for baking masks into a vertex colour layer."""
    return respond(call("paint_vertex_colors", {
        "object": object, "axis": axis, "color_a": color_a, "color_b": color_b,
        "layer": layer,
    }), response_format, title="Vertex colours")


@mcp.tool(annotations=READ)
@guard
def blender_material_report(limit: int = 200, response_format: FORMAT = "markdown") -> Any:
    """List every material with its users, the objects using it, node count,
    linked images and blend settings."""
    return respond(call("material_report", {"limit": limit}),
                   response_format, title="Materials")


# =========================================================================== #
# v3: assets from the internet, libraries, add-ons, packages
# =========================================================================== #


@mcp.tool(annotations=SLOW)
@guard
def blender_download(
    url: str,
    filename: Annotated[str, Field(description="Save as; defaults to the URL basename.")] = "",
    directory: Annotated[str, Field(
        description="Subfolder under ~/BlenderMCP_Assets.")] = "downloads",
    max_bytes: Annotated[int, Field(
        description="Size cap in bytes; default 256 MB.")] = 268_435_456,
    timeout: float = 60.0,
    sha256: Annotated[str, Field(
        description="Expected checksum; the call fails on mismatch.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Fetch a URL to a local file and report its size and SHA-256.

    Only http and https are allowed, the download is size-capped, and nothing
    downloaded is ever executed. Combine with `blender_import_asset` to pull a
    model straight onto the scene.
    """
    return respond(call("download", {
        "url": url, "filename": filename, "directory": directory,
        "max_bytes": max_bytes, "timeout": timeout, "sha256": sha256,
    }), response_format, title="Downloaded")


@mcp.tool(annotations=SLOW)
@guard
def blender_import_asset(
    url: Annotated[str, Field(description="Model URL to download first.")] = "",
    path: Annotated[str, Field(description="Or a local file path.")] = "",
    into_collection: Annotated[str, Field(
        description="Link the imported objects into this collection.")] = "",
    max_bytes: int = 268_435_456,
    timeout: float = 120.0,
    import_options: Annotated[dict, Field(
        description="Importer-specific options passed straight through, e.g. "
                    "{'global_scale': 0.01} for FBX or {'use_materials': True}.")] = {},
    response_format: FORMAT = "markdown",
) -> Any:
    """Import a 3D model from a URL or a local file, as separate editable objects.

    Handles fbx, obj, gltf, glb, stl, ply, usd, usdz, abc, dae and blend. A URL
    is downloaded first, respecting the size cap. Everything arrives as ordinary
    objects you can then validate, modify and export.
    """
    return respond(call("import_asset", {
        "url": url, "path": path, "into_collection": into_collection,
        "max_bytes": max_bytes, "timeout": timeout, "import_options": import_options,
    }), response_format, title="Asset imported")


@mcp.tool(annotations=SLOW)
@guard
def blender_export_asset(
    path: str,
    format: Annotated[str, Field(
        description="FBX, OBJ, GLTF, GLB, USD, STL or ALEMBIC. Inferred from the "
                    "extension when omitted.")] = "",
    objects: Annotated[list[str] | None, Field(
        description="Export only these; default is the selection.")] = None,
    export_options: Annotated[dict, Field(
        description="Exporter options passed straight through, e.g. "
                    "{'draco_mesh_compression_enable': True} for glTF.")] = {},
    response_format: FORMAT = "markdown",
) -> Any:
    """Export the scene or a named set of objects to a model file.

    Sensible defaults per format: fbx applies scale and bakes modifiers with
    materials embedded, glTF applies modifiers and exports textures, obj keeps
    normals and UVs. The call fails if the exporter reports success but no file
    appears, rather than claiming a lie.
    """
    return respond(call("export_asset", {
        "path": path, "format": format, "objects": objects,
        "export_options": export_options,
    }), response_format, title="Exported")


@mcp.tool(annotations=READ)
@guard
def blender_list_libraries(response_format: FORMAT = "markdown") -> Any:
    """List the asset libraries available for search and download."""
    return respond(call("list_libraries", {}), response_format, title="Libraries")


@mcp.tool(annotations=SLOW)
@guard
def blender_search_library(
    type: Annotated[Literal["hdris", "textures", "models"], Field(
        description="Asset category.")] = "hdris",
    query: Annotated[str, Field(description="Substring filter on name and tags.")] = "",
    library: Annotated[str, Field(description="Library id; default polyhaven.")] = "polyhaven",
    limit: int = 25,
    response_format: FORMAT = "markdown",
) -> Any:
    """Search a free CC0 asset library. Poly Haven needs no API key.

    Returns ids you pass to `blender_fetch_asset`. Useful for grabbing a real
    HDRI or a photogrammetry texture instead of hand-rolling one.
    """
    return respond(call("search_library", {
        "type": type, "query": query, "library": library, "limit": limit,
    }), response_format, title="Library search")


@mcp.tool(annotations=SLOW)
@guard
def blender_fetch_asset(
    id: str,
    type: Annotated[Literal["hdris", "textures", "models"], Field(
        description="Asset category, must match the search.")] = "hdris",
    resolution: Annotated[str, Field(
        description="1k, 2k, 4k, 8k for HDRIs; 1k/2k/4k for textures.")] = "1k",
    library: str = "polyhaven",
    set_as_world: Annotated[bool, Field(
        description="For HDRIs, wire it into the world shader automatically.")] = True,
    strength: float = 1.0,
    rotation: float = 0.0,
    into_collection: Annotated[str, Field(
        description="For models, import into this collection.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Download an asset from a library by id, and optionally use it.

    An HDRI becomes the world lighting in one call, which is the fastest route
    to a believable render without any HDRI hunting.
    """
    return respond(call("fetch_asset", {
        "id": id, "type": type, "resolution": resolution, "library": library,
        "set_as_world": set_as_world, "strength": strength, "rotation": rotation,
        "into_collection": into_collection,
    }), response_format, title="Asset fetched")


@mcp.tool(annotations=READ)
@guard
def blender_list_addons(response_format: FORMAT = "markdown") -> Any:
    """List every available Blender add-on with its enabled state and version."""
    return respond(call("addons_list", {}), response_format, title="Add-ons")


@mcp.tool(annotations=WRITE)
@guard
def blender_manage_addon(
    addon: Annotated[str, Field(description="Add-on module name, e.g. io_scene_gltf2.")],
    action: Annotated[Literal["enable", "disable", "install"], Field(
        description="What to do.")] = "enable",
    path: Annotated[str, Field(description="For install, a local .zip.")] = "",
    url: Annotated[str, Field(description="For install, a URL to a .zip.")] = "",
    module: Annotated[str, Field(
        description="Module name to enable after install, if it differs from the file.")] = "",
    enable: Annotated[bool, Field(description="Enable right after installing.")] = True,
    confirm: Annotated[bool, Field(
        description="Required for install: it executes third-party code.")] = False,
    response_format: FORMAT = "markdown",
) -> Any:
    """Enable, disable or install a Blender add-on.

    Installing requires `confirm=true` because it runs third-party code inside
    Blender. Only .zip archives are accepted, and the call is refused without
    that flag so an agent cannot silently install anything.
    """
    return respond(call("addons_manage", {
        "addon": addon, "action": action, "path": path, "url": url,
        "module": module, "enable": enable, "confirm": confirm,
    }), response_format, title="Add-on")


@mcp.tool(annotations=READ)
@guard
def blender_list_packages(response_format: FORMAT = "markdown") -> Any:
    """List the Python packages Blender's own interpreter can see, with sys.path
    and the project-local directory new packages go into."""
    return respond(call("packages_list", {}), response_format, title="Packages")


@mcp.tool(annotations=SLOW)
@guard
def blender_install_package(
    package: Annotated[str, Field(description="Requirement, e.g. 'scipy>=1.11'.")],
    confirm: Annotated[bool, Field(
        description="Required: pip install downloads and executes code.")] = False,
    no_deps: bool = False,
    timeout: float = 600.0,
    response_format: FORMAT = "markdown",
) -> Any:
    """pip install a package into a project-local directory.

    Packages go to their own directory, never into Blender's bundled
    site-packages, so a bad dependency cannot break the application. Refused
    without `confirm=true`.
    """
    return respond(call("packages_install", {
        "package": package, "confirm": confirm, "no_deps": no_deps,
        "timeout": timeout,
    }), response_format, title="Package installed")


@mcp.tool(annotations=WRITE)
@guard
def blender_append_node_group(
    path: str,
    names: Annotated[list[str] | None, Field(
        description="Only append these node group names.")] = None,
    assign_to_material: Annotated[str, Field(
        description="Material whose existing group node should use the first one.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Append shader or geometry node groups from another .blend file, so you can
    reuse an existing shader library instead of rebuilding graphs by hand."""
    return respond(call("append_node_group", {
        "path": path, "names": names, "assign_to_material": assign_to_material,
    }), response_format, title="Node groups appended")


# =========================================================================== #
# v3: animation, rigging, sequencing
# =========================================================================== #


@mcp.tool(annotations=READ)
@guard
def blender_list_actions(response_format: FORMAT = "markdown") -> Any:
    """List every action with its frame range, user count, slot names and how
    many objects use it."""
    return respond(call("actions_list", {}), response_format, title="Actions")


@mcp.tool(annotations=WRITE)
@guard
def blender_manage_action(
    op: Annotated[Literal["create", "assign", "rename", "copy", "remove"], Field(
        description="What to do.")] = "create",
    action: Annotated[str, Field(description="Action name, or new name for rename.")] = "",
    object: Annotated[str, Field(description="Object for create/assign.")] = "",
    new_name: Annotated[str, Field(description="Name for rename/copy.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Create, assign, rename, copy or remove an action.

    Handles Blender 4.4+ slotted actions, creating the slot up front so keys can
    be inserted immediately afterwards.
    """
    return respond(call("action_manage", {
        "op": op, "action": action, "object": object, "new_name": new_name,
    }), response_format, title="Action")


@mcp.tool(annotations=WRITE)
@guard
def blender_keyframe_channel(
    object: str,
    data_path: Annotated[str, Field(
        description="'location', 'rotation_euler', 'scale', or a full RNA path such "
                    "as 'modifiers[\"Subsurf\"].levels'.")] = "location",
    frame: int = 1,
    index: Annotated[int | None, Field(
        description="Array index; omit to key all components.")] = None,
    frame_end: Annotated[int | None, Field(
        description="Fill keys from `frame` to here.")] = None,
    step: int = 1,
    group: str = "",
    keyframe_type: Annotated[Literal["KEYFRAME", "BREAKDOWN", "MOVING_HOLD",
                                      "EXTREME", "JITTER"], Field(
        description="Keyframe type for Blender's animation types.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Insert keyframes on any data path, including modifier levels and custom
    properties, optionally filling a whole frame range.

    Broader than the original keyframe tool, which only handled the three
    standard transforms.
    """
    return respond(call("keyframe_channel", {
        "object": object, "data_path": data_path, "frame": frame, "index": index,
        "frame_end": frame_end, "step": step, "group": group,
        "keyframe_type": keyframe_type,
    }), response_format, title="Keyframes inserted")


@mcp.tool(annotations=WRITE)
@guard
def blender_remove_keyframes(
    object: str,
    data_path: str = "location",
    frame_start: int = 0,
    frame_end: int = 10000,
    all: Annotated[bool, Field(
        description="Clear the whole action instead of a frame range.")] = False,
    response_format: FORMAT = "markdown",
) -> Any:
    """Delete keyframes from a data path over a frame range, or clear the whole
    animation data on an object."""
    return respond(call("keyframe_remove", {
        "object": object, "data_path": data_path, "frame_start": frame_start,
        "frame_end": frame_end, "all": all,
    }), response_format, title="Keyframes removed")


@mcp.tool(annotations=WRITE)
@guard
def blender_curves(
    op: Annotated[Literal["list", "interpolation", "handles", "add_modifier",
                          "remove_modifier", "shift", "scale_values"], Field(
        description="What to do with the F-curves.")],
    object: str,
    interpolation: Annotated[Literal["CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD",
                                     "CUBIC", "QUART", "QUINT", "EXPO", "CIRC",
                                     "BACK", "BOUNCE", "ELASTIC"], Field(
        description="For op='interpolation'.")] = "BEZIER",
    easing: Annotated[Literal["AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT",
                               "AUTO_CLAMPED"], Field(
        description="Easing for the non-Bezier modes.")] = "AUTO",
    handle_type: Annotated[Literal["FREE", "AUTO", "VECTOR", "ALIGNED",
                                   "AUTO_CLAMPED"], Field(
        description="For op='handles'.")] = "AUTO_CLAMPED",
    modifier: Annotated[str, Field(
        description="For op='add_modifier': CYCLES, NOISE, GENERATOR, LIMITS, "
                    "STEPPED, GENERATOR.")] = "CYCLES",
    properties: Annotated[dict, Field(
        description="Modifier properties, e.g. {'mode_before': 'REPEAT'}.")] = {},
    data_path: Annotated[str, Field(
        description="Restrict to one F-curve data path.")] = "",
    frames: Annotated[float, Field(description="For op='shift', frames to move by.")] = 0.0,
    factor: Annotated[float, Field(description="For op='scale_values'.")] = 1.0,
    response_format: FORMAT = "markdown",
) -> Any:
    """Inspect and shape the F-curves of an object's action.

    This is where animation gets its character rather than just its timing:
    interpolation and easing, handle types, noise and cyclic modifiers, retiming
    the whole curve, or scaling its values.
    """
    return respond(call("curves", {
        "op": op, "object": object, "interpolation": interpolation, "easing": easing,
        "handle_type": handle_type, "modifier": modifier, "properties": properties,
        "data_path": data_path, "frames": frames, "factor": factor,
    }), response_format, title="F-curves")


@mcp.tool(annotations=WRITE)
@guard
def blender_nla(
    op: Annotated[Literal["list", "push", "mute", "remove"], Field(
        description="What to do with non-linear animation.")],
    object: str,
    action: Annotated[str, Field(description="For op='push', the action to push.")] = "",
    track: Annotated[str, Field(description="Track name.")] = "",
    frame_start: int = 1,
    frame_end: int = 0,
    blend_type: Annotated[Literal["REPLACE", "ADD", "SUBTRACT", "MULTIPLY"], Field(
        description="How the strip blends with lower tracks.")] = "REPLACE",
    value: Annotated[bool, Field(description="For op='mute'.")] = True,
    response_format: FORMAT = "markdown",
) -> Any:
    """Drive non-linear animation: push an action onto its own track so the
    active action stops driving the pose, then layer, blend and mute strips."""
    return respond(call("nla", {
        "op": op, "object": object, "action": action, "track": track,
        "frame_start": frame_start, "frame_end": frame_end,
        "blend_type": blend_type, "value": value,
    }), response_format, title="NLA")


@mcp.tool(annotations=WRITE)
@guard
def blender_drivers(
    op: Annotated[Literal["add", "list", "remove"], Field(
        description="What to do with drivers.")] = "add",
    object: str = "",
    data_path: str = "location",
    index: Annotated[int | None, Field(description="Array index.")] = None,
    type: Annotated[Literal["SCRIPTED", "AVERAGE", "SUM", "DIFFERENCE", "PRODUCT",
                            "MINIMUM", "MAXIMUM", "MODULO"], Field(
        description="Driver type for op='add'.")] = "SCRIPTED",
    expression: Annotated[str, Field(
        description="Python expression for op='add', e.g. 'frame * 0.1' or "
                    "'sin(frame/10)'.")] = "frame * 0.1",
    variables: Annotated[list[dict], Field(
        description="Driver variables: {'name', 'type', 'id_type', 'object', "
                    "'target'}. Defaults to a single 'frame' property.")] = [],
    response_format: FORMAT = "markdown",
) -> Any:
    """Add, list or remove drivers with real typed variables and expressions.

    Useful for procedural motion that should not be baked: spin a wheel from the
    frame counter, bob something with a sine, or link a value to a custom
    property.
    """
    return respond(call("driver", {
        "op": op, "object": object, "data_path": data_path, "index": index,
        "type": type, "expression": expression, "variables": variables,
    }), response_format, title="Drivers")


@mcp.tool(annotations=WRITE)
@guard
def blender_shape_keys(
    op: Annotated[Literal["list", "add", "set", "deform", "remove"], Field(
        description="What to do with shape keys.")] = "list",
    object: str = "",
    key: Annotated[str, Field(description="Shape key name.")] = "",
    value: Annotated[float, Field(description="For op='set', the slider value.")] = 0.0,
    min: float = 0.0,
    max: float = 1.0,
    offset: Annotated[list[float], Field(
        description="For op='deform', a translation to add.")] = [0, 0, 0],
    vertices: Annotated[list[int], Field(
        description="For op='deform', only move these vertex indices.")] = [],
    from_mix: bool = False,
    response_format: FORMAT = "markdown",
) -> Any:
    """Create and drive shape keys: facial blends, damage states, LOD morphs or
    simple deformation without touching the base mesh."""
    return respond(call("shape_keys", {
        "op": op, "object": object, "key": key, "value": value, "min": min,
        "max": max, "offset": offset, "vertices": vertices, "from_mix": from_mix,
    }), response_format, title="Shape keys")


@mcp.tool(annotations=SLOW)
@guard
def blender_simulate(
    op: Annotated[Literal["step", "bake", "reset"], Field(
        description="Step, bake to keyframes, or reset the cache.")] = "step",
    objects: Annotated[list[str] | None, Field(
        description="Objects to bake or reset.")] = None,
    frames: int = 60,
    frame_start: int = 0,
    response_format: FORMAT = "markdown",
) -> Any:
    """Step a physics simulation forward, bake it to keyframes, or reset the
    point cache. Bake is how a cloth or soft-body result becomes a usable
    animation rather than a live simulation."""
    return respond(call("simulate", {
        "op": op, "objects": objects, "frames": frames, "frame_start": frame_start,
    }), response_format, title="Simulation")


@mcp.tool(annotations=SLOW)
@guard
def blender_sequencer(
    op: Annotated[Literal["list", "add", "set_range", "render", "remove"], Field(
        description="What to do in the video sequencer.")] = "list",
    path: Annotated[str, Field(description="For op='add', an image or movie file.")] = "",
    name: Annotated[str, Field(description="Strip name.")] = "clip",
    channel: int = 1,
    frame_start: int = 1,
    frame_end: int = 0,
    start: int = 1,
    end: int = 250,
    mode: Annotated[Literal["MOVIE", "PNG", "OPEN_EXR", "FFMPEG"], Field(
        description="For op='render'.")] = "MOVIE",
    output: Annotated[str, Field(description="Output path or directory.")] = "",
    response_format: FORMAT = "markdown",
) -> Any:
    """Drive the video sequencer: add movie or image strips to channels, set the
    frame range, and render the sequence out to a movie or image sequence.

    This is the route to an actual rendered video rather than a single still.
    """
    return respond(call("sequencer", {
        "op": op, "path": path, "name": name, "channel": channel,
        "frame_start": frame_start, "frame_end": frame_end, "start": start,
        "end": end, "mode": mode, "output": output,
    }), response_format, title="Sequencer")


@mcp.tool(annotations=WRITE)
@guard
def blender_camera_move(
    mode: Annotated[Literal["orbit", "constraint", "dolly"], Field(
        description="Orbit turntable, follow constraint, or dolly move.")] = "orbit",
    camera: Annotated[str, Field(
        description="Camera name; defaults to the active camera.")] = "",
    pivot: Annotated[list[float], Field(description="Rotation/dolly pivot.")] = [0, 0, 0],
    frames: int = 8,
    frame_start: int = 1,
    full_turn: Annotated[bool, Field(
        description="For orbit, a full 360 degrees or a sweep.")] = True,
    radius: Annotated[float, Field(description="Orbit radius; from the camera.")] = 0.0,
    target: Annotated[str, Field(
        description="For constraint, the object to follow.")] = "",
    constraint: Annotated[Literal["TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK",
                                    "COPY_LOCATION", "COPY_ROTATION", "FOLLOW_PATH"],
                          Field(description="For constraint.")] = "TRACK_TO",
    track_axis: str = "TRACK_NEGATIVE_Z",
    up_axis: str = "UP_Y",
    frame_end: int = 48,
    response_format: FORMAT = "markdown",
) -> Any:
    """Animate the camera: a keyed orbit turntable, a follow constraint, or a
    two-point dolly.

    The orbit mode is the quickest route to a review turntable around a model -
    keys and constraints both, so it renders deterministically.
    """
    return respond(call("camera_move", {
        "mode": mode, "camera": camera, "pivot": pivot, "frames": frames,
        "frame_start": frame_start, "full_turn": full_turn, "radius": radius,
        "target": target, "constraint": constraint, "track_axis": track_axis,
        "up_axis": up_axis, "frame_end": frame_end,
    }), response_format, title="Camera animated")


@mcp.tool(annotations=WRITE)
@guard
def blender_timeline(
    op: Annotated[Literal["report", "set", "add_marker", "remove_marker"], Field(
        description="What to do.")] = "report",
    start: int = 1,
    end: int = 250,
    fps: int = 24,
    fps_base: float = 1.0,
    step: int = 1,
    name: Annotated[str, Field(description="Marker name.")] = "Marker",
    frame: int = 1,
    response_format: FORMAT = "markdown",
) -> Any:
    """Report or set the frame range, fps and step, and manage timeline markers."""
    return respond(call("timeline", {
        "op": op, "start": start, "end": end, "fps": fps, "fps_base": fps_base,
        "step": step, "name": name, "frame": frame,
    }), response_format, title="Timeline")


# =========================================================================== #
# v3: project inspection
# =========================================================================== #


@mcp.tool(annotations=READ)
@guard
def blender_settings_report(
    groups: Annotated[list[str] | None, Field(
        description="Subset of scene, render, data, preferences, files, handlers. "
                    "Default is all of them.")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Report what the project is actually set to.

    Covers the scene, the full render configuration including Cycles and EEVEE
    sampling and colour management, datablock counts, user preferences, file
    paths, linked libraries and registered handlers. This is the answer to
    "what are the current settings".
    """
    return respond(call("settings_report", {"groups": groups}),
                   response_format, title="Project settings")


@mcp.tool(annotations=WRITE)
@guard
def blender_set_setting(
    group: Annotated[Literal["render", "scene", "cycles", "eevee", "view",
                             "preferences", "unit", "world"], Field(
        description="Which settings root to change.")],
    path: Annotated[str, Field(
        description="Dotted attribute path inside that root, e.g. 'resolution_x' "
                    "or 'frame_end'.")],
    value: Annotated[Any, Field(description="New value.")],
    response_format: FORMAT = "markdown",
) -> Any:
    """Change any single project setting by dotted path, and report the old and
    new value.

    Deliberately one setting per call: a bulk setter would let a wrong path
    silently wreck a whole configuration.
    """
    return respond(call("settings_set", {"group": group, "path": path, "value": value}),
                   response_format, title="Setting changed")


@mcp.tool(annotations=READ)
@guard
def blender_blend_contents(
    types: Annotated[list[str] | None, Field(
        description="Datablock types to include, e.g. ['meshes', 'materials'].")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Inventory every datablock in the file by type, with user counts, orphans
    and which library each came from. The fastest way to see what a .blend
    actually contains before merging or cleaning it."""
    return respond(call("blend_contents", {"types": types}),
                   response_format, title="Blend contents")


@mcp.tool(annotations=READ)
@guard
def blender_scripts_and_texts(response_format: FORMAT = "json") -> Any:
    """List embedded Text datablocks and every .py file inside Blender's script
    paths, with sizes. Use it to find an add-on's or a script's real location."""
    return respond(call("scripts_and_texts", {}), response_format, title="Scripts")


@mcp.tool(annotations=READ)
@guard
def blender_filesystem(
    path: Annotated[str, Field(
        description="Directory to list. Omit to see the allowed roots.")] = "",
    pattern: Annotated[str, Field(description="Glob filter, e.g. '*.hdr'.")] = "*",
    roots: Annotated[list[str] | None, Field(
        description="Override the allowed roots for this call.")] = None,
    allow_anywhere: Annotated[bool, Field(
        description="Drop the root restriction. Off by default on purpose: an "
                    "agent that can read the whole disk is a liability.")] = False,
    limit: int = 300,
    response_format: FORMAT = "json",
) -> Any:
    """List or search files, restricted to the user's home directory, Blender's
    script folders and the asset cache by default.

    Pass `allow_anywhere` only when the user has actually asked for it.
    """
    return respond(call("filesystem", {
        "path": path, "pattern": pattern, "roots": roots,
        "allow_anywhere": allow_anywhere, "limit": limit,
    }), response_format, title="Filesystem")


@mcp.tool(annotations=READ)
@guard
def blender_python_env(
    modules: Annotated[list[str] | None, Field(
        description="Module names to test for importability, e.g. ['numpy','scipy'].")] = None,
    response_format: FORMAT = "json",
) -> Any:
    """Report Blender's interpreter, sys.path, script paths and whether given
    modules can be imported. Use it before relying on a library."""
    return respond(call("python_env", {"modules": modules}),
                   response_format, title="Python environment")


@mcp.tool(annotations=READ)
@guard
def blender_render_report(response_format: FORMAT = "json") -> Any:
    """Report what a render would actually use: engine, effective resolution,
    frame range, resolved output path and whether that directory exists, colour
    management, camera and lights."""
    return respond(call("render_report", {}), response_format, title="Render report")


@mcp.tool(annotations=READ)
@guard
def blender_diagnose(response_format: FORMAT = "markdown") -> Any:
    """Quick health check on the project: missing camera, unpacked textures,
    orphan datablocks, missing linked libraries, meshes without materials,
    or Blender running headless.

    Cheaper than a full validate when you just want to know what is wrong.
    """
    return respond(call("diagnose", {}), response_format, title="Diagnosis")


def main() -> None:  # pragma: no cover
    port = os.environ.get("BLENDER_MCP_PORT")
    if port:
        BRIDGE.port = int(port)
    mcp.run(transport="stdio")
    BRIDGE.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
