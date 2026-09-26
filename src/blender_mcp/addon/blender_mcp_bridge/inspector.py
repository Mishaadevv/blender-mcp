"""Project-wide introspection: every setting, every datablock, every file.

The complaint this answers is "the agent cannot see what the project is
actually set to". Everything here is read-only except `settings_set`, which
requires an explicit dotted path, so nothing changes by accident.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import bpy

from .bridge import CommandError, view3d_override

def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


# Filesystem browsing is confined to these roots unless `allow_anywhere` is set.
# An agent that can read the user's whole disk by default is a liability, not a
# feature.
DEFAULT_ROOTS = [
    os.path.expanduser("~"),
    os.path.join(os.path.expanduser("~"), "BlenderMCP_Assets"),
    bpy.utils.user_resource("SCRIPTS", path="") or "",
    tempfile_dir(),
]


def _in_allowed(path: str, roots: list[str]) -> bool:
    target = os.path.abspath(path)
    for root in roots:
        if not root:
            continue
        root = os.path.abspath(root)
        if target == root or target.startswith(root + os.sep):
            return True
    return False


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def _safe(obj: Any, attribute: str, default: Any = None) -> Any:
    """Read an attribute that may not exist in this Blender build or addon set.

    Preferences and render settings move between releases, and a report that
    raises because one field was renamed is worse than one with a null.
    """
    try:
        value = getattr(obj, attribute)
    except Exception:  # noqa: BLE001
        return default
    if callable(value):
        return default
    return value


def _render_settings() -> dict:
    r = bpy.context.scene.render
    e = bpy.context.scene.eevee
    c = bpy.context.scene.view_settings
    out = {
        "engine": r.engine, "resolution_x": r.resolution_x, "resolution_y": r.resolution_y,
        "resolution_percentage": r.resolution_percentage, "pixel_aspect_x": r.pixel_aspect_x,
        "fps": r.fps, "fps_base": r.fps_base, "frame_start": bpy.context.scene.frame_start,
        "frame_end": bpy.context.scene.frame_end, "filepath": r.filepath,
        "file_format": r.image_settings.file_format, "color_mode": r.image_settings.color_mode,
        "color_depth": r.image_settings.color_depth, "compression": r.image_settings.compression,
        "film_transparent": r.film_transparent, "use_motion_blur": _safe(r, "use_motion_blur"),
        "use_simplify": r.use_simplify,
        "simplify_subdivision": _safe(r, "simplify_subdivision_render"),
        "use_freestyle": _safe(r, "use_freestyle"), "use_stamp": _safe(r, "use_stamp"),
        "threads_mode": _safe(r, "threads_mode"), "threads": _safe(r, "threads"),
    }
    cycles = _safe(r, "cycles")
    if cycles is not None:
        out["cycles"] = {
            "samples": _safe(cycles, "samples"),
            "preview_samples": _safe(cycles, "preview_samples"),
            "use_denoising": _safe(cycles, "use_denoising"),
            "denoiser": _safe(cycles, "denoiser"),
            "max_bounces": _safe(cycles, "max_bounces"),
            "diffuse_bounces": _safe(cycles, "diffuse_bounces"),
            "glossy_bounces": _safe(cycles, "glossy_bounces"),
            "transmission_bounces": _safe(cycles, "transmission_bounces"),
            "volume_bounces": _safe(cycles, "volume_bounces"),
            "transparent_max_bounces": _safe(cycles, "transparent_max_bounces"),
            "use_adaptive_sampling": _safe(cycles, "use_adaptive_sampling"),
            "adaptive_threshold": _safe(cycles, "adaptive_threshold"),
            "device": _safe(cycles, "device"),
        }
    else:
        out["cycles"] = None
    if _safe(e, "taa_render_samples") is not None:
        out["eevee"] = {
            "taa_render_samples": e.taa_render_samples, "taa_samples": _safe(e, "taa_samples"),
            "use_raytracing": _safe(e, "use_raytracing"),
            "use_shadows": _safe(e, "use_shadows"),
            "use_volumetric_shadows": _safe(e, "use_volumetric_shadows"),
            "shadow_ray_count": _safe(e, "shadow_ray_count"),
            "shadow_step_count": _safe(e, "shadow_step_count"),
        }
    out["color_management"] = {
        "view_transform": _safe(c, "view_transform"), "look": _safe(c, "look"),
        "exposure": _safe(c, "exposure"), "gamma": _safe(c, "gamma"),
        "use_curve_mapping": _safe(c, "use_curve_mapping"),
        "display_device": _safe(bpy.context.scene.display_settings, "display_device"),
    }
    return out


def _scene_settings() -> dict:
    s = bpy.context.scene
    unit = s.unit_settings
    world = s.world
    return {
        "name": s.name, "frame_current": s.frame_current, "use_preview_range": s.use_preview_range,
        "gravity": [round(v, 4) for v in s.gravity],
        "unit_system": unit.system, "scale_length": unit.scale_length,
        "length_unit": unit.length_unit, "system_rotation": unit.system_rotation,
        "use_gravity": s.use_gravity,
        "render_motion_blur_shutter": round(s.render.motion_blur_shutter, 3),
        "simplify_subdivision": s.render.simplify_subdivision_render,
        "world": world.name if world else None,
        "world_nodes": len(world.node_tree.nodes) if world and world.node_tree else 0,
        "camera": s.camera.name if s.camera else None,
        "view_layers": [{"name": vl.name, "use": vl.use,
                         "samples": getattr(vl, "samples", None),
                         "use_pass_z": vl.use_pass_z,
                         "use_pass_normal": getattr(vl, "use_pass_normal", None),
                         "use_pass_ambient_occlusion": getattr(vl, "use_pass_ambient_occlusion", None)}
                        for vl in s.view_layers],
        "compositor": bool(s.use_nodes),
        "compositor_nodes": len(s.node_tree.nodes) if s.use_nodes and s.node_tree else 0,
        "collections": len(bpy.data.collections),
    }


def _object_settings() -> dict:
    counts: dict[str, int] = {}
    for ob in bpy.data.objects:
        counts[ob.type] = counts.get(ob.type, 0) + 1
    return {
        "counts": counts,
        "total": len(bpy.data.objects),
        "meshes": len(bpy.data.meshes), "materials": len(bpy.data.materials),
        "images": len(bpy.data.images), "armatures": len(bpy.data.armatures),
        "actions": len(bpy.data.actions), "node_groups": len(bpy.data.node_groups),
        "textures": len(bpy.data.textures), "brushes": len(bpy.data.brushes),
        "particle_systems": sum(len(o.particle_systems) for o in bpy.data.objects),
        "constraints": sum(len(o.constraints) for o in bpy.data.objects),
        "modifiers": sum(len(o.modifiers) for o in bpy.data.objects),
        "shape_keys": sum(1 for m in bpy.data.meshes
                          if m.shape_keys and len(m.shape_keys.key_blocks)),
        "total_faces": sum(len(o.data.polygons) for o in bpy.data.objects
                           if o.type == "MESH"),
        "total_verts": sum(len(o.data.vertices) for o in bpy.data.objects
                           if o.type == "MESH"),
    }


def _preferences() -> dict:
    prefs = bpy.context.preferences
    view = prefs.view
    edit = prefs.edit
    filepaths = _safe(prefs, "filepaths")
    system = prefs.system
    return {
        "filepath": bpy.data.filepath, "is_dirty": bpy.data.is_dirty,
        "use_save_versions": _safe(filepaths, "use_save_versions"),
        "save_version": _safe(filepaths, "save_version"),
        "autosave_minutes": _safe(filepaths, "auto_save_time"),
        "undo_steps": _safe(edit, "undo_steps"),
        "undo_memory": _safe(edit, "undo_memory_limit"),
        "use_auto_key": _safe(edit, "use_keyframe_insert_auto"),
        "theme": _safe(view, "theme"),
        "display_device": _safe(system, "display_device"),
        "compute_device_type": _safe(_safe(system, "cycles", None),
                                     "compute_device_type"),
        "addons_enabled": len(list(_safe(prefs, "addons", []) or [])),
        "language": _safe(view, "language"),
        "temporary_directory": bpy.app.tempdir,
        "blend_paths": list(bpy.utils.script_paths()),
        "python": sys.version.split()[0],
        "blender": bpy.app.version_string,
        "binary": bpy.app.binary_path,
        "background": bpy.app.background,
    }


def settings_report(params: dict) -> dict:
    """Everything the project is currently set to, in one payload."""
    groups = params.get("groups")
    wanted = set(groups) if groups else None

    def keep(name: str) -> bool:
        return wanted is None or name in wanted

    out: dict[str, Any] = {}
    if keep("scene"):
        out["scene"] = _scene_settings()
    if keep("render"):
        out["render"] = _render_settings()
    if keep("data"):
        out["data"] = _object_settings()
    if keep("preferences"):
        out["preferences"] = _preferences()
    if keep("files"):
        out["files"] = {
            "blend": bpy.data.filepath,
            "dirty": bpy.data.is_dirty,
            "libraries": [{"name": lib.name, "filepath": lib.filepath}
                          for lib in bpy.data.libraries],
            "scripts_paths": list(bpy.utils.script_paths()),
            "asset_libraries": [{"name": lib.name, "path": lib.path,
                                 "import_method": lib.import_method}
                                for lib in getattr(bpy.context.preferences.filepaths,
                                                   "asset_libraries", [])],
        }
    if keep("handlers"):
        out["handlers"] = {
            "frame_change_pre": len(bpy.app.handlers.frame_change_pre),
            "frame_change_post": len(bpy.app.handlers.frame_change_post),
            "render_pre": len(bpy.app.handlers.render_pre),
            "render_post": len(bpy.app.handlers.render_post),
            "depsgraph_update": len(bpy.app.handlers.depsgraph_update_post),
            "load_post": len(bpy.app.handlers.load_post),
            "save_pre": len(bpy.app.handlers.save_pre),
        }
    return out


def _resolve_path(root: Any, dotted: str) -> tuple[Any, str]:
    current = root
    parts = dotted.split(".")
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current:
                raise CommandError(f"no key {part!r} in {'.'.join(parts[:-1])}")
            current = current[part]
        else:
            if not hasattr(current, part):
                raise CommandError(f"{type(current).__name__} has no attribute {part!r}")
            current = getattr(current, part)
    return current, parts[-1]


def settings_set(params: dict) -> dict:
    """Change a setting by dotted path, e.g. 'scene.frame_end' or 'render.resolution_x'."""
    group = str(params.get("group", "render"))
    dotted = str(params.get("path", ""))
    if not dotted:
        raise CommandError("settings_set needs a 'path' like 'resolution_x'")
    value = params.get("value")
    roots = {
        "render": bpy.context.scene.render,
        "scene": bpy.context.scene,
        "cycles": getattr(bpy.context.scene.render, "cycles", None),
        "eevee": getattr(bpy.context.scene, "eevee", None),
        "view": bpy.context.scene.view_settings,
        "preferences": bpy.context.preferences,
        "unit": bpy.context.scene.unit_settings,
        "world": bpy.context.scene.world,
    }
    root = roots.get(group)
    if root is None:
        raise CommandError(f"unknown group {group!r}; have {', '.join(sorted(roots))}")
    holder, attribute = _resolve_path(root, dotted)
    if not hasattr(holder, attribute):
        raise CommandError(f"cannot set {group}.{dotted}: no such attribute")
    before = getattr(holder, attribute)
    try:
        setattr(holder, attribute, value)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"cannot set {group}.{dotted} to {value!r}: {exc}") from exc
    return {"group": group, "path": dotted, "before": before, "after": value}


# --------------------------------------------------------------------------- #
# datablocks
# --------------------------------------------------------------------------- #
def blend_contents(params: dict) -> dict:
    """Every datablock in the file, grouped by type, with users and orphans."""
    kinds = params.get("types")
    rows: dict[str, Any] = {}
    for kind in ("meshes", "objects", "materials", "images", "actions", "armatures",
                 "collections", "node_groups", "textures", "texts", "worlds",
                 "scenes", "cameras", "lights", "curves", "particles", "brushes",
                 "libraries"):
        if kinds and kind not in kinds:
            continue
        try:
            collection = getattr(bpy.data, kind)
        except AttributeError:
            continue
        items = []
        for block in collection:
            items.append({
                "name": block.name,
                "users": block.users,
                "fake_user": bool(getattr(block, "use_fake_user", False)),
                "is_dirty": bool(getattr(block, "is_dirty", False)),
                "library": block.library.filepath if getattr(block, "library", None) else None,
            })
            if kind == "meshes":
                items[-1]["verts"] = len(block.vertices)
                items[-1]["faces"] = len(block.polygons)
        orphans = [i["name"] for i in items if i["users"] == 0 and not i["fake_user"]]
        rows[kind] = {"count": len(items), "orphans": len(orphans),
                      "orphan_names": orphans[:30], "items": items[:200]}
    return {"filepath": bpy.data.filepath, "collections": rows}


def scripts_and_texts(params: dict) -> dict:
    """Embedded Text datablocks and any .py files in the scripts paths."""
    texts = [{"name": t.name, "lines": len(t.as_string().splitlines()),
              "size": len(t.as_string()), "use_module": t.use_module}
             for t in bpy.data.texts]
    found = []
    for root in {p for p in bpy.utils.script_paths() if p and os.path.isdir(p)}:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith("__pycache__")][:40]
            for name in sorted(files):
                if name.endswith(".py"):
                    path = os.path.join(current, name)
                    try:
                        found.append({"path": path,
                                      "size": os.path.getsize(path),
                                      "lines": sum(1 for _ in open(path,
                                                                  encoding="utf-8",
                                                                  errors="replace"))})
                    except OSError:
                        continue
            if len(found) > 800:
                break
    return {"texts": texts, "script_files": found[:800]}


# --------------------------------------------------------------------------- #
# filesystem
# --------------------------------------------------------------------------- #
def filesystem(params: dict) -> dict:
    """List or search files, confined to safe roots by default."""
    roots = list(params.get("roots") or DEFAULT_ROOTS)
    if params.get("allow_anywhere"):
        roots = [os.path.abspath(os.sep)]
    path = str(params.get("path", "")).strip()
    if not path:
        roots = [r for r in roots if r and os.path.isdir(r)]
        return {"roots": roots, "hint": "pass 'path' to list a directory"}
    if not _in_allowed(path, roots):
        raise CommandError(
            f"{path!r} is outside the allowed roots. Allowed: "
            + ", ".join(r for r in roots if r)
            + ". Pass 'allow_anywhere': true if you really mean it."
        )
    if not os.path.exists(path):
        raise CommandError(f"no such path: {path}")
    if os.path.isfile(path):
        return {"kind": "file", "path": path, "size": os.path.getsize(path)}
    pattern = str(params.get("pattern", "*"))
    import fnmatch
    limit = int(params.get("limit", 300))
    entries = []
    for name in sorted(os.listdir(path)):
        if not fnmatch.fnmatch(name, pattern):
            continue
        full = os.path.join(path, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        entries.append({
            "name": name, "path": full, "dir": os.path.isdir(full),
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
            "extension": os.path.splitext(name)[1].lower(),
        })
        if len(entries) >= limit:
            break
    return {"kind": "directory", "path": path, "count": len(entries),
            "truncated": len(entries) >= limit, "entries": entries}


def python_env(params: dict) -> dict:
    """The interpreter Blender is running, and what it can import."""
    modules = params.get("modules") or []
    imported = {}
    for name in modules:
        try:
            module = __import__(name)
            imported[name] = {"version": getattr(module, "__version__", "unknown"),
                              "path": getattr(module, "__file__", None)}
        except Exception as exc:  # noqa: BLE001
            imported[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "executable": sys.executable, "version": sys.version,
        "prefix": sys.prefix, "blender": bpy.app.version_string,
        "binary": bpy.app.binary_path, "background": bpy.app.background,
        "sys_path": list(sys.path), "modules": imported,
        "cwd": os.getcwd(), "tempdir": bpy.app.tempdir,
        "script_paths": list(bpy.utils.script_paths()),
        "addons_dir": bpy.utils.user_resource("SCRIPTS", path="addons", create=False),
    }


def render_report(params: dict) -> dict:
    """What a render would actually use, including the resolved output path."""
    scene = bpy.context.scene
    r = scene.render
    path = bpy.path.abspath(r.filepath)
    return {
        "engine": r.engine,
        "resolution": [r.resolution_x, r.resolution_y, r.resolution_percentage],
        "frames": [scene.frame_start, scene.frame_end, r.fps, r.fps_base],
        "output": {"filepath": r.filepath, "resolved": path,
                   "format": r.image_settings.file_format,
                   "directory_exists": os.path.isdir(os.path.dirname(path) or ".")},
        "overwrite": r.use_overwrite, "use_file_extension": r.use_file_extension,
        "use_stamp": r.use_stamp, "use_border": r.use_border,
        "border": [r.border_min_x, r.border_max_x, r.border_min_y, r.border_max_y],
        "film_transparent": r.film_transparent,
        "simplify": r.use_simplify,
        "has_compositor": bool(scene.use_nodes),
        "view_transform": scene.view_settings.view_transform,
        "exposure": scene.view_settings.exposure,
        "camera": scene.camera.name if scene.camera else None,
        "lights": [{"name": o.name, "type": o.data.type,
                    "energy": round(o.data.energy, 3)}
                   for o in bpy.data.objects if o.type == "LIGHT"],
    }


def diagnose(params: dict) -> dict:
    """A quick health report on the project, aimed at 'what is wrong here'."""
    problems = []
    scene = bpy.context.scene
    if scene.camera is None:
        problems.append({"severity": "warn", "check": "camera",
                         "message": "no active camera; nothing can be rendered"})
    if not [o for o in bpy.data.objects if o.type == "LIGHT"]:
        problems.append({"severity": "info", "check": "lights",
                         "message": "no lights; renders need the world to emit"})
    unpacked = [i.name for i in bpy.data.images
                if i.source == "FILE" and i.packed_file is None]
    if unpacked:
        problems.append({"severity": "warn", "check": "textures",
                         "message": f"{len(unpacked)} image(s) are not packed; the "
                                    "blend is not self-contained",
                         "images": unpacked[:20]})
    orphans = sum(1 for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.images)
                  for block in coll if block.users == 0)
    if orphans:
        problems.append({"severity": "info", "check": "orphans",
                         "message": f"{orphans} unused datablock(s)"})
    missing_libs = [lib.filepath for lib in bpy.data.libraries
                    if lib.filepath and not os.path.isfile(lib.filepath)]
    if missing_libs:
        problems.append({"severity": "error", "check": "libraries",
                         "message": "linked libraries are missing on disk",
                         "paths": missing_libs})
    objects_without_material = [o.name for o in bpy.data.objects
                                if o.type == "MESH" and not o.data.materials]
    if objects_without_material:
        problems.append({"severity": "info", "check": "materials",
                         "message": f"{len(objects_without_material)} mesh(es) have "
                                    "no material",
                         "objects": objects_without_material[:20]})
    if bpy.app.background:
        problems.append({"severity": "warn", "check": "background",
                         "message": "Blender is running headless; the MCP bridge "
                                    "needs a real window and cannot respond"})
    return {"ok": not any(p["severity"] == "error" for p in problems),
            "filepath": bpy.data.filepath, "problems": problems}
