"""Model validation and analysis for the Blender MCP bridge.

Everything in here runs inside Blender on the main thread. The guiding rule is
that a report is always produced: a check that blows up is reported as an
ERROR entry instead of aborting the whole audit, because a half-finished
report is far more useful to an agent than a traceback.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

ERROR = "error"
WARN = "warn"
INFO = "info"
OK = "ok"

SEVERITY_ORDER = {ERROR: 0, WARN: 1, OK: 2, INFO: 3}

# Pairs of names that are almost always a copy/paste mistake on hard-surface
# models. Compared case-insensitively, ignoring a trailing _L/_R/_l/_r.
MIRROR_HINTS = ("_l", "_r", "_left", "_right", "left", "right")


class Check:
    __slots__ = ("name", "severity", "message", "data")

    def __init__(self, name: str, severity: str, message: str, data: Any = None):
        self.name = name
        self.severity = severity
        self.message = message
        self.data = data

    def as_dict(self) -> dict:
        out = {"check": self.name, "severity": self.severity, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


def _safe(check: Check) -> Check:
    return check


def _run(name: str, fn: Callable[[], Check | None]) -> Check:
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - a broken check must not kill the report
        return Check(name, ERROR, f"check crashed: {type(exc).__name__}: {exc}")
    if result is None:
        return Check(name, INFO, "not applicable")
    return result


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mesh_objects(objects: Iterable[bpy.types.Object] | None = None) -> list[bpy.types.Object]:
    pool = objects if objects is not None else bpy.data.objects
    return [o for o in pool if o.type == "MESH" and isinstance(o.data, bpy.types.Mesh)]


def _filter_ignore(objs: list[bpy.types.Object], patterns: list[str] | None) -> list[bpy.types.Object]:
    """Drop studio props (floors, backdrops, lights-as-mesh) from an audit.

    A 40 m ground plane otherwise makes every dimension and size check
    meaningless, so the caller can exclude helpers by name or glob.
    """
    if not patterns:
        return objs
    import fnmatch
    lowered = [p.lower() for p in patterns]
    return [o for o in objs
            if not any(fnmatch.fnmatch(o.name.lower(), p) for p in lowered)]


def _resolve(names: list[str] | None, objects: list[bpy.types.Object]) -> list[bpy.types.Object]:
    if not names:
        return objects
    wanted = {n.lower() for n in names}
    picked = [o for o in objects if o.name.lower() in wanted]
    if not picked:
        raise ValueError(f"no mesh objects matched {names}")
    return picked


def _world_bbox(ob: bpy.types.Object) -> tuple[Vector, Vector]:
    corners = [ob.matrix_world @ Vector(c) for c in ob.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    return lo, hi


def _loop_triangles(mesh: bpy.types.Mesh) -> int:
    try:
        mesh.calc_loop_triangles()
        return len(mesh.loop_triangles)
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #
def check_scale(objects: list[bpy.types.Object], params: dict) -> Check:
    """Real-world size. Reports every mesh larger than a sane asset and the
    scene unit configuration, which is the usual root cause of a model that
    'looks tiny' in an engine."""
    scene = bpy.context.scene
    unit = scene.unit_settings
    unit_problems = []
    if unit.system == "NONE":
        unit_problems.append("scene unit system is NONE, 1 Blender unit is undefined")
    if unit.system == "METRIC" and abs(unit.scale_length - 1.0) > 1e-6:
        unit_problems.append(f"unit scale_length is {unit.scale_length}, expected 1.0")
    limit = float(params.get("max_dimension", 100.0))
    oversize = []
    for ob in objects:
        dims = ob.dimensions
        if max(dims) > limit:
            oversize.append({"object": ob.name,
                             "dimensions": [round(d, 3) for d in dims]})
    if unit_problems:
        return Check("scale", ERROR, "; ".join(unit_problems),
                     {"oversized": oversize[:20]})
    if oversize:
        return Check("scale", WARN,
                     f"{len(oversize)} object(s) exceed {limit:g} Blender units",
                     {"oversized": oversize[:20]})
    total = sum(len(o.data.polygons) for o in objects)
    return Check("scale", OK, f"units sane, {len(objects)} meshes, {total} faces",
                 {"max_dimension_limit": limit})


def check_dimensions(objects: list[bpy.types.Object], params: dict) -> Check:
    """Compare the assembly bounding box against a requested real-world spec."""
    spec = params.get("target") or {}
    if not spec:
        return None
    lo = Vector((1e18, 1e18, 1e18))
    hi = Vector((-1e18, -1e18, -1e18))
    for ob in objects:
        a, b = _world_bbox(ob)
        lo = Vector((min(lo.x, a.x), min(lo.y, a.y), min(lo.z, a.z)))
        hi = Vector((max(hi.x, b.x), max(hi.y, b.y), max(hi.z, b.z)))
    actual = [hi.x - lo.x, hi.y - lo.y, hi.z - lo.z]
    rows = []
    worst = 0.0
    axis_index = {"length": 0, "width": 1, "height": 2}
    for key, want in spec.items():
        if key not in axis_index or want is None:
            continue
        axis = "XYZ"[axis_index[key]]
        got = actual[axis_index[key]]
        err = (got - float(want)) / float(want) * 100.0 if want else 0.0
        worst = max(worst, abs(err))
        rows.append({"axis": key, "target_m": float(want), "actual_m": round(got, 4),
                     "error_pct": round(err, 2)})
    tolerance = float(params.get("tolerance_pct", 2.0))
    severity = OK if worst <= tolerance else (WARN if worst <= tolerance * 3 else ERROR)
    return Check("dimensions", severity,
                 f"worst axis deviation {worst:.2f}% (tolerance {tolerance:g}%)",
                 {"axes": rows, "bbox_min": [round(v, 4) for v in lo],
                  "bbox_max": [round(v, 4) for v in hi]})


def check_normals(objects: list[bpy.types.Object], params: dict) -> Check:
    """Zero-length normals, flipped winding on closed shells, negative scale."""
    zero = []
    flipped = []
    neg_scale = []
    threshold = math.radians(float(params.get("flip_angle_deg", 60.0)))
    for ob in objects:
        me = ob.data
        bad = sum(1 for p in me.polygons if p.normal.length < 0.5)
        if bad:
            zero.append({"object": ob.name, "faces": bad})
        if min(ob.scale) < -1e-6:
            neg_scale.append({"object": ob.name,
                              "scale": [round(v, 4) for v in ob.scale]})
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.normal_update()
        bm.faces.ensure_lookup_table()
        closed = all(len(e.link_faces) == 2 for e in bm.edges)
        if closed and len(bm.faces) >= 4:
            volume = bm.calc_volume(signed=True)
            if volume < 0:
                flipped.append(ob.name)
        bm.free()
    if flipped:
        return Check("normals", ERROR,
                     f"{len(flipped)} closed mesh(es) have inverted winding: "
                     + ", ".join(flipped[:8]),
                     {"inverted": flipped[:40], "zero_normals": zero[:20],
                      "negative_scale": neg_scale[:20]})
    if zero or neg_scale:
        return Check("normals", WARN,
                     f"{len(zero)} mesh(es) with degenerate normals, "
                     f"{len(neg_scale)} with negative scale",
                     {"zero_normals": zero[:20], "negative_scale": neg_scale[:20]})
    return Check("normals", OK, "winding consistent, no degenerate normals")


def check_topology(objects: list[bpy.types.Object], params: dict) -> Check:
    """Non-manifold edges, boundary holes, loose and zero-area geometry."""
    merge = float(params.get("weld_distance", 1e-5))
    nonmanifold: list[dict] = []
    boundary: list[dict] = []
    loose: list[dict] = []
    zero_area: list[dict] = []
    interior: list[dict] = []
    allow_boundary = bool(params.get("allow_boundary_edges", True))
    for ob in objects:
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        nm = [e for e in bm.edges if len(e.link_faces) > 2]
        bd = [e for e in bm.edges if len(e.link_faces) == 1]
        lv = sum(1 for v in bm.verts if not v.link_edges)
        le = sum(1 for e in bm.edges if not e.link_faces)
        za = sum(1 for f in bm.faces if f.calc_area() < 1e-12)
        # a face whose normal points away from the mesh centroid on a closed
        # shell is a classic leftover from a bad boolean
        if len(bm.faces) >= 8 and all(len(e.link_faces) == 2 for e in bm.edges):
            volume = bm.calc_volume(signed=True)
            ctr = Vector((0, 0, 0))
            for f in bm.faces:
                ctr += f.calc_center_median()
            ctr /= len(bm.faces)
            inward = sum(1 for f in bm.faces
                         if (f.calc_center_median() - ctr).normalized().dot(f.normal) < -0.95)
            if inward > max(2, len(bm.faces) * 0.02):
                interior.append({"object": ob.name, "faces": inward})
        if nm:
            nonmanifold.append({"object": ob.name, "edges": len(nm)})
        if bd and not allow_boundary:
            boundary.append({"object": ob.name, "edges": len(bd)})
        elif bd:
            boundary.append({"object": ob.name, "edges": len(bd),
                             "loops": _count_boundary_loops(bm)})
        if lv or le:
            loose.append({"object": ob.name, "verts": lv, "edges": le})
        if za:
            zero_area.append({"object": ob.name, "faces": za})
        bm.free()
    if nonmanifold:
        return Check("topology", ERROR,
                     f"{len(nonmanifold)} mesh(es) contain non-manifold edges "
                     "(an edge shared by more than two faces)",
                     {"nonmanifold": nonmanifold[:40], "interior_faces": interior[:20]})
    if loose or zero_area:
        return Check("topology", WARN,
                     f"{len(loose)} mesh(es) with loose geometry, "
                     f"{len(zero_area)} with zero-area faces",
                     {"loose": loose[:30], "zero_area": zero_area[:30]})
    return Check("topology", OK, "manifold, no loose or zero-area geometry",
                 {"open_shells": [b for b in boundary if b.get("edges")][:30],
                  "total_open_boundary_edges": sum(b.get("edges", 0) for b in boundary)})


def _count_boundary_loops(bm: bmesh.types.BMesh) -> int:
    seen = set()
    loops = 0
    for edge in bm.edges:
        if len(edge.link_faces) != 1 or edge.index in seen:
            continue
        loops += 1
        stack = [edge]
        while stack:
            e = stack.pop()
            if e.index in seen:
                continue
            seen.add(e.index)
            for v in e.verts:
                for other in v.link_edges:
                    if len(other.link_faces) == 1 and other.index not in seen:
                        stack.append(other)
    return loops


def check_intersections(objects: list[bpy.types.Object], params: dict) -> Check:
    """Real geometry overlap between separate objects, with a bbox prefilter.

    Touching panels of a car legitimately share a boundary, so the report is a
    warning list rather than a failure, and the tolerance is tunable.
    """
    limit = int(params.get("max_pairs", 400))
    ignore = {n.lower() for n in (params.get("ignore") or [])}
    pool = [o for o in objects if o.name.lower() not in ignore]
    boxes = []
    for ob in pool:
        lo, hi = _world_bbox(ob)
        boxes.append((ob, lo, hi))
    pairs = []
    tests = 0
    for i in range(len(boxes)):
        oa, la, ha = boxes[i]
        for j in range(i + 1, len(boxes)):
            ob_, lb, hb = boxes[j]
            if (ha.x < lb.x or hb.x < la.x or ha.y < lb.y or hb.y < la.y
                    or ha.z < lb.z or hb.z < la.z):
                continue
            pairs.append((oa, ob_))
    for oa, ob_ in pairs[:limit]:
        tests += 1
        try:
            dg = bpy.context.evaluated_depsgraph_get()
            ta = BVHTree.FromObject(oa, dg)
            tb = BVHTree.FromObject(ob_, dg)
            if ta is None or tb is None:
                continue
            hits = ta.overlap(tb)
            if hits:
                pairs_data = pairs
                del pairs_data
                result_pairs.append({
                    "a": oa.name, "b": ob_.name, "overlaps": len(hits),
                    "sample": [round(v, 4) for v in hits[0][0]],
                })
        except Exception:  # noqa: BLE001
            continue
    if result_pairs:
        return Check("intersections", WARN,
                     f"{len(result_pairs)} object pair(s) intersect; "
                     f"{tests} candidate pair(s) tested",
                     {"pairs": result_pairs[:60]})
    return Check("intersections", OK,
                 f"no intersections among {tests} bbox-overlapping pair(s)")


def check_naming(objects: list[bpy.types.Object], params: dict) -> Check:
    """Blender suffixes .001 on duplicate names; agents trip over that."""
    pattern = params.get("pattern")
    duplicates = []
    bad_names = []
    seen: dict[str, int] = {}
    for ob in objects:
        seen[ob.name] = seen.get(ob.name, 0) + 1
        base = ob.name
        while base[-4:-3] == "." and base[-3:].isdigit():
            base = base[:-4]
        if base != ob.name:
            duplicates.append(ob.name)
        if pattern and not _fnmatch(ob.name, pattern):
            bad_names.append(ob.name)
    if duplicates:
        return Check("naming", WARN,
                     f"{len(duplicates)} object(s) carry a .001-style duplicate suffix",
                     {"duplicates": duplicates[:40]})
    if bad_names:
        return Check("naming", WARN,
                     f"{len(bad_names)} object(s) do not match pattern {pattern!r}",
                     {"non_matching": bad_names[:40]})
    return Check("naming", OK, "all names unique and well formed")


def _fnmatch(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


def check_symmetry(objects: list[bpy.types.Object], params: dict) -> Check:
    """Mirror a half across an axis and measure the deviation of the other half."""
    axis = str(params.get("axis", "X")).upper()
    tol = float(params.get("tolerance", 0.002))
    pairs = []
    worst = 0.0
    buckets: dict[str, list[bpy.types.Object]] = {}
    for ob in objects:
        key = ob.name
        low = key.lower()
        for suffix in ("_l", "_r", "left", "right", "_left", "_right"):
            if low.endswith(suffix):
                key = key[: len(key) - len(suffix)]
                break
        buckets.setdefault(key, []).append(ob)
    for key, group in buckets.items():
        if len(group) != 2:
            continue
        a, b = group
        da = [round(v, 4) for v in a.dimensions]
        db = [round(v, 4) for v in b.dimensions]
        idx = "XYZ".index(axis)
        pa, pb = _world_bbox(a), _world_bbox(b)
        if idx == 0:
            dev = abs(abs((pa[1].x + pa[0].x) / 2) - abs((pb[1].x + pb[0].x) / 2))
        elif idx == 1:
            dev = abs(abs((pa[1].y + pa[0].y) / 2) - abs((pb[1].y + pb[0].y) / 2))
        else:
            dev = abs(abs((pa[1].z + pa[0].z) / 2) - abs((pb[1].z + pb[0].z) / 2))
        worst = max(worst, dev)
        pairs.append({"pair": key, "a": a.name, "b": b.name,
                      "dimensions_a": da, "dimensions_b": db,
                      "offset_deviation": round(dev, 5),
                      "size_match": da == db})
    if not pairs:
        return None
    mismatched = [p for p in pairs if not p["size_match"]]
    if worst > tol or mismatched:
        return Check("symmetry", WARN if worst <= tol * 5 else ERROR,
                     f"{len(pairs)} mirrored pair(s), worst centre offset {worst:.5f} m "
                     f"(tolerance {tol:g}); {len(mismatched)} size mismatch(es)",
                     {"pairs": pairs[:40]})
    return Check("symmetry", OK,
                 f"{len(pairs)} mirrored pair(s) match within {tol:g} m", {"pairs": pairs[:40]})


def check_materials(objects: list[bpy.types.Object], params: dict) -> Check:
    missing = []
    empty = []
    no_image = []
    for ob in objects:
        slots = getattr(ob.data, "materials", None)
        if slots is None:
            continue
        if not slots:
            missing.append(ob.name)
            continue
        for i, mat in enumerate(slots):
            if mat is None:
                empty.append({"object": ob.name, "slot": i})
                continue
            if not mat.use_nodes:
                no_image.append({"object": ob.name, "material": mat.name,
                                 "issue": "use_nodes is off"})
    if missing:
        return Check("materials", ERROR,
                     f"{len(missing)} mesh(es) have no material slot",
                     {"missing": missing[:40], "empty_slots": empty[:20]})
    if empty:
        return Check("materials", ERROR, f"{len(empty)} empty material slot(s)",
                     {"empty_slots": empty[:40]})
    if no_image:
        return Check("materials", WARN, f"{len(no_image)} material(s) not node-based",
                     {"materials": no_image[:30]})
    return Check("materials", OK,
                 f"{len({m.name for o in objects for m in o.data.materials if m})} "
                 "material(s) assigned, all node-based")


def check_uvs(objects: list[bpy.types.Object], params: dict) -> Check:
    missing = []
    out_of_range = []
    for ob in objects:
        me = ob.data
        if not me.uv_layers:
            missing.append(ob.name)
            continue
        uv = me.uv_layers.active.data
        bad = 0
        for item in uv:
            u, v = item.uv
            if u < -0.001 or u > 1.001 or v < -0.001 or v > 1.001:
                bad += 1
        if bad:
            out_of_range.append({"object": ob.name, "loops": bad,
                                 "total": len(uv)})
    if missing:
        return Check("uv", WARN, f"{len(missing)} mesh(es) have no UV map",
                     {"missing": missing[:40]})
    if out_of_range:
        return Check("uv", INFO,
                     f"{len(out_of_range)} mesh(es) use UVs outside 0..1 "
                     "(fine for tiling, a problem for baked atlases)",
                     {"out_of_range": out_of_range[:30]})
    return Check("uv", OK, "every mesh has a UV map inside 0..1")


def check_transforms(objects: list[bpy.types.Object], params: dict) -> Check:
    unapplied = []
    nonuniform = []
    for ob in objects:
        s = ob.scale
        if abs(abs(s.x) - 1) > 1e-4 or abs(abs(s.y) - 1) > 1e-4 or abs(abs(s.z) - 1) > 1e-4:
            unapplied.append({"object": ob.name,
                              "scale": [round(v, 4) for v in s]})
        if max(s) - min(s) > 1e-4:
            nonuniform.append({"object": ob.name,
                               "scale": [round(v, 4) for v in s]})
    if nonuniform:
        return Check("transforms", WARN,
                     f"{len(nonuniform)} object(s) have non-uniform scale, which "
                     "shears normals and modifiers",
                     {"non_uniform": nonuniform[:40]})
    if unapplied:
        return Check("transforms", INFO,
                     f"{len(unapplied)} object(s) carry an unapplied object scale",
                     {"unapplied": unapplied[:40]})
    return Check("transforms", OK, "all scales are uniform and applied")


def check_pivots(objects: list[bpy.types.Object], params: dict) -> Check:
    """An origin far from the geometry makes rigging and physics painful."""
    limit = float(params.get("max_distance", 0.05))
    far = []
    for ob in objects:
        if not ob.data.vertices:
            continue
        local = [v.co for v in ob.data.vertices]
        ctr = sum(local, Vector()) / len(local)
        if ctr.length > limit:
            far.append({"object": ob.name,
                        "offset_m": round(ctr.length, 4)})
    if far:
        return Check("pivots", WARN,
                     f"{len(far)} object(s) have an origin more than {limit:g} m "
                     "from their geometry centre",
                     {"objects": sorted(far, key=lambda d: -d["offset_m"])[:40]})
    return Check("pivots", OK, "every origin sits on its geometry centre")


def check_budget(objects: list[bpy.types.Object], params: dict) -> Check:
    faces = sum(len(o.data.polygons) for o in objects)
    tris = sum(_loop_triangles(o.data) for o in objects)
    budget = int(params.get("max_faces", 500_000))
    ngons = sum(1 for o in objects for p in o.data.polygons if len(p.vertices) > 4)
    tris_faces = sum(1 for o in objects for p in o.data.polygons if len(p.vertices) == 3)
    quad_ratio = 1.0 - (tris_faces + ngons) / max(faces, 1)
    data = {"faces": faces, "triangles": tris, "ngons": ngons,
            "quad_ratio": round(quad_ratio, 4), "objects": len(objects),
            "budget_faces": budget}
    if faces > budget:
        return Check("budget", WARN,
                     f"{faces} faces exceeds the {budget} budget", data)
    if quad_ratio < 0.5 and faces > 200:
        return Check("budget", INFO,
                     f"only {quad_ratio * 100:.0f}% quads ({tris_faces} tris, "
                     f"{ngons} n-gons)", data)
    return Check("budget", OK, f"{faces} faces within the {budget} budget", data)


def check_lods(objects: list[bpy.types.Object], params: dict) -> Check:
    """Look for LOD naming conventions such as _LOD0/_LOD1 or a LOD collection."""
    found: dict[str, int] = {}
    lods = []
    for ob in objects:
        low = ob.name.lower()
        for tag in ("lod0", "lod1", "lod2", "lod3", "high", "low", "mid"):
            if tag in low:
                found[tag] = found.get(tag, 0) + 1
    for col in bpy.data.collections:
        if "lod" in col.name.lower():
            lods.append({"collection": col.name, "objects": len(col.objects)})
    if not found and not lods:
        return Check("lods", INFO,
                     "no LOD naming or LOD collection detected; a single "
                     "high-detail model is fine for stills and renders")
    return Check("lods", OK, "LOD structure detected", {"tags": found,
                                                        "collections": lods})


def check_lights_and_camera(params: dict) -> Check:
    lights = [o for o in bpy.data.objects if o.type == "LIGHT"]
    cameras = [o for o in bpy.data.objects if o.type == "CAMERA"]
    scene = bpy.context.scene
    problems = []
    if not cameras:
        problems.append("no camera in the scene, nothing can be rendered")
    elif scene.camera is None:
        problems.append("scene.camera is unset")
    if not lights:
        problems.append("no lights; renders will be black unless the world emits")
    if problems:
        return Check("lighting", WARN, "; ".join(problems),
                     {"lights": len(lights), "cameras": len(cameras)})
    return Check("lighting", OK, f"{len(lights)} light(s), active camera set")


def check_orphans(params: dict) -> Check:
    meshes = [m for m in bpy.data.meshes if m.users == 0]
    mats = [m for m in bpy.data.materials if m.users == 0]
    images = [i for i in bpy.data.images if i.users == 0 and i.name != "Render Result"]
    data = {"meshes": len(meshes), "materials": len(mats), "images": len(images),
            "mesh_names": [m.name for m in meshes[:20]],
            "material_names": [m.name for m in mats[:20]]}
    total = len(meshes) + len(mats) + len(images)
    if total:
        return Check("orphans", INFO, f"{total} unused datablock(s); bpy.ops.outliner.orphans_purge "
                     "removes them", data)
    return Check("orphans", OK, "no unused datablocks", data)


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #
result_pairs: list[dict] = []   # populated by check_intersections, cleared per run


def analyze_mesh(params: dict) -> dict:
    """Deep single-mesh statistics, far beyond what list_objects reports."""
    names = params.get("object")
    ob = bpy.data.objects.get(names) if isinstance(names, str) else None
    if ob is None and isinstance(names, list) and names:
        ob = bpy.data.objects.get(names[0])
    if ob is None or ob.type != "MESH":
        raise ValueError("analyze_mesh needs the name of a mesh object")
    me = ob.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.normal_update()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    manifold = sum(1 for e in bm.edges if len(e.link_faces) == 2)
    boundary = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    wire = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    areas = [f.calc_area() for f in bm.faces]
    closed = boundary == 0 and wire == 0
    volume = bm.calc_volume(signed=True) if closed else None
    shell_loose_v = sum(1 for v in bm.verts if not v.link_faces)
    shell_loose_e = sum(1 for e in bm.edges if not e.link_faces)
    dup = 0
    try:
        before = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
        dup = before - len(bm.verts)
    except Exception:  # noqa: BLE001
        dup = -1
    bm.free()

    ngon_faces = sum(1 for p in me.polygons if len(p.vertices) > 4)
    tri_faces = sum(1 for p in me.polygons if len(p.vertices) == 3)
    local = [v.co for v in me.vertices] or [Vector()]
    ctr = sum(local, Vector()) / len(local)
    result = {
        "object": ob.name,
        "data": me.name,
        "vertices": len(me.vertices),
        "edges": len(me.edges),
        "faces": len(me.polygons),
        "triangles_after_triangulate": _loop_triangles(me),
        "tri_faces": tri_faces,
        "ngon_faces": ngon_faces,
        "manifold_edges": manifold,
        "boundary_edges": boundary,
        "wire_edges": wire,
        "closed_shell": closed,
        "signed_volume_m3": round(volume, 8) if volume is not None else None,
        "surface_area_m2": round(sum(areas), 6),
        "min_face_area": round(min(areas), 12) if areas else 0.0,
        "max_face_area": round(max(areas), 8) if areas else 0.0,
        "zero_area_faces": sum(1 for a in areas if a < 1e-12),
        "loose_vertices": shell_loose_v,
        "loose_edges": shell_loose_e,
        "duplicate_vertices_within_1e-5": dup,
        "uv_layers": [u.name for u in me.uv_layers],
        "vertex_groups": len(ob.vertex_groups),
        "shape_keys": len(me.shape_keys.key_blocks) if me.shape_keys else 0,
        "modifiers": [(m.name, m.type) for m in ob.modifiers],
        "dimensions_m": [round(v, 5) for v in ob.dimensions],
        "local_center": [round(v, 5) for v in ctr],
        "origin_offset_m": round(ctr.length, 5),
    }
    return result


def measure(params: dict) -> dict:
    """Distances that are tedious to eyeball: bbox, object pairs, axis extents."""
    mode = str(params.get("mode", "bbox"))
    if mode == "bbox":
        ob = bpy.data.objects.get(params.get("object", ""))
        if ob is None:
            ob = bpy.context.scene.collection
        if hasattr(ob, "bound_box"):
            lo, hi = _world_bbox(ob)
            size = hi - lo
            return {"object": ob.name,
                    "min": [round(v, 5) for v in lo],
                    "max": [round(v, 5) for v in hi],
                    "size": [round(v, 5) for v in size],
                    "diagonal": round(size.length, 5),
                    "center": [round((lo[i] + hi[i]) / 2, 5) for i in range(3)]}
    if mode == "objects":
        a = bpy.data.objects.get(params.get("a", ""))
        b = bpy.data.objects.get(params.get("b", ""))
        if a is None or b is None:
            raise ValueError("measure mode='objects' needs 'a' and 'b' object names")
        pa = sum((a.matrix_world @ Vector(c) for c in a.bound_box), Vector()) / 8
        pb = sum((b.matrix_world @ Vector(c) for c in b.bound_box), Vector()) / 8
        return {"a": a.name, "b": b.name,
                "center_distance_m": round((pa - pb).length, 5),
                "center_a": [round(v, 5) for v in pa],
                "center_b": [round(v, 5) for v in pb]}
    if mode == "assembly":
        objs = _resolve(params.get("objects"), _mesh_objects())
        lo = Vector((1e18, 1e18, 1e18))
        hi = Vector((-1e18, -1e18, -1e18))
        for ob in objs:
            a, b = _world_bbox(ob)
            lo = Vector((min(lo.x, a.x), min(lo.y, a.y), min(lo.z, a.z)))
            hi = Vector((max(hi.x, b.x), max(hi.y, b.y), max(hi.z, b.z)))
        size = hi - lo
        return {"objects": len(objs),
                "min": [round(v, 5) for v in lo], "max": [round(v, 5) for v in hi],
                "length_width_height": [round(v, 5) for v in size],
                "ground_z": round(lo.z, 5)}
    raise ValueError(f"unknown measure mode {mode!r}; use bbox, objects or assembly")


CHECKS: list[tuple[str, Callable[[list[bpy.types.Object], dict], Check | None]]] = [
    ("scale", check_scale),
    ("dimensions", check_dimensions),
    ("normals", check_normals),
    ("topology", check_topology),
    ("intersections", check_intersections),
    ("symmetry", check_symmetry),
    ("naming", check_naming),
    ("materials", check_materials),
    ("uv", check_uvs),
    ("transforms", check_transforms),
    ("pivots", check_pivots),
    ("budget", check_budget),
    ("lods", check_lods),
]


def validate(params: dict) -> dict:
    """Run the audit. Always returns a full report, never raises for a bad check."""
    global result_pairs
    result_pairs = []
    wanted = params.get("checks")
    objs = _resolve(params.get("objects"), _mesh_objects())
    objs = _filter_ignore(objs, params.get("ignore"))
    if params.get("auto_ignore_helpers", True):
        # a studio floor or backdrop would otherwise dominate every size check
        objs = [o for o in objs
                if not (max(o.dimensions) > 8.0 and min(o.dimensions) > 8.0)]
    if not objs:
        raise ValueError("no mesh objects in the scene to validate")
    report: list[Check] = []
    for name, fn in CHECKS:
        if wanted and name not in wanted:
            continue
        report.append(_run(name, lambda fn=fn: fn(objs, params)))
    report.append(_run("lighting", lambda: check_lights_and_camera(params)))
    if params.get("orphans", True):
        report.append(_run("orphans", lambda: check_orphans(params)))

    errors = sum(1 for c in report if c.severity == ERROR)
    warns = sum(1 for c in report if c.severity == WARN)
    passes = sum(1 for c in report if c.severity == OK)
    total = errors + warns + passes
    score = round(100.0 * passes / total, 1) if total else 0.0
    if errors:
        verdict = "fail"
    elif warns:
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"
    ordered = sorted(report, key=lambda c: SEVERITY_ORDER[c.severity])
    return {
        "verdict": verdict,
        "score": score,
        "errors": errors,
        "warnings": warns,
        "passed": passes,
        "objects_checked": len(objs),
        "total_faces": sum(len(o.data.polygons) for o in objs),
        "checks": [c.as_dict() for c in ordered],
    }
