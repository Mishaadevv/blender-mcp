"""Animation, rigging and sequencing control for the Blender MCP bridge.

`insert_keyframe` from the original tool set places three keys on a channel.
This module covers the rest: actions and their names, curve shaping, NLA,
drivers, shape keys, pose libraries, physics simulation, the sequencer and
camera moves.
"""

from __future__ import annotations

import math
from typing import Any

import bpy

from .bridge import CommandError, view3d_override

FCURVE_ACTIONS = {"INSERTKEY_NEEDED", "INSERTKEY_REPLACE", "INSERTKEY_AVAILABLE"}
INTERPOLATIONS = {
    "CONSTANT": "CONSTANT", "LINEAR": "LINEAR", "BEZIER": "BEZIER",
    "SINE": "SINE", "QUAD": "QUAD", "CUBIC": "CUBIC", "QUART": "QUART",
    "QUINT": "QUINT", "EXPO": "EXPO", "CIRC": "CIRC", "BACK": "BACK",
    "BOUNCE": "BOUNCE", "ELASTIC": "ELASTIC",
}
EASINGS = {"AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT", "AUTO_CLAMPED"}


def _object(name: str) -> bpy.types.Object:
    ob = bpy.data.objects.get(name)
    if ob is None:
        raise CommandError(f"no object named {name!r}")
    return ob


def _ensure_object_mode() -> None:
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _ad(ob: bpy.types.Object, path: str) -> bpy.types.AnimData:
    if ob.animation_data is None:
        ob.animation_data_create()
    return ob.animation_data


def _action_for(ob: bpy.types.Object, name: str | None = None,
                create: bool = True) -> bpy.types.Action:
    """Blender 4.4+ uses slotted actions; make one that already has a slot."""
    ad = _ad(ob, "")
    if ad.action is None and create:
        action = bpy.data.actions.new(name or f"{ob.name}_Action")
        try:
            slot = action.slots.new(id_type="OBJECT", name=ob.name)
            ad.action = action
            ad.action_slot = slot
        except (AttributeError, TypeError):
            ad.action = action
    elif name and ad.action and ad.action.name != name:
        ad.action.name = name
    if ad.action is None:
        raise CommandError(f"{ob.name} has no action; create one first")
    return ad.action


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def actions_list(params: dict) -> dict:
    rows = []
    for action in bpy.data.actions:
        users = [o.name for o in bpy.data.objects
                 if o.animation_data and o.animation_data.action == action]
        rows.append({
            "name": action.name,
            "users": len(users),
            "used_by": users[:10],
            "frame_range": [round(float(action.frame_range[0]), 2),
                            round(float(action.frame_range[1]), 2)],
            "fcurves": len(action.fcurves) if hasattr(action, "fcurves") else None,
            # a slot is identified by identifier/name_display depending on build
            "slots": [getattr(s, "name_display", None) or getattr(s, "identifier", str(s))
                      for s in getattr(action, "slots", [])],
        })
    return {"count": len(rows), "actions": rows}


def action_manage(params: dict) -> dict:
    action = str(params.get("action", ""))
    op = str(params.get("op", ""))
    if op == "create":
        ob = _object(str(params["object"]))
        _ensure_object_mode()
        with _active(ob), view3d_override():
            _action_for(ob, action or f"{ob.name}_Action")
        return {"action": bpy.data.objects[params["object"]].animation_data.action.name,
                "created": True}
    if op == "assign":
        target = bpy.data.actions.get(action)
        if target is None:
            raise CommandError(f"no action named {action!r}")
        ob = _object(str(params["object"]))
        _ad(ob, "")
        ob.animation_data.action = target
        return {"object": ob.name, "action": target.name}
    if op == "rename":
        act = bpy.data.actions.get(action)
        if act is None:
            raise CommandError(f"no action named {action!r}")
        act.name = str(params.get("new_name", ""))
        return {"renamed_to": act.name}
    if op in {"remove", "delete"}:
        act = bpy.data.actions.get(action)
        if act is None:
            raise CommandError(f"no action named {action!r}")
        bpy.data.actions.remove(act)
        return {"removed": action}
    if op == "copy":
        act = bpy.data.actions.get(action)
        if act is None:
            raise CommandError(f"no action named {action!r}")
        copy = act.copy()
        copy.name = str(params.get("new_name", f"{act.name}_copy"))
        return {"copied_to": copy.name}
    raise CommandError(f"unknown action op {op!r}; use create, assign, rename, "
                       "remove or copy")


def _active(ob: bpy.types.Object):
    """Select and activate one object for the duration of a block.

    ``bpy.context.selected_objects`` does not exist in the bridge's timer
    context, so selection is tracked through the view layer instead.
    """
    import contextlib

    @contextlib.contextmanager
    def ctx():
        view_layer = bpy.context.view_layer
        previous = view_layer.objects.active
        for other in view_layer.objects:
            with contextlib.suppress(RuntimeError):
                other.select_set(False)
        with contextlib.suppress(RuntimeError):
            ob.select_set(True)
        view_layer.objects.active = ob
        try:
            yield
        finally:
            with contextlib.suppress(RuntimeError):
                ob.select_set(False)
            if previous is not None:
                with contextlib.suppress(RuntimeError):
                    view_layer.objects.active = previous

    return ctx()


# --------------------------------------------------------------------------- #
# keyframes and curves
# --------------------------------------------------------------------------- #
def keyframe(params: dict) -> dict:
    """Insert keys on any data path, optionally over a frame range.

    `data_path` accepts plain names ('location', 'scale', 'rotation_euler') and
    full RNA paths ('modifiers["Subsurf"].levels', '["Key_Bone"].rotation_quaternion').
    """
    ob = _object(str(params["object"]))
    data_path = str(params.get("data_path", "location"))
    frame = int(params.get("frame", bpy.context.scene.frame_current))
    index = params.get("index")
    # Blender wants -1, not None, to mean every component
    index = -1 if index is None else int(index)
    _ensure_object_mode()
    with _active(ob):
        if params.get("keyframe_type"):
            ob.keyframe_insert(data_path=data_path, frame=frame, index=index,
                               group=str(params.get("group", "")),
                               keytype=str(params["keyframe_type"]))
        else:
            ob.keyframe_insert(data_path=data_path, frame=frame, index=index,
                               group=str(params.get("group", "")))
        act = _action_for(ob)
    created = 0
    if params.get("frame_end") is not None:
        start = frame
        end = int(params["frame_end"])
        step = int(params.get("step", 1))
        with _active(ob):
            for f in range(start + step, end + 1, step):
                ob.keyframe_insert(data_path=data_path, frame=f, index=index)
                created += 1
    return {"object": ob.name, "data_path": data_path, "frame": frame,
            "extra_keys": created, "action": act.name,
            "interpolation": params.get("interpolation", "BEZIER")}


def keyframe_remove(params: dict) -> dict:
    ob = _object(str(params["object"]))
    data_path = str(params.get("data_path", "location"))
    start = int(params.get("frame_start", 0))
    end = int(params.get("frame_end", 10_000))
    _ensure_object_mode()
    with _active(ob):
        ob.animation_data_clear() if params.get("all") else None
        if not params.get("all"):
            for f in range(start, end + 1):
                try:
                    ob.keyframe_delete(data_path=data_path, frame=f)
                except RuntimeError:
                    continue
    act = ob.animation_data.action if ob.animation_data else None
    return {"object": ob.name, "cleared": bool(params.get("all")),
            "range": [start, end], "action": act.name if act else None}


def curves(params: dict) -> dict:
    """Inspect or reshape the F-curves of an action.

    This is where animation actually gets its character: interpolation, easing,
    handle types, per-key modifiers (noise, limit, generator) and cyclic motion.
    """
    op = str(params.get("op", "list"))
    if op == "list":
        ob = _object(str(params["object"]))
        if not ob.animation_data or not ob.animation_data.action:
            raise CommandError(f"{ob.name} is not animated")
        action = ob.animation_data.action
        rows = []
        for fc in action.fcurves:
            keys = [{"frame": round(k.co[0], 3), "value": round(k.co[1], 4),
                     "interpolation": k.interpolation, "easing": k.easing,
                     "handle_left": [round(v, 3) for v in k.handle_left],
                     "handle_right": [round(v, 3) for v in k.handle_right]}
                    for k in fc.keyframe_points]
            rows.append({"data_path": fc.data_path, "index": fc.array_index,
                         "keys": len(fc.keyframe_points), "keyframes": keys[:120],
                         "modifiers": [m.type for m in fc.modifiers],
                         "extrapolation": fc.extrapolation,
                         "update": fc.update()})
        return {"object": ob.name, "action": action.name, "fcurves": rows}
    if op == "interpolation":
        ob = _object(str(params["object"]))
        action = ob.animation_data.action
        mode = str(params.get("interpolation", "BEZIER")).upper()
        if mode not in INTERPOLATIONS:
            raise CommandError(f"unknown interpolation {mode!r}; use one of "
                               + ", ".join(sorted(INTERPOLATIONS)))
        easing = str(params.get("easing", "AUTO")).upper()
        touched = 0
        for fc in action.fcurves:
            if params.get("data_path") and fc.data_path != params["data_path"]:
                continue
            for key in fc.keyframe_points:
                key.interpolation = mode
                if mode in {"SINE", "QUAD", "CUBIC", "QUART", "QUINT", "EXPO",
                            "CIRC", "BACK", "BOUNCE", "ELASTIC"}:
                    if easing in EASINGS:
                        key.easing = easing
                touched += 1
            fc.update()
        return {"object": ob.name, "interpolation": mode, "keys_changed": touched}
    if op == "handles":
        ob = _object(str(params["object"]))
        handle_type = str(params.get("handle_type", "AUTO_CLAMPED")).upper()
        touched = 0
        for fc in ob.animation_data.action.fcurves:
            for key in fc.keyframe_points:
                key.handle_left_type = handle_type
                key.handle_right_type = handle_type
                touched += 1
            fc.update()
        return {"object": ob.name, "handle_type": handle_type, "keys_changed": touched}
    if op == "add_modifier":
        ob = _object(str(params["object"]))
        kind = str(params.get("modifier", "CYCLES")).upper()
        added = []
        for fc in ob.animation_data.action.fcurves:
            if params.get("data_path") and fc.data_path != params["data_path"]:
                continue
            mod = fc.modifiers.new(kind)
            for key, value in (params.get("properties") or {}).items():
                if hasattr(mod, key):
                    setattr(mod, key, value)
            added.append(f"{fc.data_path}[{fc.array_index}]:{mod.type}")
        return {"object": ob.name, "modifier": kind, "added": added}
    if op == "remove_modifier":
        ob = _object(str(params["object"]))
        removed = 0
        for fc in ob.animation_data.action.fcurves:
            for mod in list(fc.modifiers):
                fc.modifiers.remove(mod)
                removed += 1
        return {"object": ob.name, "removed": removed}
    if op == "shift":
        ob = _object(str(params["object"]))
        before = None
        for fc in ob.animation_data.action.fcurves:
            if before is None:
                before = fc.keyframe_points[0].co[0] if fc.keyframe_points else 0.0
            for key in fc.keyframe_points:
                key.co[0] += float(params.get("frames", 0))
            fc.update()
        return {"object": ob.name, "shifted_frames": params.get("frames", 0),
                "first_key_before": round(before or 0.0, 3)}
    if op == "scale_values":
        ob = _object(str(params["object"]))
        factor = float(params.get("factor", 1.0))
        for fc in ob.animation_data.action.fcurves:
            for key in fc.keyframe_points:
                key.co[1] *= factor
            fc.update()
        return {"object": ob.name, "factor": factor}
    raise CommandError(f"unknown curve op {op!r}; use list, interpolation, handles, "
                       "add_modifier, remove_modifier, shift or scale_values")


# --------------------------------------------------------------------------- #
# NLA and drivers
# --------------------------------------------------------------------------- #
def nla(params: dict) -> dict:
    op = str(params.get("op", "list"))
    ob = _object(str(params["object"]))
    if op == "list":
        tracks = []
        if ob.animation_data:
            for track in ob.animation_data.nla_tracks:
                tracks.append({"name": track.name, "mute": track.mute,
                               "lock": track.lock, "strips": [
                                   {"name": s.name, "action": s.action.name if s.action else None,
                                    "frame_start": round(s.frame_start, 2),
                                    "frame_end": round(s.frame_end, 2),
                                    "blend_type": s.blend_type,
                                    "mute": s.mute,
                                    "influence": round(s.influence, 3)}
                                   for s in track.strips]})
        return {"object": ob.name, "tracks": tracks}
    ad = _ad(ob, "")
    if op == "push":
        # with no explicit action, push whatever is currently driving the object
        wanted = params.get("action") or (ad.action.name if ad.action else None)
        action = bpy.data.actions.get(str(wanted)) if wanted else None
        if action is None:
            raise CommandError(
                f"no action to push for {ob.name!r}. Give an 'action' name, or "
                "key the object first with blender_keyframe_channel."
            )
        if ad.action is action:
            ad.action = None
        track = ad.nla_tracks.new()
        track.name = str(params.get("track", action.name))
        strip = track.strips.new(action.name, int(params.get("frame_start", 1)),
                                 action)
        strip.action = action
        strip.blend_type = str(params.get("blend_type", "REPLACE")).upper()
        strip.frame_end = int(params.get("frame_end",
                                         strip.frame_start + action.frame_range[1]))
        return {"object": ob.name, "track": track.name, "strip": strip.name,
                "action": action.name}
    if op == "mute":
        for track in ad.nla_tracks:
            if track.name == params.get("track"):
                track.mute = bool(params.get("value", True))
        return {"object": ob.name, "track": params.get("track"),
                "mute": bool(params.get("value", True))}
    if op == "remove":
        removed = []
        for track in list(ad.nla_tracks):
            if params.get("track") in (None, track.name):
                removed.append(track.name)
                ad.nla_tracks.remove(track)
        return {"object": ob.name, "removed": removed}
    raise CommandError(f"unknown nla op {op!r}; use list, push, mute or remove")


def driver(params: dict) -> dict:
    op = str(params.get("op", "add"))
    ob = _object(str(params["object"]))
    data_path = str(params.get("data_path", "location"))
    index = params.get("index")
    if op == "add":
        fcurve = ob.driver_add(data_path, index) if index is not None \
            else ob.driver_add(data_path)
        driver = fcurve.driver
        driver.type = str(params.get("type", "SCRIPTED")).upper()
        var_specs = params.get("variables") or [
            {"name": "frame", "type": "SINGLE_PROP",
             "target": 'scene.frame_current', "id_type": "SCENE"}]
        for spec in var_specs:
            var = driver.variables.new()
            var.name = str(spec.get("name", "v"))
            var.type = str(spec.get("type", "SINGLE_PROP")).upper()
            target = var.targets[0]
            target.id_type = str(spec.get("id_type", "SCENE")).upper()
            target.id = (bpy.context.scene if target.id_type == "SCENE"
                         else bpy.data.objects.get(str(spec.get("object", ob.name)))
                         if target.id_type == "OBJECT" else None)
            target.data_path = str(spec.get("target", "frame_current"))
        driver.expression = str(params.get("expression", "frame * 0.1"))
        return {"object": ob.name, "data_path": data_path,
                "expression": driver.expression,
                "variables": [v.name for v in driver.variables]}
    if op == "list":
        found = []
        for fc in ob.animation_data.drivers if ob.animation_data else []:
            found.append({"data_path": fc.data_path, "index": fc.array_index,
                          "expression": fc.driver.expression,
                          "variables": [{"name": v.name, "type": v.type}
                                        for v in fc.driver.variables]})
        return {"object": ob.name, "drivers": found}
    if op == "remove":
        removed = 0
        if ob.animation_data:
            for fc in list(ob.animation_data.drivers):
                ob.driver_remove(fc.data_path, fc.array_index)
                removed += 1
        return {"object": ob.name, "removed": removed}
    raise CommandError(f"unknown driver op {op!r}; use add, list or remove")


# --------------------------------------------------------------------------- #
# shape keys
# --------------------------------------------------------------------------- #
def shape_keys(params: dict) -> dict:
    op = str(params.get("op", "list"))
    ob = _object(str(params["object"]))
    me = ob.data
    if op == "list":
        if not me.shape_keys:
            return {"object": ob.name, "keys": []}
        return {"object": ob.name,
                "keys": [{"name": k.name, "value": round(k.value, 4),
                          "min": k.slider_min, "max": k.slider_max,
                          "relative": k.relative_key.name if k.relative_key else None}
                         for k in me.shape_keys.key_blocks],
                "active": me.shape_keys.use_relative and me.shape_keys.active_shape_key_index}
    if op == "add":
        if not me.shape_keys:
            ob.shape_key_add(name=str(params.get("name", "Key")), from_mix=False)
        name = str(params.get("name", "Key"))
        key = ob.shape_key_add(name=name, from_mix=bool(params.get("from_mix", False)))
        key.slider_min = float(params.get("min", 0.0))
        key.slider_max = float(params.get("max", 1.0))
        return {"object": ob.name, "key": key.name, "vertices": len(key.data)}
    if op == "set":
        if not me.shape_keys:
            raise CommandError(f"{ob.name} has no shape keys")
        key = me.shape_keys.key_blocks.get(str(params.get("key", "")))
        if key is None:
            raise CommandError(f"no shape key {params.get('key')!r}")
        key.value = float(params.get("value", 0.0))
        return {"object": ob.name, "key": key.name, "value": round(key.value, 4)}
    if op == "deform":
        if not me.shape_keys:
            raise CommandError(f"{ob.name} has no shape keys")
        key = me.shape_keys.key_blocks[str(params["key"])]
        moved = 0
        for index, vertex in enumerate(key.data):
            target = params.get("vertices", [])
            if not target or index in target:
                vertex.co += Vector_(params.get("offset", [0, 0, 0]))
                moved += 1
        return {"object": ob.name, "key": key.name, "vertices_moved": moved}
    if op == "remove":
        if not me.shape_keys:
            raise CommandError(f"{ob.name} has no shape keys")
        key = me.shape_keys.key_blocks.get(str(params.get("key", "")))
        if key is None:
            raise CommandError(f"no shape key {params.get('key')!r}")
        ob.shape_key_remove(key)
        return {"object": ob.name, "removed": params.get("key")}
    raise CommandError(f"unknown shape_keys op {op!r}; use list, add, set, deform "
                       "or remove")


def Vector_(values):
    from mathutils import Vector as V
    return V(values)


# --------------------------------------------------------------------------- #
# pose, physics simulation, sequencer, camera
# --------------------------------------------------------------------------- #
def simulate(params: dict) -> dict:
    """Step or bake a physics simulation."""
    op = str(params.get("op", "bake"))
    scene = bpy.context.scene
    if op == "step":
        frames = int(params.get("frames", 10))
        start = scene.frame_current
        with view3d_override():
            for _ in range(frames):
                bpy.ops.screen.frame_forward()
        return {"stepped": frames, "from": start, "to": scene.frame_current}
    if op == "bake":
        frames = int(params.get("frames", 60))
        scene.frame_start = int(params.get("frame_start", scene.frame_start))
        scene.frame_end = scene.frame_start + frames
        baked = []
        for name in (params.get("objects") or []):
            ob = bpy.data.objects.get(name)
            if ob is None:
                continue
            for modifier in ob.modifiers:
                if modifier.type in {"CLOTH", "SOFT_BODY", "FLUID", "PARTICLE_SYSTEM",
                                     "DYNAMIC_PAINT"}:
                    with view3d_override():
                        bpy.context.view_layer.objects.active = ob
                        bpy.ops.object.modifier_apply_as_data(
                            modifier=modifier.name)
                    baked.append({"object": ob.name, "modifier": modifier.name})
        return {"baked": baked, "frame_end": scene.frame_end}
    if op == "reset":
        reset = []
        for name in (params.get("objects") or []):
            ob = bpy.data.objects.get(name)
            if ob is None:
                continue
            with view3d_override():
                bpy.context.view_layer.objects.active = ob
                for modifier in ob.modifiers:
                    if modifier.type in {"CLOTH", "SOFT_BODY"}:
                        try:
                            bpy.ops.ptcache.free_bake_all()
                        except Exception:  # noqa: BLE001
                            pass
                        reset.append({"object": ob.name, "modifier": modifier.name})
        return {"reset": reset}
    raise CommandError(f"unknown simulate op {op!r}; use step, bake or reset")


def sequencer(params: dict) -> dict:
    """Drive the video sequencer: strips, frame range and render settings."""
    op = str(params.get("op", "list"))
    scene = bpy.context.scene
    if not scene.sequence_editor:
        scene.sequence_editor_create()
    editor = scene.sequence_editor
    if op == "list":
        return {"sequences": [{"name": s.name, "type": s.type,
                               "channel": s.channel, "frame_start": s.frame_start,
                               "frame_final_start": s.frame_final_start,
                               "frame_final_end": s.frame_final_end,
                               "filepath": getattr(s, "filepath", None)}
                              for s in editor.strips],
                "frame_start": scene.frame_start, "frame_end": scene.frame_end}
    if op == "add":
        path = str(params.get("path", ""))
        if not path:
            raise CommandError("sequencer add needs a 'path' to an image or movie")
        strip = editor.strips.new_movie(str(params.get("name", "clip")), path,
                                         int(params.get("channel", 1)),
                                         int(params.get("frame_start", 1)))
        if params.get("frame_end"):
            strip.frame_final_duration = int(params["frame_end"]) - int(
                params.get("frame_start", 1))
        return {"strip": strip.name, "frames": [strip.frame_final_start,
                                                 strip.frame_final_end]}
    if op == "set_range":
        scene.frame_start = int(params.get("start", scene.frame_start))
        scene.frame_end = int(params.get("end", scene.frame_end))
        return {"frame_start": scene.frame_start, "frame_end": scene.frame_end}
    if op == "render":
        op2 = str(params.get("mode", "MOVIE")).upper()
        scene.render.image_settings.file_format = op2
        scene.render.filepath = str(params.get("output", "//render_"))
        with view3d_override():
            if op2 == "MOVIE":
                bpy.ops.render.render(animation=True)
            else:
                directory = str(params.get("output", "//frames/frame_"))
                scene.render.filepath = directory
                bpy.ops.render.render(animation=True)
        return {"rendered": True, "output": scene.render.filepath,
                "format": op2, "range": [scene.frame_start, scene.frame_end]}
    if op == "remove":
        name = str(params.get("name", ""))
        if name not in editor.strips:
            raise CommandError(f"no strip named {name!r}")
        editor.strips.remove(editor.strips[name])
        return {"removed": name}
    raise CommandError(f"unknown sequencer op {op!r}; use list, add, set_range, "
                       "render or remove")


def camera_move(params: dict) -> dict:
    """Animate a camera along a path, or hand it a follow constraint.

    ``mode='orbit'`` is the common case: a turntable that the agent can render
    without hand-keying anything.
    """
    camera = bpy.data.objects.get(str(params.get("camera", ""))) or scene_camera()
    if camera is None or camera.type != "CAMERA":
        raise CommandError("no camera given and no active camera")
    mode = str(params.get("mode", "orbit"))
    pivot = params.get("pivot", [0, 0, 0])
    if mode == "orbit":
        frames = int(params.get("frames", 8))
        start = int(params.get("frame_start", bpy.context.scene.frame_start))
        full = params.get("full_turn", True)
        radius = float(params.get("radius", (Vector_(camera.location)
                                             - Vector_(pivot)).length))
        base = math.atan2(camera.location.y - pivot[1], camera.location.x - pivot[0])
        _ensure_object_mode()
        with _active(camera):
            for i in range(frames):
                frame = start + i
                angle = base + (math.tau * i / frames if params.get("full_turn", True) else
                                math.radians(360.0) * i / max(frames - 1, 1))
                camera.location = (pivot[0] + math.cos(angle) * radius,
                                   pivot[1] + math.sin(angle) * radius,
                                   camera.location.z)
                direction = Vector_(pivot) - camera.location
                camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
                camera.keyframe_insert("location", frame=frame)
                camera.keyframe_insert("rotation_euler", frame=frame)
        return {"camera": camera.name, "mode": "orbit", "frames": frames,
                "frame_start": start, "frame_end": start + frames - 1}
    if mode == "constraint":
        target = _object(str(params.get("target", "")))
        kind = str(params.get("constraint", "TRACK_TO")).upper()
        existing = next((c for c in camera.constraints if c.type == kind), None)
        constraint = existing or camera.constraints.new(kind)
        if kind in {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}:
            constraint.target = target
            if kind == "TRACK_TO":
                constraint.track_axis = str(params.get("track_axis", "TRACK_NEGATIVE_Z"))
                constraint.up_axis = str(params.get("up_axis", "UP_Y"))
        elif kind in {"FOLLOW_PATH", "COPY_LOCATION", "COPY_ROTATION"}:
            constraint.target = target
        return {"camera": camera.name, "constraint": kind, "target": target.name}
    if mode == "dolly":
        start = int(params.get("frame_start", bpy.context.scene.frame_start))
        end = int(params.get("frame_end", start + 48))
        from_start = Vector_(params.get("from", list(camera.location)))
        to = Vector_(params.get("to", list(camera.location)))
        _ensure_object_mode()
        with _active(camera):
            for frame in (start, end):
                t = 0.0 if frame == start else 1.0
                camera.location = from_start.lerp(to, t)
                camera.keyframe_insert("location", frame=frame)
        return {"camera": camera.name, "mode": "dolly",
                "range": [start, end],
                "from": [round(v, 3) for v in from_start],
                "to": [round(v, 3) for v in to]}
    raise CommandError(f"unknown camera mode {mode!r}; use orbit, constraint or dolly")


def scene_camera() -> bpy.types.Object | None:
    return bpy.context.scene.camera


def timeline(params: dict) -> dict:
    """Frame range, markers and playback settings."""
    scene = bpy.context.scene
    op = str(params.get("op", "report"))
    if op == "report":
        return {"frame_start": scene.frame_start, "frame_end": scene.frame_end,
                "current": scene.frame_current, "fps": scene.render.fps,
                "fps_base": scene.render.fps_base, "step": scene.frame_step,
                "use_preview_range": scene.use_preview_range,
                "preview_start": scene.frame_preview_start,
                "preview_end": scene.frame_preview_end,
                "markers": [{"frame": m.frame, "name": m.name}
                            for m in scene.timeline_markers]}
    if op == "set":
        if params.get("start") is not None:
            scene.frame_start = int(params["start"])
        if params.get("end") is not None:
            scene.frame_end = int(params["end"])
        if params.get("fps") is not None:
            scene.render.fps = int(params["fps"])
        if params.get("fps_base") is not None:
            scene.render.fps_base = float(params["fps_base"])
        if params.get("step") is not None:
            scene.frame_step = int(params["step"])
        return {"frame_start": scene.frame_start, "frame_end": scene.frame_end,
                "fps": scene.render.fps}
    if op == "add_marker":
        marker = scene.timeline_markers.new(str(params.get("name", "Marker")),
                                            frame=int(params.get("frame", 1)))
        return {"marker": marker.name, "frame": marker.frame}
    if op == "remove_marker":
        name = str(params.get("name", ""))
        if name not in scene.timeline_markers:
            raise CommandError(f"no marker named {name!r}")
        scene.timeline_markers.remove(scene.timeline_markers[name])
        return {"removed": name}
    raise CommandError(f"unknown timeline op {op!r}; use report, set, add_marker "
                       "or remove_marker")
