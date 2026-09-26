"""Extended operations for the Blender MCP bridge: context control, predicate
selection, geometry, UV, rigging, physics, scene and render utilities.

Design note: everything returns plain JSON-serialisable data and raises
``CommandError`` for problems an agent can act on. Operator calls go through
``view3d_override`` because the bridge runs from a timer with no area context.
"""

from __future__ import annotations

import contextlib
import math
import os
from typing import Any, Iterable, Sequence

import bmesh
import bpy
from mathutils import Vector

from .bridge import CommandError, view3d_override


# --------------------------------------------------------------------------- #
# context plumbing
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _active(objects: Sequence[bpy.types.Object]):
    """Make ``objects`` the active + selected set for the duration of a block."""
    view_layer = bpy.context.view_layer
    previous = bpy.context.view_layer.objects.active
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    for ob in objects:
        try:
            ob.select_set(True)
        except RuntimeError:
            continue
    if objects:
        view_layer.objects.active = objects[0]
    try:
        yield
    finally:
        for ob in objects:
            with contextlib.suppress(RuntimeError):
                ob.select_set(False)
        if previous is not None:
            with contextlib.suppress(RuntimeError):
                view_layer.objects.active = previous


def _mesh(objects: Iterable[bpy.types.Object] | None = None) -> list[bpy.types.Object]:
    pool = objects if objects is not None else bpy.data.objects
    out = [o for o in pool if o.type == "MESH" and isinstance(o.data, bpy.types.Mesh)]
    if not out:
        raise CommandError("no mesh objects available")
    return out


def _byname(name: str) -> bpy.types.Object:
    ob = bpy.data.objects.get(name)
    if ob is None:
        raise CommandError(f"no object named {name!r}")
    return ob


def _ensure_object_mode() -> None:
    if bpy.context.mode != "OBJECT":
        with view3d_override():
            bpy.ops.object.mode_set(mode="OBJECT")


def _in_edit(ob: bpy.types.Object, fn):
    """Run ``fn`` with ``ob`` in edit mode, then restore object mode."""
    _ensure_object_mode()
    with _active([ob]), view3d_override():
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            return fn()
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")


def _info(ob: bpy.types.Object) -> dict:
    return {"object": ob.name, "faces": len(ob.data.polygons),
            "verts": len(ob.data.vertices),
            "modifiers": [m.name for m in ob.modifiers]}


# --------------------------------------------------------------------------- #
# context + selection
# --------------------------------------------------------------------------- #
def set_context(params: dict) -> dict:
    """Set mode, active object, active collection and selection in one call.

    Agent workflows constantly need "select these, make that active, switch to
    edit mode" as an atomic step; doing it in three round trips is slow and
    leaves Blender in a half-applied state if one call fails.
    """
    _ensure_object_mode()
    result: dict[str, Any] = {}
    if params.get("collection"):
        col = bpy.data.collections.get(str(params["collection"]))
        if col is None:
            raise CommandError(f"no collection named {params['collection']!r}")
        bpy.context.view_layer.active_layer_collection = (
            bpy.context.view_layer.layer_collection.children[col.name])
        result["active_collection"] = col.name
    if params.get("active"):
        ob = _byname(str(params["active"]))
        for other in bpy.context.view_layer.objects:
            other.select_set(False)
        ob.select_set(True)
        bpy.context.view_layer.objects.active = ob
        result["active"] = ob.name
    if "select" in params:
        names = params["select"]
        names = [names] if isinstance(names, str) else list(names)
        picked = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
        missing = [n for n in names if n not in bpy.data.objects]
        for ob in bpy.context.view_layer.objects:
            ob.select_set(False)
        for ob in picked:
            ob.select_set(True)
        if picked and bpy.context.view_layer.objects.active not in picked:
            bpy.context.view_layer.objects.active = picked[0]
        result["selected"] = [o.name for o in picked]
        if missing:
            result["missing"] = missing
    if params.get("mode") in {"EDIT", "OBJECT", "POSE", "SCULPT"}:
        target = bpy.context.view_layer.objects.active
        if target is None:
            raise CommandError("cannot set a mode without an active object")
        if params["mode"] == "EDIT" and target.type != "MESH":
            raise CommandError(f"{target.name} is {target.type}, not a mesh")
        with view3d_override():
            bpy.ops.object.mode_set(mode=params["mode"])
        result["mode"] = bpy.context.mode
    result["mode_now"] = bpy.context.mode
    return result


def select_by(params: dict) -> dict:
    """Select objects by a predicate instead of by name.

    Names are the first thing an agent gets wrong on a large scene; this lets it
    ask for "every mesh with loose verts" or "every object larger than 2 m"
    instead of listing hundreds of names.
    """
    pool = _mesh()
    mode = str(params.get("by", "type"))
    matched: list[bpy.types.Object] = []
    if mode == "name":
        import fnmatch
        pattern = str(params.get("pattern", "*"))
        matched = [o for o in pool if fnmatch.fnmatch(o.name, pattern)]
    elif mode == "type":
        wanted = str(params.get("object_type", "MESH")).upper()
        matched = [o for o in bpy.data.objects if o.type == wanted]
    elif mode == "material":
        wanted = str(params.get("material", ""))
        matched = [o for o in pool
                   if any(m and m.name == wanted for m in o.data.materials)]
    elif mode == "collection":
        col = bpy.data.collections.get(str(params.get("collection", "")))
        if col is None:
            raise CommandError(f"no collection named {params['collection']!r}")
        matched = list(col.all_objects)
    elif mode == "size":
        limit = float(params.get("min_dimension", 1.0))
        axis = int(params.get("axis", -1))
        if axis < 0:
            matched = [o for o in pool if max(o.dimensions) >= limit]
        else:
            matched = [o for o in pool if o.dimensions[axis] >= limit]
    elif mode == "loose":
        matched = [o for o in pool if _loose_count(o) > 0]
    elif mode == "no_material":
        matched = [o for o in pool if not o.data.materials]
    elif mode == "no_uv":
        matched = [o for o in pool if not o.data.uv_layers]
    elif mode == "empty_parent":
        matched = [o for o in pool if o.parent is None]
    else:
        raise CommandError(f"unknown selection mode {mode!r}; use name, type, "
                           "material, collection, size, loose, no_material, "
                           "no_uv or empty_parent")
    limit_n = int(params.get("limit", 500))
    matched = matched[:limit_n]
    _ensure_object_mode()
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    for ob in matched:
        ob.select_set(True)
    if matched and params.get("make_active", True):
        bpy.context.view_layer.objects.active = matched[0]
    return {"matched": len(matched), "objects": [o.name for o in matched[:200]],
            "truncated": len(matched) >= limit_n}


def _loose_count(ob: bpy.types.Object) -> int:
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    n = sum(1 for v in bm.verts if not v.link_faces)
    bm.free()
    return n


# --------------------------------------------------------------------------- #
# undo / transaction
# --------------------------------------------------------------------------- #
# Blender's undo stack does not survive an Open File. Rewinding across one makes
# it restore handles into freed datablocks, which crashes the process rather
# than raising. Remembering the file at each checkpoint lets us refuse instead.
_UNDO_FILE: dict[str, str | None] = {}


def _undo_op(operator: str, **properties) -> None:
    """Run an ed.* operator, which is picky about the context it is called from.

    Blender raises a bare ``context is incorrect`` RuntimeError from a timer
    callback in some window states; turn that into something an agent can act on.
    """
    try:
        with view3d_override():
            getattr(bpy.ops.ed, operator)(**properties)
    except RuntimeError as exc:
        raise CommandError(
            f"Blender refused {operator}: {exc}. The undo stack is only reachable "
            "from a window with a valid screen context; try again after clicking "
            "into the 3D viewport, or use blender_execute_python to call the "
            "operator directly."
        ) from exc


def undo(params: dict) -> dict:
    _ensure_object_mode()
    current = bpy.data.filepath
    recorded = _UNDO_FILE.get("filepath")
    if recorded is not None and current != recorded:
        raise CommandError(
            "refusing to undo across an Open File: the undo stack was recorded in "
            f"{recorded or '<unsaved>'} but the current file is {current or '<unsaved>'}. "
            "Blender's undo does not survive a file load and rewinding past one "
            "crashes the process. Re-run the step, or checkpoint again in this file."
        )
    _undo_op("undo_push", message="mcp undo point")
    _undo_op("undo")
    return {"undone": True, "is_dirty": bpy.data.is_dirty,
            "filepath": current or None}


def redo(params: dict) -> dict:
    _ensure_object_mode()
    _undo_op("redo")
    return {"redone": True, "is_dirty": bpy.data.is_dirty,
            "filepath": bpy.data.filepath or None}


def checkpoint(params: dict) -> dict:
    _ensure_object_mode()
    label = str(params.get("label", "mcp checkpoint"))
    _undo_op("undo_push", message=label)
    _UNDO_FILE["filepath"] = bpy.data.filepath
    return {"checkpoint": True, "label": label,
            "filepath": bpy.data.filepath or None}


# --------------------------------------------------------------------------- #
# geometry modifiers / operators
# --------------------------------------------------------------------------- #
def geometry(params: dict) -> dict:
    """Geometry operations that do not fit the generic modifier tool."""
    action = str(params.get("operation", ""))
    targets = _mesh()
    names = params.get("objects")
    if names:
        targets = [_byname(n) for n in ([names] if isinstance(names, str) else names)]
    if action == "mirror":
        axis = str(params.get("axis", "X")).upper()
        for ob in targets:
            mod = ob.modifiers.new("Mirror", "MIRROR")
            mod.use_axis = tuple(a == axis for a in "XYZ")
            if params.get("bisect"):
                mod.use_bisect_axis = (True, False, False) if axis == "X" else \
                    (False, True, False) if axis == "Y" else (False, False, True)
            if params.get("clip", True):
                mod.use_clip = True
        return {"operation": "mirror", "axis": axis,
                "objects": [o.name for o in targets]}
    if action == "array":
        count = int(params.get("count", 3))
        offset = params.get("offset", [1.0, 0.0, 0.0])
        for ob in targets:
            mod = ob.modifiers.new("Array", "ARRAY")
            mod.count = count
            mod.use_relative_offset = False
            mod.use_constant_offset = True
            mod.constant_offset_displace = offset
        return {"operation": "array", "count": count, "offset": offset}
    if action == "solidify":
        thickness = float(params.get("thickness", 0.02))
        for ob in targets:
            mod = ob.modifiers.new("Solidify", "SOLIDIFY")
            mod.thickness = thickness
            mod.offset = float(params.get("offset", -1.0))
        return {"operation": "solidify", "thickness": thickness}
    if action == "screw":
        ob = targets[0]
        _in_edit(ob, lambda: bpy.ops.mesh.screw(
            angle=float(params.get("angle", math.pi)),
            steps=int(params.get("steps", 12)),
            radius=float(params.get("radius", 0.5)),
            use_merge_vertices=bool(params.get("merge", True))))
        return {"operation": "screw", **_info(ob)}
    if action == "spin":
        ob = targets[0]
        _in_edit(ob, lambda: bpy.ops.mesh.spin(
            steps=int(params.get("steps", 12)),
            angle=float(params.get("angle", math.pi)),
            axis=params.get("axis", "Z"),
            use_merge=bool(params.get("merge", True))))
        return {"operation": "spin", **_info(ob)}
    if action == "weld":
        dist = float(params.get("distance", 1e-4))
        merged = 0
        for ob in targets:
            def run(ob=ob, dist=dist):
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.mesh.remove_doubles(threshold=dist)
            _in_edit(ob, run)
            merged += 1
        return {"operation": "weld", "distance": dist, "objects": merged}
    if action == "recalc_normals":
        for ob in targets:
            _in_edit(ob, lambda: bpy.ops.mesh.normals_make_consistent(inside=False))
        return {"operation": "recalc_normals", "objects": [o.name for o in targets]}
    if action == "flip_normals":
        for ob in targets:
            _in_edit(ob, lambda: bpy.ops.mesh.flip_normals())
        return {"operation": "flip_normals", "objects": [o.name for o in targets]}
    if action == "triangulate":
        method = str(params.get("method", "BEAUTY"))
        for ob in targets:
            _in_edit(ob, lambda m=method: bpy.ops.mesh.triangulate(quad_method=m,
                                                                   ngon_method=m))
        return {"operation": "triangulate", "method": method}
    if action == "decimate":
        ratio = float(params.get("ratio", 0.5))
        for ob in targets:
            mod = ob.modifiers.new("Decimate", "DECIMATE")
            mod.ratio = ratio
        return {"operation": "decimate", "ratio": ratio}
    if action == "remesh":
        mode = str(params.get("mode", "VOXEL")).upper()
        for ob in targets:
            mod = ob.modifiers.new("Remesh", "REMESH")
            mod.mode = mode
            if mode == "VOXEL":
                mod.voxel_size = float(params.get("voxel_size", 0.05))
            else:
                mod.adaptivity = float(params.get("adaptivity", 0.0))
        return {"operation": "remesh", "mode": mode}
    if action == "wireframe":
        for ob in targets:
            mod = ob.modifiers.new("Wireframe", "WIREFRAME")
            mod.thickness = float(params.get("thickness", 0.01))
        return {"operation": "wireframe"}
    if action == "shrink_fatten":
        offset = float(params.get("offset", 0.0))
        for ob in targets:
            mod = ob.modifiers.new("ShrinkFatten", "SHRINKFATTEN")
            mod.offset = offset
        return {"operation": "shrink_fatten", "offset": offset}
    if action == "bevel_all":
        width = float(params.get("width", 0.005))
        for ob in targets:
            mod = ob.modifiers.new("Bevel", "BEVEL")
            mod.width = width
            mod.segments = int(params.get("segments", 2))
            mod.limit_method = "ANGLE"
        return {"operation": "bevel_all", "width": width}
    raise CommandError(f"unknown geometry operation {action!r}")


def modifiers(params: dict) -> dict:
    """Inspect, reorder, mute or remove modifiers across objects."""
    action = str(params.get("action", "list"))
    names = params.get("objects")
    targets = [_byname(n) for n in ([names] if isinstance(names, str) else names)] if names \
        else [o for o in bpy.data.objects if o.type == "MESH"]
    changed = []
    for ob in targets:
        mods = list(ob.modifiers)
        if action == "list":
            changed.append({"object": ob.name,
                            "modifiers": [{"name": m.name, "type": m.type,
                                           "show_viewport": m.show_viewport,
                                           "show_render": m.show_render}
                                          for m in mods]})
        elif action == "mute":
            for m in mods:
                m.show_viewport = bool(params.get("value", True))
            changed.append({"object": ob.name, "muted": params.get("value", True)})
        elif action == "remove":
            target = str(params.get("modifier", ""))
            if target in {"all", ""}:
                for m in mods:
                    ob.modifiers.remove(m)
            else:
                if target not in ob.modifiers:
                    raise CommandError(f"{ob.name} has no modifier {target!r}")
                ob.modifiers.remove(ob.modifiers[target])
            changed.append({"object": ob.name, "removed": target or "all"})
        elif action == "move":
            target = str(params.get("modifier", ""))
            if target not in ob.modifiers:
                raise CommandError(f"{ob.name} has no modifier {target!r}")
            index = int(params.get("index", 0))
            while ob.modifiers.find(target) > index:
                bpy.ops.object.modifier_move_down(modifier=target)
            changed.append({"object": ob.name, "moved": target, "index": index})
        elif action == "apply":
            target = str(params.get("modifier", ""))
            with _active([ob]):
                if target in {"all", ""}:
                    for m in list(ob.modifiers):
                        bpy.ops.object.modifier_apply(modifier=m.name)
                else:
                    if target not in ob.modifiers:
                        raise CommandError(f"{ob.name} has no modifier {target!r}")
                    bpy.ops.object.modifier_apply(modifier=target)
            changed.append({"object": ob.name, "applied": target or "all"})
        else:
            raise CommandError(f"unknown modifier action {action!r}")
    return {"action": action, "results": changed}


# --------------------------------------------------------------------------- #
# UV
# --------------------------------------------------------------------------- #
def uv(params: dict) -> dict:
    """Unwrapping and UV maintenance that the node tools do not cover."""
    action = str(params.get("action", "report"))
    names = params.get("objects")
    targets = [_byname(n) for n in ([names] if isinstance(names, str) else names)] if names \
        else _mesh()
    for ob in targets:
        if not ob.data.uv_layers:
            ob.data.uv_layers.new(name="UVMap")
    if action == "report":
        rows = []
        for ob in targets:
            uv = ob.data.uv_layers.active
            if uv is None:
                rows.append({"object": ob.name, "uv": None})
                continue
            us = [d.uv[0] for d in uv.data]
            vs = [d.uv[1] for d in uv.data]
            rows.append({"object": ob.name, "layer": uv.name,
                         "loops": len(uv.data),
                         "u_range": [round(min(us), 4), round(max(us), 4)],
                         "v_range": [round(min(vs), 4), round(max(vs), 4)],
                         "tiling": max(us) > 1.001 or min(us) < -0.001})
        return {"action": action, "meshes": rows}
    if action == "smart_project":
        angle = math.radians(float(params.get("angle_limit_deg", 66)))
        margin = float(params.get("margin", 0.002))
        scale_to_bounds = bool(params.get("scale_to_bounds", False))
        for ob in targets:
            def run(angle=angle, margin=margin, scale_to_bounds=scale_to_bounds):
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.uv.smart_project(angle_limit=angle, island_margin=margin,
                                         scale_to_bounds=scale_to_bounds)
                bpy.ops.object.mode_set(mode="OBJECT")
            _in_edit(ob, run)
        return {"action": action, "objects": [o.name for o in targets]}
    if action == "unwrap":
        method = str(params.get("method", "ANGLE_BASED")).upper()
        margin = float(params.get("margin", 0.002))
        for ob in targets:
            def run(method=method, margin=margin):
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.uv.unwrap(method=method, margin=margin)
                bpy.ops.object.mode_set(mode="OBJECT")
            _in_edit(ob, run)
        return {"action": action, "method": method}
    if action == "pack_islands":
        for ob in targets:
            def run():
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.uv.pack_islands(margin=float(params.get("margin", 0.002)))
                bpy.ops.object.mode_set(mode="OBJECT")
            _in_edit(ob, run)
        return {"action": action}
    if action == "remove_doubles":
        dist = float(params.get("distance", 1e-4))
        for ob in targets:
            def run(dist=dist):
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                bpy.ops.uv.remove_doubles(distance=dist)
                bpy.ops.object.mode_set(mode="OBJECT")
            _in_edit(ob, run)
        return {"action": action, "distance": dist}
    if action == "scale":
        sx = float(params.get("x", 1.0))
        sy = float(params.get("y", sx))
        for ob in targets:
            uv = ob.data.uv_layers.active
            for item in uv.data:
                item.uv = (item.uv[0] * sx, item.uv[1] * sy)
        return {"action": action, "scale": [sx, sy]}
    if action == "center":
        for ob in targets:
            uv = ob.data.uv_layers.active
            us = [d.uv[0] for d in uv.data]
            vs = [d.uv[1] for d in uv.data]
            cu, cv = (min(us) + max(us)) / 2, (min(vs) + max(vs)) / 2
            for item in uv.data:
                item.uv = (item.uv[0] - cu, item.uv[1] - cv)
        return {"action": action}
    raise CommandError(f"unknown uv action {action!r}; use report, smart_project, "
                       "unwrap, pack_islands, remove_doubles, scale or center")


# --------------------------------------------------------------------------- #
# rigging
# --------------------------------------------------------------------------- #
def rig(params: dict) -> dict:
    """Armature creation, bones and automatic weights."""
    action = str(params.get("action", ""))
    name = str(params.get("name", "Rig"))
    if action == "create_armature":
        ob = bpy.data.objects.get(name)
        if ob and ob.type == "ARMATURE":
            return {"armature": ob.name, "created": False}
        arm_data = bpy.data.armatures.new(name)
        arm = bpy.data.objects.new(name, arm_data)
        bpy.context.scene.collection.objects.link(arm)
        arm.show_in_front = True
        return {"armature": arm.name, "created": True,
                "bones": len(arm_data.bones)}
    if action == "add_bone":
        arm = bpy.data.objects.get(name)
        if arm is None or arm.type != "ARMATURE":
            raise CommandError(f"no armature named {name!r}")
        bone_name = str(params.get("bone", "Bone"))
        head = params.get("head", [0.0, 0.0, 0.0])
        tail = params.get("tail", [0.0, 0.0, 0.1])
        _ensure_object_mode()
        with _active([arm]), view3d_override():
            bpy.ops.object.mode_set(mode="EDIT")
            bone = arm.data.edit_bones.new(bone_name)
            bone.head = Vector(head)
            bone.tail = Vector(tail)
            if params.get("parent"):
                parent = arm.data.edit_bones.get(str(params["parent"]))
                if parent:
                    bone.parent = parent
            bpy.ops.object.mode_set(mode="OBJECT")
        return {"armature": arm.name, "bone": bone_name,
                "bones": [b.name for b in arm.data.bones]}
    if action == "skin":
        target = _byname(str(params.get("object", "")))
        arm = bpy.data.objects.get(name)
        if arm is None or arm.type != "ARMATURE":
            raise CommandError(f"no armature named {name!r}")
        _ensure_object_mode()
        with _active([arm, target]), view3d_override():
            bpy.ops.object.parent_set(type="ARMATURE_AUTO")
        return {"object": target.name, "armature": arm.name,
                "vertex_groups": len(target.vertex_groups),
                "parent": target.parent.name if target.parent else None}
    if action == "clear":
        for ob in bpy.data.objects:
            if ob.type == "MESH" and ob.parent and ob.parent.type == "ARMATURE":
                groups = ob.vertex_groups
                ob.parent = None
                for g in list(groups):
                    groups.remove(g)
        return {"cleared": True}
    raise CommandError(f"unknown rig action {action!r}; use create_armature, "
                       "add_bone, skin or clear")


def pose(params: dict) -> dict:
    """Pose bones: location, rotation, scale, constraints."""
    arm_name = str(params.get("armature", ""))
    arm = bpy.data.objects.get(arm_name)
    if arm is None or arm.type != "ARMATURE":
        raise CommandError(f"no armature named {arm_name!r}")
    bone_name = str(params.get("bone", ""))
    bone = arm.pose.bones.get(bone_name)
    if bone is None:
        raise CommandError(f"{arm_name} has no pose bone {bone_name!r}")
    if params.get("location"):
        bone.location = params["location"]
    if params.get("rotation_degrees"):
        bone.rotation_euler = tuple(math.radians(a)
                                    for a in params["rotation_degrees"])
    if params.get("scale"):
        bone.scale = params["scale"]
    _ensure_object_mode()
    bpy.context.view_layer.update()
    return {"armature": arm_name, "bone": bone_name,
            "location": [round(v, 5) for v in bone.location],
            "rotation_deg": [round(math.degrees(v), 3) for v in bone.rotation_euler]}


# --------------------------------------------------------------------------- #
# physics
# --------------------------------------------------------------------------- #
def physics(params: dict) -> dict:
    """Rigid body, cloth, soft body, collision and force fields."""
    action = str(params.get("action", ""))
    names = params.get("objects")
    targets = [_byname(n) for n in ([names] if isinstance(names, str) else names)] if names \
        else _mesh()
    if action == "rigid_body":
        body_type = str(params.get("type", "ACTIVE")).upper()
        for ob in targets:
            _ensure_object_mode()
            with _active([ob]), view3d_override():
                bpy.ops.rigidbody.object_add(type=body_type)
            if params.get("mass") is not None:
                ob.rigid_body.mass = float(params["mass"])
            if params.get("collision_shape"):
                ob.rigid_body.collision_shape = str(params["collision_shape"]).upper()
        return {"action": action, "type": body_type,
                "objects": [o.name for o in targets]}
    if action == "passive":
        bpy.ops.rigidbody.world_add()
        world = bpy.context.scene.rigidbody_world
        world.time_scale = float(params.get("time_scale", 1.0))
        world.substeps_per_frame = int(params.get("substeps", 10))
        world.solver_iterations = int(params.get("solver_iterations", 10))
        return {"action": action, "collection": world.collection.name,
                "enabled": world.enabled}
    if action == "bake":
        scene = bpy.context.scene
        frames = int(params.get("frames", 60))
        start = scene.frame_start
        scene.frame_end = start + frames
        bpy.ops.ptcache.bake_all(bake=True)
        return {"action": action, "baked": True, "frame_end": scene.frame_end}
    if action == "cloth":
        for ob in targets:
            _ensure_object_mode()
            with _active([ob]), view3d_override():
                bpy.ops.object.modifier_add(type="CLOTH")
            mod = ob.modifiers.get("Cloth")
            if mod and mod.type == "CLOTH":
                s = mod.settings
                s.quality = int(params.get("quality", 5))
                s.mass = float(params.get("mass", 0.3))
                if params.get("preset"):
                    preset = str(params["preset"]).upper()
                    attr = getattr(s, f"preset_{preset.lower()}", None)
                    if attr is not None:
                        attr(s)
        return {"action": action, "objects": [o.name for o in targets]}
    if action == "collision":
        for ob in targets:
            _ensure_object_mode()
            with _active([ob]), view3d_override():
                bpy.ops.object.modifier_add(type="COLLISION")
        return {"action": action, "objects": [o.name for o in targets]}
    if action == "soft_body":
        for ob in targets:
            _ensure_object_mode()
            with _active([ob]), view3d_override():
                bpy.ops.object.modifier_add(type="SOFT_BODY")
        return {"action": action, "objects": [o.name for o in targets]}
    if action == "force_field":
        field_type = str(params.get("type", "FORCE")).upper()
        name = str(params.get("name", "Field"))
        bpy.ops.object.effector_add(type=field_type, location=params.get("location", [0, 0, 1]))
        ob = bpy.context.active_object
        ob.name = name
        if params.get("strength") is not None:
            ob.field.strength = float(params["strength"])
        return {"action": action, "field": ob.name, "type": field_type}
    if action == "rigid_body_constraint":
        raise CommandError("use the generic operator runner for "
                           "bpy.ops.rigidbody.constraint_add")
    raise CommandError(f"unknown physics action {action!r}")


# --------------------------------------------------------------------------- #
# scene organisation
# --------------------------------------------------------------------------- #
def scene_ops(params: dict) -> dict:
    """Hierarchy, instancing, purging and dependency-graph introspection."""
    action = str(params.get("action", ""))
    if action == "duplicate_hierarchy":
        root = _byname(str(params.get("object", "")))
        new_root = root.copy()
        new_root.data = root.data if root.data else None
        new_root.name = str(params.get("new_name", root.name + "_copy"))
        bpy.context.scene.collection.objects.link(new_root)
        mapping = {root: new_root}
        stack = [root]
        while stack:
            current = stack.pop()
            for child in current.children:
                copy = child.copy()
                copy.data = child.data
                copy.name = f"{child.name}_copy"
                new_root.users_collection[0].objects.link(copy)
                copy.parent = mapping[current]
                copy.matrix_parent_inverse = child.matrix_parent_inverse.copy()
                mapping[child] = copy
                stack.append(child)
        return {"root": new_root.name, "copied": len(mapping)}
    if action == "instance_collection":
        col = bpy.data.collections.get(str(params.get("collection", "")))
        if col is None:
            raise CommandError(f"no collection named {params['collection']!r}")
        _ensure_object_mode()
        empty = bpy.data.objects.new(f"{col.name}_Instance", None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = col
        empty.location = params.get("location", [0.0, 0.0, 0.0])
        bpy.context.scene.collection.objects.link(empty)
        return {"instance": empty.name, "collection": col.name}
    if action == "apply_instances":
        count = 0
        for ob in [o for o in bpy.data.objects if o.instance_type == "COLLECTION"]:
            _ensure_object_mode()
            with _active([ob]), view3d_override():
                bpy.ops.object.duplicates_make_real()
                bpy.data.objects.remove(ob, do_unlink=True)
            count += 1
        return {"applied": count}
    if action == "purge_orphans":
        _ensure_object_mode()
        removed = {"meshes": 0, "materials": 0, "images": 0}
        for _ in range(4):
            for attr, key in (("meshes", "meshes"), ("materials", "materials"),
                              ("images", "images")):
                for block in list(getattr(bpy.data, attr)):
                    if block.users == 0 and not getattr(block, "use_fake_user", False):
                        removed[key] += 1
                        getattr(bpy.data, attr).remove(block)
        return {"purged": removed}
    if action == "depsgraph":
        dg = bpy.context.evaluated_depsgraph_get()
        rows = []
        for ob in bpy.data.objects:
            ev = ob.evaluated_get(dg)
            try:
                meshes = sum(1 for _ in ev.data.vertices) if ev.type == "MESH" else 0
            except Exception:  # noqa: BLE001
                meshes = -1
            rows.append({"object": ob.name, "type": ob.type,
                         "evaluated_verts": meshes,
                         "modifiers": len(ob.modifiers),
                         "depsgraph_parents": [p.name for p in ev.parents]})
        return {"objects": len(rows), "rows": rows[:200]}
    if action == "addons":
        import addon_utils
        return {"enabled": sorted(m.__name__ for m in addon_utils.modules()
                                 if addon_utils.check(m.__name__)[1]),
                "available": sorted(m.__name__ for m in addon_utils.modules())[:80]}
    if action == "memory":
        return {"meshes": len(bpy.data.meshes), "objects": len(bpy.data.objects),
                "materials": len(bpy.data.materials), "images": len(bpy.data.images),
                "armatures": len(bpy.data.armatures),
                "actions": len(bpy.data.actions),
                "texts": len(bpy.data.texts)}
    raise CommandError(f"unknown scene action {action!r}")


# --------------------------------------------------------------------------- #
# render extras
# --------------------------------------------------------------------------- #
def render_extras(params: dict) -> dict:
    """Render passes, turntables and clay/wireframe previews."""
    action = str(params.get("action", ""))
    scene = bpy.context.scene
    if action == "turntable":
        frames = int(params.get("frames", 8))
        start = scene.frame_start
        out_dir = params.get("output_dir") or os.path.join(
            os.path.expanduser("~"), "BlenderMCP_Renders")
        os.makedirs(out_dir, exist_ok=True)
        scene.render.resolution_x = int(params.get("resolution", 960))
        scene.render.resolution_y = int(params.get("resolution", 960)) // 2
        scene.render.image_settings.file_format = "PNG"
        written = []
        camera = scene.camera
        if camera is None:
            raise CommandError("no active camera; set one before a turntable")
        pivot = Vector(params.get("pivot", [0.0, 0.0, 0.0]))
        radius = (camera.location - pivot).length
        base_ang = math.atan2(camera.location.y - pivot.y,
                              camera.location.x - pivot.x)
        for i in range(frames):
            ang = base_ang + math.tau * i / frames
            camera.location = pivot + Vector((math.cos(ang) * radius,
                                             math.sin(ang) * radius, 0.0))
            direction = pivot - camera.location
            camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            scene.render.filepath = os.path.join(out_dir, f"turntable_{i:03d}.png")
            bpy.ops.render.render(write_still=True)
            written.append(scene.render.filepath)
        camera.location = pivot + Vector((math.cos(base_ang) * radius,
                                          math.sin(base_ang) * radius, 0.0))
        return {"action": action, "frames": written, "output_dir": out_dir}
    if action == "clay":
        previous = scene.render.engine
        scene.render.engine = "BLENDER_WORKBENCH"
        shading = scene.display.shading
        previous_light = shading.light
        previous_color = shading.color_type
        shading.light = "STUDIO"
        shading.color_type = "SINGLE"
        out = os.path.join(params.get("output_dir", os.path.expanduser("~")),
                           "ZAZ_clay.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        scene.render.filepath = out
        scene.render.image_settings.file_format = "PNG"
        bpy.ops.render.render(write_still=True)
        scene.render.engine = previous
        shading.light = previous_light
        shading.color_type = previous_color
        return {"action": action, "file": out}
    if action == "passes":
        wanted = params.get("passes", ["Z", "NORMAL"])
        available = [i.identifier for i in
                     scene.view_layers[0].cycles.pass_layout.bl_rna.properties[
                         "pass_type" and "name"].enum_items] if False else None
        added = []
        vl = scene.view_layers[0]
        for name in wanted:
            try:
                vl.use_pass_z = True if str(name).upper() == "Z" else vl.use_pass_z
                if str(name).upper() == "NORMAL":
                    vl.use_pass_normal = True
                if str(name).upper() == "AO":
                    vl.use_pass_ambient_occlusion = True
                if str(name).upper() in {"MIST", "CRYPTOMATTE"}:
                    vl.use_pass_mist = True
                if str(name).upper() == "COMBINED":
                    vl.use_pass_combined = True
                added.append(str(name).upper())
            except Exception:  # noqa: BLE001
                continue
        del available
        return {"action": action, "enabled_passes": added}
    if action == "contact_sheet":
        images = list(params.get("images") or [])
        if not images:
            raise CommandError("contact_sheet needs an 'images' list of paths")
        cols = int(params.get("columns", 3))
        rows = (len(images) + cols - 1) // cols
        cell = int(params.get("cell", 420))
        canvas = bpy.data.images.new("ContactSheet", width=cols * cell, height=rows * cell,
                                     alpha=False)
        buf = [0.0] * (cols * cell * rows * cell * 4)
        for index, path in enumerate(images):
            if not os.path.isfile(path):
                continue
            src = bpy.data.images.load(path)
            scale = min(cell / src.size[0], cell / src.size[1])
            sw, sh = int(src.size[0] * scale), int(src.size[1] * scale)
            pixels = list(src.pixels)
            cx = (index % cols) * cell + (cell - sw) // 2
            cy = (rows - 1 - index // cols) * cell + (cell - sh) // 2
            for y in range(sh):
                row = (cy + y) * cols * cell
                for x in range(sw):
                    si = (y * sw + x) * 4
                    di = ((row + cx + x) * 4)
                    buf[di:di + 4] = pixels[si:si + 4]
            bpy.data.images.remove(src)
        canvas.pixels.foreach_set(buf)
        canvas.update()
        out = params.get("output") or os.path.join(os.path.expanduser("~"),
                                                   "contact_sheet.png")
        canvas.filepath_raw = out
        canvas.file_format = "PNG"
        canvas.save()
        bpy.data.images.remove(canvas)
        return {"action": action, "file": out, "tiles": len(images)}
    raise CommandError(f"unknown render action {action!r}; use turntable, clay, "
                       "passes or contact_sheet")
