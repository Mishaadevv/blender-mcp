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

Typical flow:
1. `blender_setup`   - ONCE, if `blender_status` says not connected. Copies the
   bundled addon into Blender's user addons folder and enables it. Ask the user
   to restart Blender afterwards. Skip it entirely if Blender is already
   connected.
2. `blender_status`  - confirm the bridge is online, note the open .blend file.
3. `blender_get_scene` - survey the existing scene before touching it.
4. Build with `blender_add_primitive` / `blender_create_mesh` /
   `blender_edit_mesh` / `blender_add_modifier` / `blender_create_material`.
5. `blender_look_at` + `blender_set_render_settings` + `blender_render`, or
   `blender_capture_viewport` for a fast visual check.
6. `blender_save_blend` to persist.

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
    version="1.0.0",
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


def main() -> None:  # pragma: no cover
    port = os.environ.get("BLENDER_MCP_PORT")
    if port:
        BRIDGE.port = int(port)
    mcp.run(transport="stdio")
    BRIDGE.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
