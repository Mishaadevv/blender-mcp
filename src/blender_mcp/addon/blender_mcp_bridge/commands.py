"""Command implementations executed on Blender's main thread.

Every handler takes a plain dict of parameters and returns a JSON friendly
dict. Handlers raise :class:`bridge.CommandError` for actionable user errors.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import random
import sys
import tempfile
import time
import traceback

import bmesh
import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from .bridge import CommandError, view3d_override
from . import ops as _ops
from . import textures as _tex
from . import validate as _val

# --------------------------------------------------------------------------- #
# generic helpers
# --------------------------------------------------------------------------- #

OBJECT_TYPES = {
    "EMPTY", "MESH", "CURVE", "SURFACE", "FONT", "META", "CURVE",
    "POINTCLOUD", "VOLUME", "GPENCIL", "GREASEPENCIL", "ARMATURE", "LATTICE",
    "LIGHT", "LIGHT_PROBE", "CAMERA", "SPEAKER", "BONE", "COLLECTION",
}

PRIMITIVES = {
    "cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "circle", "grid",
    "torus", "plane", "monkey",
}

#: Names that map to a ``bpy.ops.mesh.primitive_<name>_add`` operator.
_PY_PRIMITIVES = {
    "cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "plane", "torus",
}

#: Friendly aliases accepted from callers.
_PRIMITIVE_ALIASES = {
    "sphere": "uv_sphere",
    "icosphere": "ico_sphere",
    "circle": "circle",
    "grid": "grid",
    "monkey": "monkey",
}


def _path_base_dir() -> str:
    return os.path.expanduser(
        os.environ.get("BLENDER_MCP_ROOT", os.path.expanduser("~"))
    )


def resolve_path(path: str, must_exist: bool = False) -> str:
    if not isinstance(path, str) or not path.strip():
        raise CommandError("A non-empty file path is required.")
    expanded = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(_path_base_dir(), expanded)
    expanded = os.path.normpath(expanded)
    roots = os.environ.get("BLENDER_MCP_ALLOWED_ROOTS", "").strip()
    if roots:
        allowed = [os.path.normpath(os.path.expanduser(r)) for r in roots.split(os.pathsep)]
        if not any(expanded == r or expanded.startswith(r + os.sep) for r in allowed):
            raise CommandError(
                f"Path {expanded!r} is outside the allowed roots "
                f"({os.pathsep.join(allowed)}). Set BLENDER_MCP_ALLOWED_ROOTS to change."
            )
    if must_exist and not os.path.exists(expanded):
        raise CommandError(
            f"File not found: {expanded}. Paths are resolved relative to "
            f"{_path_base_dir()!r} unless absolute."
        )
    parent = os.path.dirname(expanded)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise CommandError(f"Cannot create directory {parent!r}: {exc}") from exc
    return expanded


def obj_of(name: str, expected_type: str | None = None) -> bpy.types.Object:
    if not name:
        raise CommandError("An object name is required.")
    ob = bpy.data.objects.get(name)
    if ob is None:
        close = [o.name for o in bpy.data.objects if name.lower() in o.name.lower()][:8]
        hint = f" Did you mean: {close}?" if close else (
            " Use blender_list_objects to see what is in the scene."
        )
        raise CommandError(f"Object {name!r} not found.{hint}")
    if expected_type and ob.type != expected_type:
        raise CommandError(
            f"Object {name!r} is a {ob.type}, not a {expected_type}. "
            "Blender cannot perform this operation on that type."
        )
    return ob


def objs_of(names, expected_type=None) -> list:
    if names is None:
        raise CommandError("A non-empty list of object names is required.")
    if isinstance(names, str):
        names = [names]
    result = [obj_of(n, expected_type) for n in names]
    if not result:
        raise CommandError("No objects matched.")
    return result


def get_collection(name: str, create: bool = False) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if col is None and create:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def link_only(ob: bpy.types.Object, collection) -> None:
    for col in list(ob.users_collection):
        col.objects.unlink(ob)
    collection.objects.link(ob)


@contextlib.contextmanager
def active_objects(objects, mode: str | None = None):
    """Temporarily make ``objects`` the active+selected set in object mode.

    Objects created through ``bpy.data.objects.new()`` are not registered in the
    view layer until it is refreshed, and ``select_set`` raises on them. Without
    the update the selection silently fails and operators like join do nothing
    at all, so the state is verified and a failure is reported loudly.
    """
    view_layer = bpy.context.view_layer
    view_layer.update()
    previous = view_layer.objects.active
    previous_mode = previous.mode if previous is not None else None
    if previous_mode not in (None, "OBJECT") and bpy.context.mode != "OBJECT":
        with contextlib.suppress(Exception):
            bpy.ops.object.mode_set(mode="OBJECT")
    for other in bpy.data.objects:
        with contextlib.suppress(RuntimeError):
            other.select_set(False)
    unselectable = []
    for item in objects:
        try:
            item.select_set(True)
        except RuntimeError as exc:
            unselectable.append(f"{item.name} ({exc})")
    if unselectable:
        raise CommandError(
            "These objects could not be selected, so the operation was aborted: "
            + "; ".join(unselectable)
        )
    if objects:
        view_layer.objects.active = objects[0]
    if mode:
        with contextlib.suppress(RuntimeError):
            bpy.ops.object.mode_set(mode=mode)
    try:
        yield
    finally:
        if mode == "EDIT" and bpy.context.mode == "EDIT":
            with contextlib.suppress(Exception):
                bpy.ops.object.mode_set(mode="OBJECT")
        if previous is not None and objects:
            with contextlib.suppress(Exception):
                view_layer.objects.active = previous
        with contextlib.suppress(Exception):
            view_layer.update()


@contextlib.contextmanager
def edit_bmesh(ob: bpy.types.Object):
    """Yield ``(bmesh, geom_helper)`` with ``ob`` in edit mode."""
    if ob.type != "MESH":
        raise CommandError(f"Object {ob.name!r} is a {ob.type}; edit ops need a mesh.")
    with active_objects([ob], mode="EDIT"):
        bm = bmesh.from_edit_mesh(ob.data)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        try:
            yield bm
        finally:
            bmesh.update_edit_mesh(ob.data, loop_triangles=False, destructive=False)
            with contextlib.suppress(Exception):
                bpy.ops.object.mode_set(mode="OBJECT")


def selection_geom(bm, region: str = "selected"):
    if region == "all":
        return list(bm.verts) + list(bm.edges) + list(bm.faces)
    verts = [v for v in bm.verts if v.select]
    edges = [e for e in bm.edges if e.select]
    faces = [f for f in bm.faces if f.select]
    if not (verts or edges or faces):
        verts, edges, faces = list(bm.verts), list(bm.edges), list(bm.faces)
    return verts + edges + faces


def bm_elements(bm, context: str):
    """The element sequence matching a bmesh select mode ('VERT'/'EDGE'/'FACE')."""
    return {"VERT": bm.verts, "EDGE": bm.edges, "FACE": bm.faces}[context]


def bm_counts(bm) -> dict:
    return {"vertices": len(bm.verts), "edges": len(bm.edges), "faces": len(bm.faces)}


def _bmesh_call(bm, name: str, **kwargs):
    """Call a bmesh op, dropping keyword arguments this Blender build lacks."""
    op = getattr(bmesh.ops, name, None)
    if op is None:
        raise CommandError(
            f"bmesh.ops.{name} is not available in Blender {bpy.app.version_string}."
        )
    while True:
        try:
            return op(bm, **kwargs)
        except TypeError as exc:
            message = str(exc)
            dropped = None
            for key in list(kwargs):
                if key in message:
                    dropped = key
                    break
            if dropped is None:
                raise CommandError(f"bmesh.ops.{name} failed: {message}") from exc
            kwargs.pop(dropped)


def enum_options(owner, name: str) -> list:
    """Best effort enum options for a (possibly dynamic) RNA enum property."""
    try:
        prop = owner.bl_rna.properties[name]
    except (KeyError, AttributeError):
        return []
    for source in (prop, owner.bl_rna.properties.get(name)):
        if source is None:
            continue
        try:
            items = [i.identifier for i in source.enum_items]
            if items:
                return items
        except (AttributeError, TypeError):
            pass
    return []


RENDER_ENGINES = {
    "eevee": "BLENDER_EEVEE_NEXT",
    "eevee_next": "BLENDER_EEVEE_NEXT",
    "cycles": "CYCLES",
    "workbench": "BLENDER_WORKBENCH",
    "solid": "BLENDER_WORKBENCH",
}

#: `RenderSettings.engine` is a dynamic enum whose items Python cannot enumerate
#: reliably (it reports only the built-in EEVEE in 4.5 even with Cycles loaded), so
#: validity is established by attempting the assignment instead.
KNOWN_ENGINES = ("CYCLES", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH")


def set_render_engine(scene, value: str) -> dict:
    """Assign the render engine, mapping friendly names. Returns before/after."""
    key = str(value).strip().lower().replace(" ", "_")
    candidate = RENDER_ENGINES.get(key, str(value))
    before = scene.render.engine
    try:
        scene.render.engine = candidate
    except TypeError as exc:
        scene.render.engine = before
        raise CommandError(
            f"Unknown render engine {value!r}. "
            f"Use one of: {list(KNOWN_ENGINES)}, or the full identifier reported "
            f"by blender_get_scene (currently {before!r}). Blender said: {exc}"
        ) from exc
    if scene.render.engine != candidate:
        raise CommandError(
            f"Blender accepted {candidate!r} but the engine is now "
            f"{scene.render.engine!r} - that engine is not built into this Blender."
        )
    return {"from": before, "to": scene.render.engine}


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #


def vec(v) -> list:
    return [round(float(c), 6) for c in v]


def mesh_stats(me: bpy.types.Mesh | None) -> dict:
    if me is None:
        return {"vertices": 0, "edges": 0, "faces": 0, "polygons": 0}
    return {
        "vertices": len(me.vertices),
        "edges": len(me.edges),
        "faces": len(me.polygons),
        "polygons": len(me.polygons),
        "shade_smooth": bool(me.polygons and me.polygons[0].use_smooth),
    }


def obj_summary(ob: bpy.types.Object, detailed: bool = False) -> dict:
    data = {
        "name": ob.name,
        "type": ob.type,
        "data": getattr(ob.data, "name", None) if ob.data else None,
        "location": vec(ob.location),
        "rotation_euler_deg": [round(math.degrees(a), 4) for a in ob.rotation_euler],
        "scale": vec(ob.scale),
        "dimensions": vec(ob.dimensions),
        "collections": [c.name for c in ob.users_collection],
        "parent": ob.parent.name if ob.parent else None,
        "children": [c.name for c in ob.children],
        "visible": ob.visible_get() if ob.name in bpy.context.view_layer.objects else ob.hide_viewport is False,
        "hide_viewport": ob.hide_viewport,
        "hide_render": ob.hide_render,
        "selected": bool(ob.select_get()),
        "active": bpy.context.view_layer.objects.active is ob,
        "materials": [m.name if m else None for m in ob.data.materials] if getattr(ob.data, "materials", None) else [],
        "modifiers": [
            {"name": m.name, "type": m.type, "show_viewport": m.show_viewport, "show_render": m.show_render}
            for m in getattr(ob, "modifiers", [])
        ],
        "constraints": [c.type for c in getattr(ob, "constraints", [])],
    }
    if ob.type == "MESH":
        data["mesh"] = mesh_stats(ob.data)
    if ob.type == "LIGHT":
        data["light"] = {
            "type": ob.data.type,
            "energy": round(ob.data.energy, 4),
            "color": vec(ob.data.color),
            "size": round(getattr(ob.data, "size", 0.0), 4),
        }
    if ob.type == "CAMERA":
        data["camera"] = {
            "lens": round(ob.data.lens, 3),
            "fov_deg": round(math.degrees(ob.data.angle), 3),
            "clip_start": round(ob.data.clip_start, 4),
            "clip_end": round(ob.data.clip_end, 2),
        }
    if detailed:
        data["matrix_world"] = [vec(row) for row in ob.matrix_world]
        data["custom_properties"] = {
            k: ob[k] for k in ob.keys() if not k.startswith("_")
        }
        if ob.type == "MESH":
            data["shape_keys"] = [k.name for k in ob.data.shape_keys.key_blocks] if ob.data.shape_keys else []
            data["vertex_groups"] = [g.name for g in ob.vertex_groups]
    return data


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_ping(params):
    scene = bpy.context.scene
    return {
        "ok": True,
        "blender_version": bpy.app.version_string,
        "version": list(bpy.app.version),
        "binary": bpy.app.binary_path,
        "background": bpy.app.background,
        "filepath": bpy.data.filepath,
        "is_dirty": bpy.data.is_dirty,
        "mode": bpy.context.mode,
        "scene": scene.name,
        "engine": scene.render.engine,
        "known_engines": list(KNOWN_ENGINES),
        "python": sys.version.split()[0],
        "script_directory": bpy.utils.user_resource("SCRIPTS"),
        "addons_directory": bpy.utils.user_resource("SCRIPTS", path="addons", create=True),
    }


def cmd_get_scene(params):
    scene = bpy.context.scene
    detailed = bool(params.get("detailed"))
    objects = [obj_summary(o, detailed) for o in scene.objects]
    collections = []

    def walk(col, depth=0):
        collections.append(
            {
                "name": col.name,
                "objects": len(col.objects),
                "children": [c.name for c in col.children],
                "depth": depth,
            }
        )
        for child in col.children:
            walk(child, depth + 1)

    walk(scene.collection)
    return {
        "name": scene.name,
        "filepath": bpy.data.filepath,
        "is_dirty": bpy.data.is_dirty,
        "frame": {
            "current": scene.frame_current,
            "start": scene.frame_start,
            "end": scene.frame_end,
            "step": scene.frame_step,
            "fps": scene.render.fps,
            "fps_base": scene.render.fps_base,
        },
        "render": {
            "engine": scene.render.engine,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "resolution_percentage": scene.render.resolution_percentage,
            "film_transparent": scene.render.film_transparent,
            "filepath": scene.render.filepath,
            "image_format": scene.render.image_settings.file_format,
        },
        "world": {
            "name": scene.world.name if scene.world else None,
            "color": vec(scene.world.color) if scene.world else None,
        },
        "unit_system": scene.unit_settings.system,
        "scale_length": scene.unit_settings.scale_length,
        "gravity": vec(scene.gravity),
        "active_object": scene.view_layers[0].objects.active.name
        if scene.view_layers[0].objects.active
        else None,
        "selected": [o.name for o in scene.objects if o.select_get()],
        "counts": {
            "objects": len(objects),
            "meshes": len(bpy.data.meshes),
            "materials": len(bpy.data.materials),
            "collections": len(bpy.data.collections),
            "images": len(bpy.data.images),
            "actions": len(bpy.data.actions),
            "armatures": len(bpy.data.armatures),
            "cameras": len(bpy.data.cameras),
            "lights": len(bpy.data.lights),
        },
        "collections": collections,
        "objects": objects,
        "materials": [m.name for m in bpy.data.materials],
        "meshes": [{"name": m.name, **mesh_stats(m), "users": m.users} for m in bpy.data.meshes],
        "images": [
            {"name": i.name, "filepath": i.filepath, "size": list(i.size), "source": i.source}
            for i in bpy.data.images
            if i.source == "FILE"
        ],
    }


def cmd_list_objects(params):
    import re

    scene = bpy.context.scene
    collection = params.get("collection")
    obj_type = params.get("type")
    pattern = params.get("name_pattern")
    parent = params.get("parent")
    detailed = bool(params.get("detailed"))
    limit = int(params.get("limit") or 200)
    offset = int(params.get("offset") or 0)

    pool = scene.objects
    if collection:
        col = get_collection(collection)
        if col is None:
            raise CommandError(
                f"Collection {collection!r} not found. Available: "
                f"{[c.name for c in bpy.data.collections]}"
            )
        pool = col.all_objects
    items = list(pool)
    if obj_type:
        wanted = {t.strip().upper() for t in str(obj_type).split(",")}
        unknown = wanted - OBJECT_TYPES
        if unknown:
            raise CommandError(
                f"Unknown object type(s) {sorted(unknown)}. Valid: {sorted(OBJECT_TYPES)}"
            )
        items = [o for o in items if o.type in wanted]
    if parent:
        if parent == "none":
            items = [o for o in items if o.parent is None]
        else:
            p = obj_of(parent)
            items = [o for o in items if o.parent is p]
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        items = [o for o in items if rx.search(o.name)]

    total = len(items)
    page = items[offset : offset + limit]
    return {
        "total": total,
        "count": len(page),
        "offset": offset,
        "has_more": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
        "objects": [obj_summary(o, detailed) for o in page],
    }


def cmd_get_object(params):
    return obj_summary(obj_of(params.get("name"), params.get("expect_type")), detailed=True)


def cmd_add_primitive(params):
    kind = str(params.get("type", "cube")).lower()
    kind = _PRIMITIVE_ALIASES.get(kind, kind)
    if kind not in PRIMITIVES:
        raise CommandError(
            f"Unknown primitive {params.get('type')!r}. "
            f"Choose one of: {sorted(PRIMITIVES)}"
        )
    location = Vector(params.get("location") or (0, 0, 0))
    rotation = [math.radians(a) for a in (params.get("rotation") or (0, 0, 0))]
    scale = Vector(params.get("scale") or (1, 1, 1))
    kwargs = {k: v for k, v in (params.get("parameters") or {}).items() if v is not None}

    with active_objects([]):
        operator = getattr(bpy.ops.mesh, f"primitive_{kind}_add") if kind in _PY_PRIMITIVES \
            else getattr(bpy.ops.mesh, kind)
        kwargs = coerce_all(operator.get_rna_type(), kwargs)
        # Not every primitive operator accepts scale (torus_add does not), so the
        # scale is applied to the resulting object instead.
        result = operator(location=location, rotation=rotation, **kwargs)
    ob = bpy.context.view_layer.objects.active
    if ob is None:
        raise CommandError(f"Operator returned {list(result)} and created no object.")
    if scale != Vector((1, 1, 1)):
        ob.scale = scale

    name = params.get("name")
    if name:
        ob.name = str(name)
        if ob.data:
            ob.data.name = str(name)
    if params.get("collection"):
        col = get_collection(str(params["collection"]), create=True)
        link_only(ob, col)
    if params.get("material"):
        assign_material_to(ob, str(params["material"]))
    if params.get("shade_smooth"):
        set_shade_smooth([ob], True, params.get("auto_smooth_angle"))
    return obj_summary(ob, detailed=True)


def cmd_create_mesh(params):
    name = str(params.get("name") or "Mesh")
    verts = params.get("vertices") or []
    faces = params.get("faces") or []
    if len(verts) < 3:
        raise CommandError("A mesh needs at least 3 vertices.")
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(v) for v in verts], [], [list(f) for f in faces])
    mesh.update()
    mesh.validate()
    ob = bpy.data.objects.new(name, mesh)
    col = get_collection(str(params.get("collection")), create=True) if params.get("collection") else bpy.context.scene.collection
    col.objects.link(ob)
    ob.location = Vector(params.get("location") or (0, 0, 0))
    if params.get("material"):
        assign_material_to(ob, str(params["material"]))
    bpy.context.view_layer.update()
    return obj_summary(ob, detailed=True)


def cmd_delete_objects(params):
    names = params.get("objects")
    if not names:
        raise CommandError("Provide the 'objects' to delete.")
    objs = objs_of(names)
    removed = [o.name for o in objs]
    for ob in objs:
        bpy.data.objects.remove(ob, do_unlink=True)
    return {"deleted": removed, "remaining_objects": len(bpy.context.scene.objects)}


def cmd_duplicate_objects(params):
    objs = objs_of(params.get("objects"))
    copies = []
    for ob in objs:
        new = ob.copy()
        if ob.data is not None:
            new.data = ob.data.copy()
        new.name = f"{ob.name}.001"
        for col in ob.users_collection:
            col.objects.link(new)
        copies.append(new)
    if params.get("collection"):
        col = get_collection(str(params["collection"]), create=True)
        for c in copies:
            link_only(c, col)
    return {"created": [obj_summary(c) for c in copies]}


def cmd_rename_object(params):
    ob = obj_of(params.get("object"))
    new_name = str(params.get("name") or "").strip()
    if not new_name:
        raise CommandError("A non-empty new name is required.")
    if new_name in bpy.data.objects and new_name != ob.name:
        raise CommandError(f"An object named {new_name!r} already exists.")
    old = ob.name
    ob.name = new_name
    if params.get("rename_data", True) and ob.data is not None:
        ob.data.name = new_name
    return {"renamed": {"from": old, "to": ob.name}}


def cmd_transform_objects(params):
    objs = objs_of(params.get("objects"))
    location = params.get("location")
    rotation = params.get("rotation")
    scale = params.get("scale")
    rotation_mode = params.get("rotation_mode")
    if location is None and rotation is None and scale is None:
        raise CommandError("Provide at least one of location, rotation or scale.")
    if params.get("relative"):
        if location is not None:
            location = [o.location[i] + location[i] for i in range(3)]
        if rotation is not None:
            rotation = [math.degrees(o.rotation_euler[i]) + rotation[i] for i in range(3)]
        if scale is not None:
            scale = [o.scale[i] * scale[i] for i in range(3)]
    if rotation_mode:
        mode = str(rotation_mode).upper()
        valid = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX", "QUATERNION", "AXIS_ANGLE"}
        if mode not in valid:
            raise CommandError(f"rotation_mode must be one of {sorted(valid)}")
    for ob in objs:
        if rotation_mode:
            ob.rotation_mode = mode
        if location is not None:
            ob.location = Vector(location)
        if rotation is not None:
            ob.rotation_euler = Euler([math.radians(a) for a in rotation], "XYZ")
        if scale is not None:
            ob.scale = Vector(scale)
    bpy.context.view_layer.update()
    return {"updated": [obj_summary(o) for o in objs]}


def cmd_apply_transform(params):
    objs = objs_of(params.get("objects"))
    location = bool(params.get("location", True))
    rotation = bool(params.get("rotation", True))
    scale = bool(params.get("scale", True))
    with active_objects(objs):
        bpy.ops.object.transform_apply(
            location=location, rotation=rotation, scale=scale
        )
    bpy.context.view_layer.update()
    return {"applied_to": [o.name for o in objs]}


def cmd_select_objects(params):
    names = params.get("objects")
    if names == "all":
        targets = list(bpy.context.scene.objects)
    elif names in (None, [], "none"):
        targets = []
    else:
        targets = objs_of(names)
    for ob in bpy.context.scene.objects:
        with contextlib.suppress(RuntimeError):
            ob.select_set(False)
    for ob in targets:
        ob.select_set(True)
    active = params.get("active")
    if active:
        bpy.context.view_layer.objects.active = obj_of(active)
    elif targets:
        bpy.context.view_layer.objects.active = targets[0]
    return {
        "selected": [o.name for o in bpy.context.scene.objects if o.select_get()],
        "active": bpy.context.view_layer.objects.active.name
        if bpy.context.view_layer.objects.active
        else None,
    }


def cmd_join_objects(params):
    objs = objs_of(params.get("objects"))
    if len(objs) < 2:
        raise CommandError("Joining needs at least two objects.")
    if any(o.type != "MESH" for o in objs):
        raise CommandError("Only mesh objects can be joined.")
    target = objs[0].name
    source_names = [o.name for o in objs]  # the join frees these references
    with active_objects(objs):
        status = bpy.ops.object.join()
    bpy.context.view_layer.update()
    if "FINISHED" not in status:
        raise CommandError(
            f"bpy.ops.object.join returned {list(status)}; nothing was joined."
        )
    ob = bpy.context.view_layer.objects.active
    if ob is None:
        raise CommandError("Join finished but Blender reports no active object.")
    if ob.name != target:
        raise CommandError(
            f"Expected the join to land on {target!r} but the active object is "
            f"{ob.name!r}. The inputs were {source_names}."
        )
    return {"joined_into": ob.name, "source_objects": source_names, **obj_summary(ob)}


def cmd_parent_objects(params):
    child = obj_of(params.get("child"))
    parent = obj_of(params.get("parent"))
    if child is parent:
        raise CommandError("An object cannot be parented to itself.")
    if child.name in {c.name for c in parent.children}:
        raise CommandError(f"{child.name!r} is already a child of {parent.name!r}.")
    matrix = child.matrix_world.copy()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    if params.get("keep_transform", True):
        child.matrix_world = matrix
    bpy.context.view_layer.update()
    return {
        "child": child.name,
        "parent": parent.name,
        "children": [c.name for c in parent.children],
    }


def cmd_create_collection(params):
    name = str(params.get("name") or "").strip()
    if not name:
        raise CommandError("A collection name is required.")
    parent_name = params.get("parent")
    parent = get_collection(parent_name, create=True) if parent_name else bpy.context.scene.collection
    if name in bpy.data.collections:
        raise CommandError(f"Collection {name!r} already exists.")
    col = bpy.data.collections.new(name)
    parent.children.link(col)
    for child in params.get("objects") or []:
        link_only(obj_of(child), col)
    return {"collection": col.name, "objects": len(col.objects)}


def cmd_assign_to_collection(params):
    objs = objs_of(params.get("objects"))
    col = get_collection(str(params.get("collection")))
    if col is None:
        raise CommandError(
            f"Collection {params.get('collection')!r} not found. "
            f"Available: {[c.name for c in bpy.data.collections]}"
        )
    for ob in objs:
        if col not in list(ob.users_collection):
            col.objects.link(ob)
    return {"collection": col.name, "objects": [o.name for o in objs]}


def cmd_add_modifier(params):
    ob = obj_of(params.get("object"))
    mod_type = str(params.get("type", "")).upper()
    name = str(params.get("name") or mod_type.title())
    try:
        mod = ob.modifiers.new(name=name, type=mod_type)
    except (RuntimeError, TypeError) as exc:
        valid = sorted({i.identifier for i in bpy.types.Modifier.bl_rna.properties["type"].enum_items})
        raise CommandError(f"Cannot add modifier {mod_type!r}: {exc}. Valid types: {valid}") from exc
    if mod.type == "NODES" and hasattr(mod, "node_group"):
        node_group = params.get("node_group")
        if node_group:
            ng = bpy.data.node_groups.get(node_group)
            if ng is None:
                raise CommandError(f"Geometry node group {node_group!r} not found.")
            mod.node_group = ng
    apply_params(mod, params.get("properties") or {})
    for key, value in (params.get("flags") or {}).items():
        setattr(mod, key, value)
    bpy.context.view_layer.update()
    return {"object": ob.name, "modifier": modifier_summary(mod)}


def modifier_summary(mod) -> dict:
    out = {"name": mod.name, "type": mod.type, "show_viewport": mod.show_viewport, "show_render": mod.show_render}
    if mod.type == "BEVEL":
        out.update(width=mod.width, segments=mod.segments, limit_method=mod.limit_method)
    elif mod.type == "SUBSURF":
        out.update(levels=mod.levels, render_levels=mod.render_levels)
    elif mod.type == "ARRAY":
        out.update(count=mod.count, relative_offset_factor=vec(mod.relative_offset_factor))
    elif mod.type == "SOLIDIFY":
        out.update(thickness=mod.thickness, offset=mod.offset)
    elif mod.type == "MIRROR":
        out.update(axis=tuple(m for m, on in zip("XYZ", mod.use_axis) if on))
    elif mod.type == "DISPLACE":
        out.update(strength=mod.strength, mid_level=mod.mid_level)
    elif mod.type == "SIMPLE_DEFORM":
        out.update(deform_method=mod.deform_method, angle=mod.angle)
    elif mod.type == "SHRINKWRAP":
        out.update(target=mod.target.name if mod.target else None, wrap_method=mod.wrap_method)
    return out


def cmd_apply_modifier(params):
    ob = obj_of(params.get("object"))
    name = params.get("modifier")
    with active_objects([ob]):
        if name in (None, "", "all"):
            if not ob.modifiers:
                raise CommandError(f"{ob.name!r} has no modifiers to apply.")
            bpy.ops.object.modifier_apply(modifier=ob.modifiers[-1].name)
            remaining = [m.name for m in ob.modifiers]
        else:
            mod = ob.modifiers.get(str(name))
            if mod is None:
                raise CommandError(
                    f"Modifier {name!r} not found on {ob.name!r}. "
                    f"Available: {[m.name for m in ob.modifiers]}"
                )
            bpy.ops.object.modifier_apply(modifier=mod.name)
            remaining = [m.name for m in ob.modifiers]
    bpy.context.view_layer.update()
    return {"object": ob.name, "remaining_modifiers": remaining, **obj_summary(ob)}


def coerce_value(rna, key: str, value):
    """Coerce one value to the type the RNA property declares.

    MCP clients frequently flatten nested structures and end up sending
    ``{"size": "70"}`` instead of ``{"size": 70}``, which bpy rejects with
    "expected a float type, not str". The expected type is right here in the RNA
    definition, so the conversion is unambiguous.
    """
    if not isinstance(value, str):
        return value
    try:
        prop = rna.properties[key]
    except (KeyError, TypeError, AttributeError):
        return value
    if prop.type == "FLOAT":
        with contextlib.suppress(ValueError):
            return float(value)
    elif prop.type == "INT":
        with contextlib.suppress(ValueError):
            return int(float(value))
    elif prop.type == "BOOLEAN":
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    elif prop.type == "ENUM":
        identifiers = {i.identifier for i in prop.enum_items}
        for candidate in (value.upper(), value.lower()):
            if candidate in identifiers:
                return candidate
    return value


def coerce_all(rna, values: dict) -> dict:
    return {key: coerce_value(rna, key, value) for key, value in (values or {}).items()}


def apply_params(target, properties: dict) -> list:
    """Set RNA properties, coercing strings to the declared type; report skips."""
    skipped: list = []
    rna = target.bl_rna
    for key, value in (properties or {}).items():
        if key not in rna.properties:
            skipped.append(key)
            continue
        try:
            setattr(target, key, coerce_value(rna, key, value))
        except (AttributeError, TypeError, ValueError):
            skipped.append(key)
    if skipped:
        print(f"[blender_mcp] skipped unknown/invalid properties: {skipped}")
    return skipped


# --------------------------------------------------------------------------- #
# materials
# --------------------------------------------------------------------------- #

BSDF_ALIASES = {
    "base_color": "Base Color",
    "color": "Base Color",
    "metallic": "Metallic",
    "roughness": "Roughness",
    "ior": "IOR",
    "alpha": "Alpha",
    "emission": "Emission Color",
    "emission_color": "Emission Color",
    "emission_strength": "Emission Strength",
    "coat": "Coat Weight",
    "coat_weight": "Coat Weight",
    "coat_roughness": "Coat Roughness",
    "sheen": "Sheen Weight",
    "sheen_weight": "Sheen Weight",
    "specular": "Specular IOR Level",
    "transmission": "Transmission Weight",
    "subsurface": "Subsurface Weight",
    "anisotropic": "Anisotropic",
    "metallic_roughness": None,
}

TEXTURE_NODES = {
    "noise": "ShaderNodeTexNoise",
    "voronoi": "ShaderNodeTexVoronoi",
    "gradient": "ShaderNodeTexGradient",
    "wave": "ShaderNodeTexWave",
    "checker": "ShaderNodeTexChecker",
    "magic": "ShaderNodeTexMagic",
    "brick": "ShaderNodeTexBrick",
    "musgrave": "ShaderNodeTexWhiteNoise",
    "white_noise": "ShaderNodeTexWhiteNoise",
    "image": "ShaderNodeTexImage",
    "environment": "ShaderNodeTexEnvironment",
}


def principled(mat: bpy.types.Material):
    if not mat.use_nodes:
        mat.use_nodes = True
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")


def set_input(node, name: str, value):
    socket = node.inputs.get(name)
    if socket is None:
        return False
    if socket.type == "RGBA" and len(value) == 3:
        value = (value[0], value[1], value[2], 1.0)
    if socket.type == "RGBA":
        socket.default_value = tuple(float(c) for c in value)[:4]
    elif socket.type == "VECTOR" and not hasattr(value, "__len__"):
        socket.default_value = (value, value, value)
    else:
        socket.default_value = value
    return True


def material_of(name: str) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if mat is None:
        close = [m.name for m in bpy.data.materials if name.lower() in m.name.lower()][:8]
        raise CommandError(
            f"Material {name!r} not found."
            + (f" Did you mean one of: {close}?" if close else " Create it with blender_create_material.")
        )
    return mat


def assign_material_to(ob: bpy.types.Object, material_name: str, slot: int | None = None):
    if ob.data is None or not hasattr(ob.data, "materials"):
        raise CommandError(f"Object {ob.name!r} ({ob.type}) cannot hold materials.")
    mat = material_of(material_name)
    ob.data.materials.clear()
    ob.data.materials.append(mat)
    return mat


def cmd_list_materials(params):
    detailed = bool(params.get("detailed"))
    out = []
    for mat in bpy.data.materials:
        item = {"name": mat.name, "users": mat.users, "use_nodes": mat.use_nodes}
        if detailed and mat.use_nodes:
            item["nodes"] = [n.bl_idname for n in mat.node_tree.nodes]
            bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is not None:
                item["principled"] = {
                    s.name: (list(s.default_value) if s.type == "RGBA" else s.default_value)
                    for s in bsdf.inputs
                    if not s.is_linked
                }
        out.append(item)
    return {"total": len(out), "materials": out}


def cmd_create_material(params):
    name = str(params.get("name") or "").strip()
    if not name:
        raise CommandError("A material name is required.")
    if name in bpy.data.materials and not params.get("overwrite"):
        raise CommandError(
            f"Material {name!r} already exists. Pass overwrite=true to reuse it."
        )
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = principled(mat)
    for key, value in (params.get("principled") or {}).items():
        socket = BSDF_ALIASES.get(key, key.replace("_", " ").title())
        if not set_input(bsdf, socket, value) and not set_input(bsdf, key, value):
            print(f"[blender_mcp] material socket {socket!r} not found")
    if params.get("base_color") is not None:
        set_input(bsdf, "Base Color", params["base_color"])
    for key, default in (("metallic", 0.0), ("roughness", 0.5), ("ior", 1.45)):
        if params.get(key) is not None:
            set_input(bsdf, BSDF_ALIASES[key], params[key])
    if params.get("emission_color") is not None:
        set_input(bsdf, "Emission Color", params["emission_color"])
    if params.get("emission_strength") is not None:
        set_input(bsdf, "Emission Strength", params["emission_strength"])

    if params.get("blend_method"):
        method = str(params["blend_method"]).upper()
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED" if method == "BLEND" else "DITHERED"
        elif hasattr(mat, "blend_method"):
            mat.blend_method = method
    if params.get("backface_culling") is not None and hasattr(mat, "use_backface_culling"):
        mat.use_backface_culling = bool(params["backface_culling"])
    if params.get("viewport_color"):
        mat.diffuse_color = tuple(params["viewport_color"])[:4]

    targets = params.get("assign_to") or []
    for ob_name in targets:
        assign_material_to(obj_of(ob_name), name)
    return {
        "material": name,
        "assigned_to": list(targets),
        "nodes": [n.name for n in mat.node_tree.nodes],
    }


def cmd_assign_material(params):
    objs = objs_of(params.get("objects"))
    mat = material_of(str(params.get("material")))
    for ob in objs:
        if ob.data is None or not hasattr(ob.data, "materials"):
            continue
        ob.data.materials.clear()
        ob.data.materials.append(mat)
    return {
        "material": mat.name,
        "objects": [
            {"name": o.name, "materials": [m.name for m in o.data.materials]}
            for o in objs
        ],
    }


def cmd_set_material_node(params):
    mat = material_of(str(params.get("material")))
    if not mat.use_nodes:
        mat.use_nodes = True
    tree = mat.node_tree
    node_type = TEXTURE_NODES.get(str(params.get("node_type", "noise")).lower())
    if node_type is None:
        raise CommandError(
            f"Unknown texture node {params.get('node_type')!r}. "
            f"Available: {sorted(TEXTURE_NODES)}"
        )
    node = tree.nodes.new(node_type)
    node.name = str(params.get("node_name") or node_type.split("Tex")[-1])
    node.label = str(params.get("node_name") or node.bl_label)
    apply_params(node, params.get("properties") or {})
    if params.get("image"):
        image = load_image(params["image"])
        if image is not None and node_type == "ShaderNodeTexImage":
            node.image = image
    if params.get("location"):
        node.location = list(params["location"])

    input_socket = str(params.get("input") or "Base Color")
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None or params.get("disconnect_output", True):
        out = node.outputs.get(params.get("output") or "Color") or node.outputs[0]
        inp = bsdf.inputs.get(BSDF_ALIASES.get(input_socket, input_socket))
        if inp is None:
            inp = bsdf.inputs.get(input_socket)
        if inp is None:
            raise CommandError(
                f"Principled BSDF has no input {input_socket!r}. "
                f"Available: {[s.name for s in bsdf.inputs]}"
            )
        tree.links.new(out, inp)
    return {"material": mat.name, "node": node.name, "type": node.bl_idname}


def load_image(path: str):
    resolved = resolve_path(path, must_exist=True)
    ext = os.path.splitext(resolved)[1].lower().lstrip(".")
    if ext in {"hdr", "exr"}:
        try:
            return bpy.data.images.load(resolved, check_existing=True)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Failed to load HDR image {resolved!r}: {exc}") from exc
    return bpy.data.images.load(resolved, check_existing=True)


def cmd_load_image_texture(params):
    image = load_image(str(params.get("path")))
    mat = bpy.data.materials.get(str(params.get("material"))) if params.get("material") else None
    if mat is None:
        mat = bpy.data.materials.new("MCP_ImageMaterial")
        mat.use_nodes = True
    tree = mat.node_tree
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    bsdf = principled(mat)
    if params.get("link_to_base_color", True):
        tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    return {"image": image.name, "size": list(image.size), "material": mat.name}


# --------------------------------------------------------------------------- #
# mesh editing (bmesh based, context free)
# --------------------------------------------------------------------------- #

EDIT_ACTIONS = {
    "extrude_faces": ("extrude_face_region", {"geom": "faces"}, {}),
    "extrude_edges": ("extrude_edge_only", {"geom": "edges"}, {}),
    "extrude_verts": ("extrude_vert_only", {"geom": "verts"}, {}),
    "bevel": ("bevel", {"geom": "all"}, {"offset": 0.05, "segments": 1, "profile": 0.5,
                                          "affect": "EDGES", "clamp_overlap": True}),
    "inset": ("inset_region", {"geom": "faces"}, {"thickness": 0.1, "depth": 0.0, "use_even_offset": True}),
    "inset_individual": ("inset_individual", {"geom": "faces"}, {"thickness": 0.05}),
    "subdivide": ("subdivide_edges", {"geom": "all"}, {"cuts": 1, "use_grid_fill": True}),
    "subdivide_smooth": ("subdivide_edges", {"geom": "all"}, {"cuts": 1, "use_grid_fill": True, "smooth": 0.0}),
    "poke": ("poke", {"geom": "faces"}, {"offset": 0.0}),
    "triangulate": ("triangulate", {"geom": "faces"}, {"quad_method": "BEAUTY", "ngon_method": "BEAUTY"}),
    "dissolve_edges": ("dissolve_edges", {"geom": "all"}, {"use_verts": False}),
    "dissolve_faces": ("dissolve_faces", {"geom": "faces"}, {"use_verts": False}),
    "dissolve_verts": ("dissolve_verts", {"geom": "verts"}, {}),
    "recalc_normals": ("recalc_face_normals", {"geom": "faces"}, {}),
    "flip_normals": ("reverse_faces", {"geom": "faces"}, {}),
    "symmetrize": ("symmetrize", {"geom": "all"}, {"direction": "NEGATIVE_X"}),
    "connect": ("connect_verts", {"geom": "verts"}, {}),
    "split_edges": ("split_edges", {"geom": "all"}, {}),
    "spin": ("spin", {"geom": "all"}, {"angle": math.pi, "steps": 12, "use_merge": False}),
    "bridge": ("bridge_loops", {"geom": "all"}, {}),
    "holes_fill": ("holes_fill", {"geom": "all"}, {"sides": 0}),
    "contextual_create": ("contextual_create", {"geom": "all"}, {}),
    "remove_doubles": ("remove_doubles", {"geom": "all"}, {"dist": 0.0001}),
    "edge_turn": ("rotate", {"geom": "all"}, {}),
    "flip": ("rotate", {"geom": "all"}, {}),
}


def cmd_edit_mesh(params):
    action = str(params.get("action", "")).lower()
    objs = objs_of(params.get("objects") or [bpy.context.view_layer.objects.active.name
                                            if bpy.context.view_layer.objects.active else None],
                   "MESH")
    region = str(params.get("region", "selected")).lower()
    if region not in {"selected", "all"}:
        raise CommandError("region must be 'selected' or 'all'.")

    if action in {"delete_verts", "delete_edges", "delete_faces", "delete"}:
        # bm.select_mode and bmesh.ops.delete use different vocabularies.
        target = action.replace("delete_", "")
        select_mode = {"verts": "VERT", "edges": "EDGE", "faces": "FACE"}[target]
        delete_context = {"verts": "VERTS", "edges": "EDGES", "faces": "FACES"}[target]
        results = []
        for ob in objs:
            with edit_bmesh(ob) as bm:
                bm.select_mode = {select_mode}
                if region == "all":
                    for element in bm_elements(bm, select_mode):
                        element.select = True
                geom = [element for element in bm_elements(bm, select_mode) if element.select]
                if not geom:
                    raise CommandError(f"Nothing selected to delete on {ob.name!r}.")
                before = bm_counts(bm)
                _bmesh_call(bm, "delete", geom=geom, context=delete_context)
                during = bm_counts(bm)
            results.append({"object": ob.name, "before": before, "during": during,
                            **mesh_stats(ob.data)})
        return {"action": action, "results": results}

    if action in {"merge_verts", "collapse", "weld"}:
        results = []
        for ob in objs:
            with edit_bmesh(ob) as bm:
                geom = selection_geom(bm, region)
                before = bm_counts(bm)
                if action == "collapse":
                    _bmesh_call(bm, "dissolve_verts", geom=geom, use_verts=True)
                else:
                    _bmesh_call(bm, "remove_doubles", geom=geom,
                                dist=float(params.get("distance", 0.0001)))
                during = bm_counts(bm)
            results.append({"object": ob.name, "before": before, "during": during,
                            **mesh_stats(ob.data)})
        return {"action": action, "results": results}

    if action in {"select_all", "select_none", "select_invert"}:
        results = []
        for ob in objs:
            with edit_bmesh(ob) as bm:
                for el in (bm.verts, bm.edges, bm.faces):
                    for item in el:
                        if action == "select_all":
                            item.select = True
                        elif action == "select_none":
                            item.select = False
                        else:
                            item.select = not item.select
                results.append(
                    {
                        "object": ob.name,
                        "selected_verts": sum(1 for v in bm.verts if v.select),
                        "selected_edges": sum(1 for e in bm.edges if e.select),
                        "selected_faces": sum(1 for f in bm.faces if f.select),
                    }
                )
        return {"action": action, "results": results}

    if action in {"set_shade_smooth", "set_shade_flat"}:
        smooth = action == "set_shade_smooth"
        return set_shade_smooth(objs, smooth, params.get("auto_smooth_angle"))

    spec = EDIT_ACTIONS.get(action)
    if spec is None:
        raise CommandError(
            f"Unknown mesh action {action!r}. Available: "
            f"{sorted(set(EDIT_ACTIONS) | {'delete', 'delete_verts', 'delete_edges', 'delete_faces', 'merge_verts', 'collapse', 'select_all', 'select_none', 'select_invert', 'set_shade_smooth', 'set_shade_flat'})}"
        )
    op_name, kinds, defaults = spec
    extra = dict(defaults)
    extra.update(params.get("parameters") or {})
    results = []
    for ob in objs:
        with edit_bmesh(ob) as bm:
            if kinds.get("geom") == "faces":
                geom = [f for f in bm.faces if f.select] or list(bm.faces)
            elif kinds.get("geom") == "edges":
                geom = [e for e in bm.edges if e.select] or list(bm.edges)
            elif kinds.get("geom") == "verts":
                geom = [v for v in bm.verts if v.select] or list(bm.verts)
            else:
                geom = selection_geom(bm, region)
            before = bm_counts(bm)
            _bmesh_call(bm, op_name, geom=geom, **extra)
            during = bm_counts(bm)
        # Mesh data is only authoritative once the bmesh has been written back.
        results.append({"object": ob.name, "before": before, "during": during,
                        **mesh_stats(ob.data)})
    return {"action": action, "operator": op_name, "results": results}


def set_shade_smooth(objs, smooth: bool, auto_smooth_angle=None):
    for ob in objs:
        if ob.type != "MESH":
            continue
        if auto_smooth_angle is not None:
            with active_objects([ob]), view3d_override():
                with contextlib.suppress(Exception):
                    bpy.ops.object.shade_auto_smooth(angle=math.radians(float(auto_smooth_angle)))
        for poly in ob.data.polygons:
            poly.use_smooth = bool(smooth)
        ob.data.update()
    return {
        "objects": [o.name for o in objs],
        "shade_smooth": bool(smooth),
        "auto_smooth_angle": auto_smooth_angle,
    }


def cmd_shade_smooth(params):
    objs = objs_of(params.get("objects"), "MESH")
    return set_shade_smooth(objs, bool(params.get("smooth", True)),
                            params.get("auto_smooth_angle"))


# --------------------------------------------------------------------------- #
# camera / lights / world
# --------------------------------------------------------------------------- #


def cmd_add_light(params):
    kind = str(params.get("type", "AREA")).upper()
    valid = {"POINT", "SUN", "SPOT", "AREA"}
    if kind not in valid:
        raise CommandError(f"Light type must be one of {sorted(valid)}")
    data = bpy.data.lights.new(str(params.get("name") or kind.title()), type=kind)
    data.energy = float(params.get("energy", 1000.0))
    if params.get("color"):
        data.color = tuple(params["color"])[:3]
    for attr, key in (("size", "size"), ("size_y", "size_y"), ("spot_size", "spot_size"),
                      ("shadow_soft_size", "shadow_soft_size")):
        if params.get(key) is not None and hasattr(data, attr):
            setattr(data, attr, float(params[key]))
    ob = bpy.data.objects.new(data.name, data)
    target_col = get_collection(str(params["collection"]), create=True) if params.get("collection") else bpy.context.scene.collection
    target_col.objects.link(ob)
    ob.location = Vector(params.get("location") or (4, 4, 6))
    if params.get("rotation"):
        ob.rotation_euler = Euler([math.radians(a) for a in params["rotation"]], "XYZ")
    if params.get("point_at"):
        aim_at(ob, params["point_at"])
    bpy.context.view_layer.update()
    return obj_summary(ob, detailed=True)


def aim_at(ob, target) -> Vector:
    """Rotate ``ob`` so its local -Z axis points at ``target``."""
    if isinstance(target, str) and target in bpy.data.objects:
        point = bpy.data.objects[target].matrix_world.translation
    elif isinstance(target, str):
        try:
            point = Vector([float(x) for x in target.split(",")])
        except ValueError as exc:
            raise CommandError(
                f"Invalid target {target!r}: expected an object name or 'x,y,z'."
            ) from exc
    else:
        point = Vector(target)
    # matrix_world is still stale for an object created moments ago, which would
    # silently yield a zero direction and leave the rotation untouched.
    if ob.parent is None:
        origin = Vector(ob.location)
    else:
        bpy.context.view_layer.update()
        origin = ob.matrix_world.translation
    direction = point - origin
    if direction.length < 1e-6:
        raise CommandError(
            f"{ob.name!r} is already at the target {list(point)}; nothing to aim at."
        )
    ob.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return ob.rotation_euler


def cmd_add_camera(params):
    name = str(params.get("name") or "Camera")
    data = bpy.data.cameras.new(name)
    if params.get("lens"):
        data.lens = float(params["lens"])
    elif params.get("fov_degrees"):
        data.angle = math.radians(float(params["fov_degrees"]))
    data.clip_start = float(params.get("clip_start", 0.1))
    data.clip_end = float(params.get("clip_end", 1000.0))
    ob = bpy.data.objects.new(name, data)
    target_col = get_collection(str(params["collection"]), create=True) if params.get("collection") else bpy.context.scene.collection
    target_col.objects.link(ob)
    ob.location = Vector(params.get("location") or (7, -7, 5))
    if params.get("rotation"):
        ob.rotation_euler = Euler([math.radians(a) for a in params["rotation"]], "XYZ")
    if params.get("point_at"):
        aim_at(ob, params["point_at"])
    if params.get("active", True):
        bpy.context.scene.camera = ob
    bpy.context.view_layer.update()
    return obj_summary(ob, detailed=True)


def cmd_look_at(params):
    ob = obj_of(params.get("object"))
    target = params.get("target")
    if target is None:
        raise CommandError("Provide a target object name or 'x,y,z' coordinates.")
    aim_at(ob, target)
    bpy.context.view_layer.update()
    return {"object": ob.name, "rotation_degrees": [round(math.degrees(a), 3) for a in ob.rotation_euler]}


def cmd_set_active_camera(params):
    ob = obj_of(params.get("name") or params.get("object"), "CAMERA")
    bpy.context.scene.camera = ob
    return {"camera": ob.name, "location": vec(ob.location)}


def cmd_set_world(params):
    scene = bpy.context.scene
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    tree = world.node_tree
    bg = next((n for n in tree.nodes if n.type == "BACKGROUND"), None)
    if bg is None:
        bg = tree.nodes.new("ShaderNodeBackground")
        out = next((n for n in tree.nodes if n.type == "OUTPUT_WORLD"), None)
        if out:
            tree.links.new(bg.outputs[0], out.inputs["Surface"])
    preset = str(params.get("preset", "color")).lower()
    detail = {"preset": preset}

    if params.get("color") or preset in {"color", "solid", "studio_gray"}:
        default_color = [0.18, 0.18, 0.18] if preset == "studio_gray" else [0.05, 0.05, 0.05]
        color = list(params.get("color") or default_color)
        bg.inputs[0].default_value = tuple(color[:3]) + (1.0,)
        bg.inputs[1].default_value = float(params.get("strength", 1.0))
    elif preset == "hdri" or params.get("hdri"):
        image = load_image(str(params.get("hdri")))
        env = tree.nodes.new("ShaderNodeTexEnvironment")
        env.image = image
        tree.links.new(env.outputs["Color"], bg.inputs[0])
        bg.inputs[1].default_value = float(params.get("strength", 1.0))
        detail["hdri"] = image.name
    elif preset in {"sky", "sunny"}:
        sky = tree.nodes.new("ShaderNodeTexSky")
        sky.sky_type = "NISHITA" if "NISHITA" in {i.identifier for i in sky.bl_rna.properties["sky_type"].enum_items} else sky.sky_type
        for key, attr in (("sun_elevation", "sun_elevation"), ("sun_rotation", "sun_rotation"),
                          ("altitude", "altitude"), ("air_density", "air_density"),
                          ("dust_density", "dust_density"), ("sun_intensity", "sun_intensity")):
            if params.get(key) is not None and hasattr(sky, attr):
                setattr(sky, attr, float(params[key]))
        tree.links.new(sky.outputs[0], bg.inputs[0])
        bg.inputs[1].default_value = float(params.get("strength", 1.0))
    if params.get("gradient"):
        top, bottom = params["gradient"]
        ramp = tree.nodes.new("ShaderNodeValToRGB")
        coord = tree.nodes.new("ShaderNodeTexCoord")
        sep = tree.nodes.new("ShaderNodeSeparateXYZ")
        ramp.color_ramp.elements[0].color = tuple(list(bottom)[:3]) + (1.0,)
        ramp.color_ramp.elements[1].color = tuple(list(top)[:3]) + (1.0,)
        tree.links.new(coord.outputs["Generated"], sep.inputs[0])
        tree.links.new(sep.outputs["Z"], ramp.inputs[0])
        tree.links.new(ramp.outputs[0], bg.inputs[0])
        detail["gradient"] = [list(top), list(bottom)]
    detail["background_color"] = list(bg.inputs[0].default_value)
    detail["strength"] = bg.inputs[1].default_value
    return detail


# --------------------------------------------------------------------------- #
# render / output
# --------------------------------------------------------------------------- #


def cmd_set_render_settings(params):
    scene = bpy.context.scene
    render = scene.render
    changes = {}
    if params.get("engine"):
        changes["engine"] = set_render_engine(scene, str(params["engine"]))
    if params.get("resolution"):
        render.resolution_x, render.resolution_y = (int(v) for v in params["resolution"])
    if params.get("resolution_percentage") is not None:
        render.resolution_percentage = int(params["resolution_percentage"])
    if params.get("samples") is not None:
        samples = int(params["samples"])
        if hasattr(render, "cycles"):
            render.cycles.samples = samples
            render.cycles.preview_samples = min(samples, 32)
        if hasattr(render, "eevee"):
            with contextlib.suppress(Exception):
                render.eevee.taa_render_samples = samples
                render.eevee.taa_samples = min(samples, 64)
        changes["samples"] = samples
    if params.get("fps") is not None:
        render.fps = int(params["fps"])
    if params.get("film_transparent") is not None:
        render.film_transparent = bool(params["film_transparent"])
    if params.get("use_motion_blur") is not None and hasattr(render, "use_motion_blur"):
        render.use_motion_blur = bool(params["use_motion_blur"])
    if params.get("view_transform"):
        view_settings = scene.view_settings
        requested = str(params["view_transform"])
        try:
            view_settings.view_transform = requested
        except TypeError as exc:
            available = enum_options(view_settings, "view_transform")
            raise CommandError(
                f"Invalid view_transform {requested!r}. "
                f"Available: {available or 'see the Colour Management panel'}. ({exc})"
            ) from exc
        changes["view_transform"] = view_settings.view_transform
    if params.get("look"):
        try:
            scene.view_settings.look = str(params["look"])
        except TypeError as exc:
            available = enum_options(scene.view_settings, "look")
            raise CommandError(
                f"Invalid look {params['look']!r}. Available: {available or 'none'}."
            ) from exc
    if params.get("output_format"):
        render.image_settings.file_format = str(params["output_format"]).upper()
    if params.get("output_path"):
        render.filepath = resolve_path(str(params["output_path"]))
    if params.get("denoise") is not None and hasattr(render, "cycles"):
        render.cycles.use_denoising = bool(params["denoise"])
    if params.get("max_bounces") is not None and hasattr(render, "cycles"):
        render.cycles.max_bounces = int(params["max_bounces"])
    if params.get("gpu_device") and hasattr(render, "cycles"):
        render.cycles.device = str(params["gpu_device"]).upper()
    if params.get("dof"):
        dof = params["dof"]
        cam = scene.camera
        if cam is None:
            raise CommandError("Depth of field needs an active camera; add one first.")
        cam.data.dof.use_dof = True
        if dof.get("focus_object"):
            cam.data.dof.focus_object = obj_of(dof["focus_object"])
        if dof.get("focus_distance") is not None:
            cam.data.dof.focus_distance = float(dof["focus_distance"])
        if dof.get("aperture_fstop") is not None:
            cam.data.dof.aperture_fstop = float(dof["aperture_fstop"])
    if params.get("frame_range"):
        scene.frame_start, scene.frame_end = (int(v) for v in params["frame_range"])
    return {
        "changes": changes,
        "engine": render.engine,
        "resolution": [render.resolution_x, render.resolution_y, render.resolution_percentage],
        "samples": getattr(getattr(render, "cycles", None), "samples", None),
        "view_transform": scene.view_settings.view_transform,
    }


def _output_path(params, default_name: str) -> str:
    if params.get("output_path"):
        return resolve_path(str(params["output_path"]))
    directory = tempfile.gettempdir()
    return os.path.join(directory, default_name)


def cmd_render(params):
    scene = bpy.context.scene
    mode = str(params.get("mode", "still")).lower()
    if mode not in {"still", "animation"}:
        raise CommandError("mode must be 'still' or 'animation'.")
    explicit = bool(params.get("output_path"))
    output = _output_path(params, f"blender_mcp_{int(time.time())}.png")
    previous_filepath = scene.render.filepath
    previous_format = scene.render.image_settings.file_format
    if params.get("format"):
        scene.render.image_settings.file_format = str(params["format"]).upper()
    extension = "." + scene.render.image_settings.file_format.lower()
    if not output.lower().endswith(extension):
        output = os.path.splitext(output)[0] + extension

    started = time.time()
    try:
        scene.render.filepath = output
        if mode == "animation":
            bpy.ops.render.render(animation=True)
        else:
            bpy.ops.render.render(write_still=True)
    finally:
        if not explicit:
            scene.render.filepath = previous_filepath
            scene.render.image_settings.file_format = previous_format
    return {
        "mode": mode,
        "output": output,
        "exists": os.path.exists(output),
        "bytes": os.path.getsize(output) if os.path.exists(output) else 0,
        "seconds": round(time.time() - started, 2),
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "engine": scene.render.engine,
    }


def cmd_capture(params):
    mode = str(params.get("mode", "viewport")).lower()
    width = int(params.get("width") or 1280)
    height = int(params.get("height") or 720)
    view = params.get("view")
    output = _output_path(params, f"blender_mcp_view_{int(time.time())}.png")

    method = "screen_screenshot"
    if mode == "window":
        bpy.ops.screen.screenshot(filepath=output)
    elif mode in {"camera", "render"}:
        scene = bpy.context.scene
        previous = scene.render.filepath
        scene.render.filepath = output
        try:
            if mode == "camera":
                with view3d_override():
                    bpy.ops.render.opengl(write_still=True, view_context=False)
                method = "opengl_camera"
            else:
                bpy.ops.render.render(write_still=True)
                method = "full_render"
        finally:
            scene.render.filepath = previous
    else:
        method = capture_viewport(output, width, height, view)

    if not os.path.exists(output):
        raise CommandError(f"Capture produced no file at {output!r}.")
    return {
        "mode": mode,
        "method": method,
        "path": output,
        "width": width,
        "height": height,
        "bytes": os.path.getsize(output),
    }


def capture_viewport(path: str, width: int, height: int, view=None) -> str:
    """Rasterise the current 3D viewport to ``path``.

    Blender 4.x no longer exposes SpaceView3D.draw_view3d, so the supported
    route is ``render.opengl`` with ``view_context=True``: it draws whatever
    the viewport is looking at, with the viewport's own shading mode.
    """
    screen = bpy.context.screen
    if screen is None:
        raise CommandError(
            "No screen context is available for a viewport capture. Use "
            "mode='camera' or mode='render' instead."
        )
    if not any(area.type == "VIEW_3D" for area in screen.areas):
        raise CommandError(
            "The current Blender workspace has no 3D viewport. Switch to a "
            "Layout workspace, or use mode='camera'/'render'."
        )
    if view:
        with view3d_override():
            with contextlib.suppress(Exception):
                bpy.ops.view3d.view_axis(type=str(view).upper(), align_active=False)

    render = bpy.context.scene.render
    saved = (render.resolution_x, render.resolution_y,
             render.resolution_percentage, render.filepath)
    try:
        render.resolution_x = width
        render.resolution_y = height
        render.resolution_percentage = 100
        render.filepath = path
        with view3d_override():
            bpy.ops.render.opengl(write_still=True, view_context=True)
    finally:
        (render.resolution_x, render.resolution_y,
         render.resolution_percentage, render.filepath) = saved
    return "opengl_viewport"


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #

FILE_OPS = {
    "new": lambda p: bpy.ops.wm.read_homefile(use_empty=bool(p.get("empty", True))),
    "open": lambda p: bpy.ops.wm.open_mainfile(filepath=resolve_path(p["path"], must_exist=True)),
    "save": lambda p: bpy.ops.wm.save_mainfile(
        filepath=resolve_path(p["path"]) if p.get("path") else bpy.data.filepath),
    "save_as": lambda p: bpy.ops.wm.save_as_mainfile(
        filepath=resolve_path(p["path"]), copy=bool(p.get("copy", False))),
    "revert": lambda p: bpy.ops.wm.revert_mainfile(),
}


def cmd_file_op(params):
    action = str(params.get("action", "")).lower()
    fn = FILE_OPS.get(action)
    if fn is None:
        raise CommandError(
            f"Unknown file action {action!r}. Choose one of: {sorted(FILE_OPS)}"
        )
    if action in {"open", "save_as"} and not params.get("path"):
        raise CommandError(f"Action {action!r} requires a 'path'.")
    started = time.time()
    result = fn(params)
    return {
        "action": action,
        "filepath": bpy.data.filepath,
        "is_dirty": bpy.data.is_dirty,
        "objects": len(bpy.context.scene.objects),
        "result": list(result),
        "seconds": round(time.time() - started, 2),
    }


IMPORTERS = {
    ".glb": ("import_scene", "gltf"), ".gltf": ("import_scene", "gltf"),
    ".fbx": ("import_scene", "fbx"), ".obj": ("wm", "obj_import"),
    ".stl": ("wm", "stl_import"), ".ply": ("wm", "ply_import"),
    ".usd": ("wm", "usd_import"), ".usdc": ("wm", "usd_import"),
    ".usda": ("wm", "usd_import"), ".abc": ("wm", "alembic_import"),
    ".dae": ("wm", "collada_import"), ".blend": ("wm", "open_mainfile"),
}
EXPORTERS = {
    ".glb": ("export_scene", "gltf", "glTF Binary"),
    ".gltf": ("export_scene", "gltf", "glTF Separate"),
    ".fbx": ("export_scene", "fbx", "FBX"), ".obj": ("wm", "obj_export", "OBJ"),
    ".stl": ("wm", "stl_export", "STL"), ".ply": ("wm", "ply_export", "PLY"),
    ".usd": ("wm", "usd_export", "USD"), ".abc": ("wm", "alembic_export", "Alembic"),
    ".blend": ("wm", "save_as_mainfile", "blend"),
}


def _resolve_op(namespace: str, name: str):
    module = getattr(bpy.ops, namespace, None)
    if module is None:
        return None
    return getattr(module, name, None)


def cmd_import_model(params):
    path = resolve_path(str(params.get("path")), must_exist=True)
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMPORTERS:
        raise CommandError(
            f"Unsupported import format {ext!r}. Supported: {sorted(IMPORTERS)}"
        )
    namespace, name = IMPORTERS[ext]
    op = _resolve_op(namespace, name)
    if op is None:
        raise CommandError(
            f"Importer for {ext!r} ({namespace}.{name}) is not available in this Blender build."
        )
    before = set(bpy.data.objects)
    kwargs = dict(params.get("properties") or {})
    if ext in {".glb", ".gltf"} and "filepath" not in kwargs:
        kwargs["filepath"] = path
    elif ext not in {".blend"}:
        kwargs.setdefault("filepath", path)
    if ext == ".blend":
        kwargs["filepath"] = path
    with view3d_override():
        result = op(**kwargs)
    created = [o.name for o in bpy.data.objects if o not in before]
    if params.get("collection"):
        col = get_collection(str(params["collection"]), create=True)
        for name_ in created:
            with contextlib.suppress(Exception):
                link_only(bpy.data.objects[name_], col)
    bpy.context.view_layer.update()
    return {
        "path": path,
        "format": ext,
        "created_objects": created,
        "count": len(created),
        "result": list(result),
    }


def cmd_export_model(params):
    path = resolve_path(str(params.get("path")))
    ext = os.path.splitext(path)[1].lower()
    if ext not in EXPORTERS:
        raise CommandError(
            f"Unsupported export format {ext!r}. Supported: {sorted(EXPORTERS)}"
        )
    namespace, name, filter_name = EXPORTERS[ext]
    op = _resolve_op(namespace, name)
    if op is None:
        raise CommandError(
            f"Exporter for {ext!r} ({namespace}.{name}) is not available in this Blender build."
        )
    kwargs = dict(params.get("properties") or {})
    kwargs.setdefault("filepath", path)
    selection = params.get("selection") or "selected_objects"
    if ext in {".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply", ".abc"}:
        kwargs.setdefault("use_selection", selection == "selected_objects")
    if ext == ".ply":
        kwargs.pop("use_selection", None)
        if selection == "selected_objects":
            kwargs.setdefault("export_selected", True)
    with view3d_override():
        result = op(**kwargs)
    return {"path": path, "format": ext, "exists": os.path.exists(path),
            "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
            "result": list(result)}


# --------------------------------------------------------------------------- #
# animation
# --------------------------------------------------------------------------- #


def cmd_set_frame(params):
    scene = bpy.context.scene
    if params.get("frame") is not None:
        scene.frame_set(int(params["frame"]))
    if params.get("start") is not None:
        scene.frame_start = int(params["start"])
    if params.get("end") is not None:
        scene.frame_end = int(params["end"])
    if params.get("fps") is not None:
        scene.render.fps = int(params["fps"])
    bpy.context.view_layer.update()
    return {
        "current": scene.frame_current,
        "start": scene.frame_start,
        "end": scene.frame_end,
        "fps": scene.render.fps,
    }


DATA_PATHS = {
    "location": "location", "rotation": "rotation_euler", "scale": "scale",
    "energy": "data.energy", "color": "data.color", "lens": "data.lens",
    "focal_length": "data.lens", "angle": "data.angle",
}


def cmd_insert_keyframe(params):
    targets = params.get("objects") or [bpy.context.view_layer.objects.active.name
                                       if bpy.context.view_layer.objects.active else None]
    objs = objs_of(targets)
    frame = int(params.get("frame") if params.get("frame") is not None
                else bpy.context.scene.frame_current)
    paths = params.get("data_paths")
    if paths is None:
        keys = [k for k in ("location", "rotation", "scale") if params.get(k)]
        paths = [DATA_PATHS[k] for k in keys] or ["location"]
    if isinstance(paths, str):
        paths = [DATA_PATHS.get(paths, paths)]
    group = params.get("group")
    inserted = []
    bpy.context.scene.frame_set(frame)
    for ob in objs:
        for path in paths:
            resolved = DATA_PATHS.get(path, path)
            keywords = {"data_path": resolved, "frame": frame}
            if group:
                keywords["group"] = group
            try:
                ob.keyframe_insert(**keywords)
            except Exception as exc:  # noqa: BLE001
                raise CommandError(
                    f"Cannot keyframe {resolved!r} on {ob.name!r}: {exc}"
                ) from exc
            inserted.append({"object": ob.name, "data_path": resolved, "frame": frame})
    return {"frame": frame, "keyframes": inserted, "actions": [a.name for a in bpy.data.actions]}


def cmd_animation_info(params):
    scene = bpy.context.scene
    objs = list(scene.objects)
    limit = int(params.get("limit") or 25)
    return {
        "frame_range": [scene.frame_start, scene.frame_end, scene.render.fps],
        "current": scene.frame_current,
        "actions": [
            {"name": a.name, "fcurves": len(a.fcurves), "users": a.users} for a in bpy.data.actions
        ],
        "animated_objects": [
            {
                "name": o.name,
                "action": o.animation_data.action.name if o.animation_data and o.animation_data.action else None,
                "keyframes": sum(
                    len(fc.keyframe_points)
                    for fc in (o.animation_data.action.fcurves if o.animation_data and o.animation_data.action else [])
                ),
            }
            for o in objs
            if o.animation_data and o.animation_data.action
        ][:limit],
    }


# --------------------------------------------------------------------------- #
# introspection / operator bridge
# --------------------------------------------------------------------------- #


def cmd_list_operators(params):
    query = str(params.get("query") or "").lower().replace(".", " ")
    category = params.get("category")
    include_properties = bool(params.get("include_properties"))
    limit = int(params.get("limit") or 100)
    results = []
    for namespace in dir(bpy.ops):
        if namespace.startswith("_") or (category and namespace != category):
            continue
        module = getattr(bpy.ops, namespace)
        for op_name in dir(module):
            if op_name.startswith("_"):
                continue
            full = f"{namespace}.{op_name}"
            if query and query not in full.replace(".", " "):
                continue
            entry = {"operator": full, "module": namespace}
            try:
                rna = getattr(module, op_name).get_rna_type()
                entry["description"] = rna.description
                if include_properties:
                    props = {}
                    for p in rna.properties:
                        if p.identifier == "rna_type":
                            continue
                        info = {"type": p.type}
                        if p.type == "ENUM":
                            info["options"] = [i.identifier for i in p.enum_items]
                        elif p.type in {"FLOAT", "INT"} and not p.is_readonly:
                            info["default"] = getattr(p, "default", None)
                        info["readonly"] = bool(p.is_readonly)
                        props[p.identifier] = info
                    entry["properties"] = props
            except Exception:  # noqa: BLE001
                entry["description"] = ""
            results.append(entry)
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    return {"total": len(results), "operators": results}


def cmd_run_operator(params):
    operator = str(params.get("operator", "")).strip()
    if "." not in operator:
        raise CommandError(
            f"Operator must be 'category.name' (e.g. mesh.primitive_cube_add), got {operator!r}."
        )
    namespace, _, name = operator.partition(".")
    op = _resolve_op(namespace, name)
    if op is None:
        raise CommandError(
            f"Operator {operator!r} does not exist. Use blender_list_operators to discover valid ones."
        )
    properties = dict(params.get("properties") or {})
    skipped: list = []
    try:
        rna = op.get_rna_type()
        skipped = [key for key in properties if key not in rna.properties]
        for key in skipped:
            properties.pop(key)
        properties = coerce_all(rna, properties)
    except Exception:  # noqa: BLE001
        pass

    override = {}
    if params.get("area_type"):
        override["area"] = next(
            (a for scr in bpy.data.screens for a in scr.areas if a.type == str(params["area_type"])),
            None,
        )
    result = None
    error = None
    for use_3d in (False, True):
        try:
            if use_3d:
                with view3d_override():
                    result = op(**properties)
            elif override.get("area") is not None:
                with bpy.context.temp_override(area=override["area"]):
                    result = op(**properties)
            else:
                result = op(**properties)
            error = None
            break
        except RuntimeError as exc:
            error = exc
            if "context" not in str(exc).lower():
                break
    if error is not None:
        raise CommandError(
            f"Operator {operator!r} failed: {error}. "
            "It may need a specific area/mode context, or required properties."
        ) from error
    bpy.context.view_layer.update()
    return {
        "operator": operator,
        "status": list(result),
        "skipped_properties": skipped,
        "active_object": bpy.context.view_layer.objects.active.name
        if bpy.context.view_layer.objects.active
        else None,
        "objects_in_scene": len(bpy.context.scene.objects),
    }


# --------------------------------------------------------------------------- #
# arbitrary python
# --------------------------------------------------------------------------- #


class _Result:
    """Explicit return marker for blender_execute_python: Result(x) -> x."""

    _mcp_result = True

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return repr(self.value)


def _compile_with_tail(code: str):
    """Split source into (body, trailing expression) so both forms return a value.

    Returns ``(mode, source, tail)`` where mode is 'eval' or 'exec' and tail is
    the source of the last expression statement, if there is one.
    """
    import ast

    try:
        tree = ast.parse(code, mode="exec", filename="<blender_mcp>")
    except SyntaxError:
        # not valid as a statement block; maybe it is a bare expression
        ast.parse(code, mode="eval", filename="<blender_mcp>")
        return "eval", code, None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tail = ast.unparse(tree.body[-1].value)
        head = ast.Module(body=tree.body[:-1], type_ignores=[])
        ast.fix_missing_locations(head)
        return "exec", compile(head, "<blender_mcp>", "exec"), tail
    return "exec", compile(tree, "<blender_mcp>", "exec"), None


def cmd_execute(params):
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        raise CommandError("Provide non-empty Python code in 'code'.")
    context = dict(params.get("context") or {})

    buffer = None
    stdout_sink = sys.stdout
    if context.get("stdout") == "capture":
        import io

        buffer = io.StringIO()
        stdout_sink = buffer

    namespace = {
        "bpy": bpy,
        "bmesh": bmesh,
        "math": math,
        "os": os,
        "sys": sys,
        "json": json,
        "random": random,
        "time": time,
        "Vector": Vector,
        "Euler": Euler,
        "Matrix": Matrix,
        "Quaternion": Quaternion,
        "view3d": view3d_override,
        "Result": _Result,
        "resolve_path": resolve_path,
        "obj_of": obj_of,
        "__name__": "__blender_mcp__",
    }

    result_value = None
    error = None
    tb = None
    original_stdout = sys.stdout
    started = time.time()
    try:
        sys.stdout = stdout_sink
        if context.get("stdout") != "none":
            mode, source, tail = _compile_with_tail(code)
            if mode == "eval":
                result_value = eval(source, namespace)  # noqa: S307
            else:
                exec(source, namespace)  # noqa: S102
                if tail is not None:
                    result_value = eval(  # noqa: S307
                        compile(tail, "<blender_mcp>", "eval"), namespace
                    )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc()
    finally:
        sys.stdout = original_stdout
        with contextlib.suppress(Exception):
            bpy.context.view_layer.update()

    payload = {
        "return_value": result_value,
        "stdout": buffer.getvalue() if buffer is not None else "",
        "error": error,
        "traceback": tb,
        "seconds": round(time.time() - started, 4),
    }
    if error is None:
        active = bpy.context.view_layer.objects.active
        payload["active_object"] = active.name if active else None
        payload["objects_in_scene"] = len(bpy.context.scene.objects)
    return payload


def cmd_search_blender_docs(params):
    """Offline keyword lookup across operator descriptions and RNA docs."""
    query = str(params.get("query", "")).lower()
    if not query:
        raise CommandError("A non-empty 'query' is required.")
    limit = int(params.get("limit") or 40)
    hits = []
    for namespace in dir(bpy.ops):
        if namespace.startswith("_"):
            continue
        module = getattr(bpy.ops, namespace)
        for op_name in dir(module):
            if op_name.startswith("_"):
                continue
            full = f"{namespace}.{op_name}"
            if query in full.lower():
                hits.append(full)
                if len(hits) >= limit:
                    return {"query": query, "matches": hits}
    return {"query": query, "matches": sorted(hits)}


# --------------------------------------------------------------------------- #
# v2 additions: validation, textures, extended operations
# --------------------------------------------------------------------------- #
def cmd_validate(params):
    """Full model audit: scale, dimensions, normals, topology, intersections,
    symmetry, naming, materials, UVs, transforms, pivots, budget, LODs."""
    return _val.validate(params)


def cmd_analyze_mesh(params):
    """Deep statistics for a single mesh: manifoldness, shell volume, areas,
    loose and duplicate geometry, UV and modifier inventory."""
    return _val.analyze_mesh(params)


def cmd_measure(params):
    """Real-world measurements: bounding box, centre distance, assembly extent."""
    return _val.measure(params)


def cmd_find_problems(params):
    """Validation restricted to what failed, ranked, with a fix hint per item.

    Same engine as ``validate`` but shaped for an agent that wants to act, not
    for a human reading a report top to bottom.
    """
    report = _val.validate(params)
    fixes = {
        "scale": "call blender_set_render_settings or fix the scene unit scale",
        "dimensions": "edit the generator parameters rather than adding geometry",
        "normals": "blender_geometry operation='recalc_normals'",
        "topology": "blender_geometry operation='weld' then re-check; "
                    "delete loose islands",
        "intersections": "move or shrink one of the reported pairs",
        "symmetry": "mirror one half onto the other",
        "naming": "strip the .001 suffix before exporting",
        "materials": "blender_assign_material",
        "uv": "blender_uv action='smart_project'",
        "transforms": "blender_apply_transform",
        "pivots": "set each origin to geometry first, then parent",
        "budget": "decimate or drop to a lower LOD",
        "lods": "add _LOD1/_LOD2 duplicates or a LOD collection",
        "lighting": "blender_add_light / blender_set_active_camera",
        "orphans": "bpy.ops.outliner.orphans_purge(do_recursive=True)",
    }
    actionable = [c for c in report["checks"] if c["severity"] in ("error", "warn")]
    for item in actionable:
        item["fix"] = fixes.get(item["check"], "inspect manually")
    return {"verdict": report["verdict"], "score": report["score"],
            "actionable": actionable,
            "passed": [c["check"] for c in report["checks"] if c["severity"] == "ok"]}


def cmd_generate_texture(params):
    """Procedural texture written to a PNG on disk."""
    return _tex.generate(params)


def cmd_generate_pbr_set(params):
    """Matched BaseColor/Roughness/Metallic/Normal/AO from one seed, optionally
    wired straight into a material."""
    return _tex.generate_pbr_set(params)


def cmd_bake_texture(params):
    """Bake the active material's procedural nodes down to image files."""
    return _tex.bake(params)


def cmd_pack_textures(params):
    """Pack loose images into the .blend so the file is self-contained."""
    return _tex.pack_textures(params)


def cmd_list_images(params):
    return _tex.list_images(params)


def cmd_set_context(params):
    return _ops.set_context(params)


def cmd_select_by(params):
    return _ops.select_by(params)


def cmd_undo(params):
    return _ops.undo(params)


def cmd_redo(params):
    return _ops.redo(params)


def cmd_checkpoint(params):
    return _ops.checkpoint(params)


def cmd_geometry(params):
    return _ops.geometry(params)


def cmd_modifiers(params):
    return _ops.modifiers(params)


def cmd_uv(params):
    return _ops.uv(params)


def cmd_rig(params):
    return _ops.rig(params)


def cmd_pose(params):
    return _ops.pose(params)


def cmd_physics(params):
    return _ops.physics(params)


def cmd_scene_ops(params):
    return _ops.scene_ops(params)


def cmd_render_extras(params):
    return _ops.render_extras(params)


def cmd_batch(params):
    """Run many commands in a single round trip.

    Each step is ``{"command": <handler name>, "params": {...}}``. Results are
    reported per step so one failure does not hide the steps that worked;
    ``stop_on_error`` (default false) keeps going.
    """
    steps = params.get("steps") or params.get("commands")
    if not isinstance(steps, list) or not steps:
        raise CommandError("batch needs a 'steps' list of "
                           "{'command': ..., 'params': {...}} objects")
    if len(steps) > int(params.get("max_steps", 200)):
        raise CommandError(f"batch limited to {params.get('max_steps', 200)} steps")
    stop_on_error = bool(params.get("stop_on_error", False))
    results = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            results.append({"index": index, "ok": False,
                            "error": "step must be an object"})
            continue
        name = step.get("command") or step.get("name")
        step_params = step.get("params") or step.get("arguments") or {}
        if name == "batch":
            results.append({"index": index, "ok": False,
                            "error": "batch cannot be nested"})
            continue
        handler = HANDLERS.get(str(name))
        if handler is None:
            results.append({"index": index, "ok": False, "command": name,
                            "error": f"unknown command {name!r}"})
            if stop_on_error:
                break
            continue
        started = time.time()
        try:
            data = handler(dict(step_params) if isinstance(step_params, dict) else {})
            results.append({"index": index, "ok": True, "command": name,
                            "seconds": round(time.time() - started, 4),
                            "data": data})
        except Exception as exc:  # noqa: BLE001
            results.append({"index": index, "ok": False, "command": name,
                            "error": f"{type(exc).__name__}: {exc}"})
            if stop_on_error:
                break
    return {"steps": len(steps), "executed": len(results),
            "failed": sum(1 for r in results if not r["ok"]),
            "results": results}


HANDLERS = {
    "ping": cmd_ping,
    "get_scene": cmd_get_scene,
    "list_objects": cmd_list_objects,
    "get_object": cmd_get_object,
    "add_primitive": cmd_add_primitive,
    "create_mesh": cmd_create_mesh,
    "delete_objects": cmd_delete_objects,
    "duplicate_objects": cmd_duplicate_objects,
    "rename_object": cmd_rename_object,
    "transform_objects": cmd_transform_objects,
    "apply_transform": cmd_apply_transform,
    "select_objects": cmd_select_objects,
    "join_objects": cmd_join_objects,
    "parent_objects": cmd_parent_objects,
    "create_collection": cmd_create_collection,
    "assign_to_collection": cmd_assign_to_collection,
    "add_modifier": cmd_add_modifier,
    "apply_modifier": cmd_apply_modifier,
    "edit_mesh": cmd_edit_mesh,
    "shade_smooth": cmd_shade_smooth,
    "list_materials": cmd_list_materials,
    "create_material": cmd_create_material,
    "assign_material": cmd_assign_material,
    "set_material_node": cmd_set_material_node,
    "load_image_texture": cmd_load_image_texture,
    "add_light": cmd_add_light,
    "add_camera": cmd_add_camera,
    "look_at": cmd_look_at,
    "set_active_camera": cmd_set_active_camera,
    "set_world": cmd_set_world,
    "set_render_settings": cmd_set_render_settings,
    "render": cmd_render,
    "capture": cmd_capture,
    "file_op": cmd_file_op,
    "import_model": cmd_import_model,
    "export_model": cmd_export_model,
    "set_frame": cmd_set_frame,
    "insert_keyframe": cmd_insert_keyframe,
    "animation_info": cmd_animation_info,
    "list_operators": cmd_list_operators,
    "run_operator": cmd_run_operator,
    "search_api": cmd_search_blender_docs,
    "execute": cmd_execute,
    # --- v2: model validation -------------------------------------------
    "validate": cmd_validate,
    "analyze_mesh": cmd_analyze_mesh,
    "measure": cmd_measure,
    "find_problems": cmd_find_problems,
    # --- v2: textures ----------------------------------------------------
    "generate_texture": cmd_generate_texture,
    "generate_pbr_set": cmd_generate_pbr_set,
    "bake_texture": cmd_bake_texture,
    "pack_textures": cmd_pack_textures,
    "list_images": cmd_list_images,
    # --- v2: context, selection, history --------------------------------
    "set_context": cmd_set_context,
    "select_by": cmd_select_by,
    "undo": cmd_undo,
    "redo": cmd_redo,
    "checkpoint": cmd_checkpoint,
    # --- v2: geometry, uv, rig, physics ----------------------------------
    "geometry": cmd_geometry,
    "modifiers": cmd_modifiers,
    "uv": cmd_uv,
    "rig": cmd_rig,
    "pose": cmd_pose,
    "physics": cmd_physics,
    # --- v2: scene and render --------------------------------------------
    "scene_ops": cmd_scene_ops,
    "render_extras": cmd_render_extras,
    # --- v2: batching ---------------------------------------------------
    "batch": cmd_batch,
}
