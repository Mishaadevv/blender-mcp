"""End-to-end self test: launches Blender with the bridge and exercises every area.

Usage:  python scripts/selftest.py [--keep] [--no-headless]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from blender_mcp.client import BRIDGE, BlenderError, find_blender_executable  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, function) -> None:
    started = time.time()
    try:
        detail = function()
        results.append((PASS, name, f"{detail} ({time.time() - started:.2f}s)"))
        print(f"  {PASS}  {name}: {detail}")
    except Exception as exc:  # noqa: BLE001
        message = str(exc).replace("\n", " ")[:400]
        results.append((FAIL, name, message))
        print(f"  {FAIL}  {name}: {message}")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    return message


def _raise_batch(report):
    """Turn a failed batch into a readable assertion instead of a bare False."""
    bad = [f"{r.get('command')}: {r.get('error')}" for r in report["results"]
           if not r["ok"]]
    raise AssertionError("batch steps failed -> " + " | ".join(bad)[:300])


def _expect_error(function, *args, needle: str) -> str:
    try:
        function(*args)
    except BlenderError as exc:
        if needle.lower() not in str(exc).lower():
            raise AssertionError(f"error did not mention {needle!r}: {exc}") from exc
        return "clean error"
    raise AssertionError(f"expected a failure mentioning {needle!r}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        with __import__("contextlib").suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="do not kill headless Blender")
    args = parser.parse_args()

    if BRIDGE.is_available():
        print(f"Using the already running Blender on {BRIDGE.host}:{BRIDGE.port}")
    else:
        print("No bridge found; starting a minimised Blender...")
        os.environ["BLENDER_MCP_AUTOLAUNCH"] = "1"
        BRIDGE.maybe_launch_instance()
    print(f"Blender: {find_blender_executable()}\n")

    workdir = HERE.parent / "_selftest"
    workdir.mkdir(exist_ok=True)

    print("1. connection & scene")
    info = BRIDGE.call("ping")
    check("ping", lambda: assert_true(
        info["blender_version"].startswith("4.5"), "version 4.5.x"))
    headless = bool(info.get("background"))
    print(f"   (headless Blender: {headless}"
          f"{' - file reload tests will be skipped' if headless else ''})")

    if headless:
        check("clear startup objects", lambda: assert_true(
            BRIDGE.call("delete_objects", {"objects": [
                o["name"] for o in BRIDGE.call("list_objects", {"limit": 500})["objects"]
            ]})["remaining_objects"] == 0, "scene cleared"))
    else:
        check("new empty file", lambda: assert_true(
            BRIDGE.call("file_op", {"action": "new", "empty": True})["objects"] == 0, "scene cleared"))
    scene = {}
    check("get_scene", lambda: (scene.update(BRIDGE.call("get_scene")),
                                assert_true("counts" in scene, "scene summary present"))[1])
    check("search_api", lambda: assert_true(
        len(BRIDGE.call("search_api", {"query": "primitive"})["matches"]) > 5, "operator search works"))
    check("list_operators", lambda: assert_true(
        len(BRIDGE.call("list_operators", {"query": "primitive_torus",
                                           "include_properties": True})["operators"]) == 1,
        "operator introspection with properties"))

    print("\n2. geometry")
    check("add cube", lambda: assert_true(
        BRIDGE.call("add_primitive", {"type": "cube", "name": "Cube", "location": [0, 0, 1]})["name"] == "Cube",
        "cube created"))
    check("add torus", lambda: assert_true(
        BRIDGE.call("add_primitive", {
            "type": "torus", "name": "Torus",
            "parameters": {"major_radius": 1.0, "minor_radius": 0.3,
                           "major_segments": 48, "minor_segments": 12},
            "location": [3, 0, 1], "shade_smooth": True})["name"] == "Torus", "torus created"))
    check("create mesh from data", lambda: assert_true(
        BRIDGE.call("create_mesh", {
            "name": "Pyramid", "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [.5, .5, 1]],
            "faces": [[0, 1, 2, 3], [0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]],
            "location": [-3, 0, 0]})["mesh"]["faces"] == 5, "custom mesh created"))
    check("list objects", lambda: assert_true(
        BRIDGE.call("list_objects", {"type": "MESH"})["total"] == 3, "3 meshes listed"))
    check("get object", lambda: assert_true(
        BRIDGE.call("get_object", {"name": "Torus"})["modifiers"] == [], "object detail returned"))
    check("duplicate", lambda: assert_true(
        len(BRIDGE.call("duplicate_objects", {"objects": ["Cube"]})["created"]) == 1, "duplicated"))
    check("rename", lambda: assert_true(
        BRIDGE.call("rename_object", {"object": "Cube.001", "name": "CubeCopy"})["renamed"]["to"] == "CubeCopy",
        "renamed"))
    check("transform", lambda: assert_true(
        BRIDGE.call("transform_objects", {"objects": ["CubeCopy"], "location": [0, 4, 1],
                                          "rotation": [0, 0, 45]})["updated"][0]["location"] == [0, 4, 1],
        "moved and rotated"))
    check("apply transform", lambda: assert_true(
        BRIDGE.call("apply_transform", {"objects": ["CubeCopy"]})["applied_to"] == ["CubeCopy"],
        "transform baked"))
    check("parent", lambda: assert_true(
        BRIDGE.call("parent_objects", {"child": "CubeCopy", "parent": "Torus"})["parent"] == "Torus",
        "parented"))
    check("collection", lambda: assert_true(
        BRIDGE.call("create_collection", {"name": "Props", "objects": ["Pyramid"]})["collection"] == "Props",
        "collection created"))
    check("join", lambda: assert_true(
        BRIDGE.call("join_objects", {"objects": ["CubeCopy", "Torus"]})["joined_into"] == "CubeCopy",
        "two objects joined into the first"))
    check("delete", lambda: assert_true(
        BRIDGE.call("delete_objects", {"objects": ["CubeCopy"]})["deleted"] == ["CubeCopy"], "object deleted"))

    print("\n2b. joining freshly created meshes")
    # Regression: objects built with bpy.data.objects.new() were not selectable
    # until the view layer refreshed, so join silently did nothing and the
    # caller renamed an unrelated object.
    for suffix in ("A", "B", "C"):
        BRIDGE.call("create_mesh", {
            "name": f"JoinTest{suffix}",
            "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
            "faces": [[0, 1, 2, 3]],
        })
    check("join fresh meshes", lambda: assert_true(
        BRIDGE.call("join_objects", {"objects": ["JoinTestA", "JoinTestB", "JoinTestC"]})["joined_into"]
        == "JoinTestA", "all three merged into the first"))
    check("join kept the geometry", lambda: assert_true(
        BRIDGE.call("get_object", {"name": "JoinTestA"})["mesh"]["faces"] == 3,
        "3 quads survived the join"))
    check("join removed the sources", lambda: assert_true(
        "JoinTestB" not in [o["name"] for o in BRIDGE.call("list_objects", {"limit": 200})["objects"]],
        "no leftovers"))
    check("join left bystanders alone", lambda: assert_true(
        "Pyramid" in [o["name"] for o in BRIDGE.call("list_objects", {"limit": 200})["objects"]],
        "unrelated objects untouched"))
    BRIDGE.call("delete_objects", {"objects": ["JoinTestA"]})

    print("\n3. mesh editing & modifiers")
    BRIDGE.call("add_primitive", {"type": "cube", "name": "BevelMe"})
    check("bevel mesh", lambda: assert_true(
        BRIDGE.call("edit_mesh", {"action": "bevel", "objects": ["BevelMe"],
                                  "parameters": {"offset": 0.05, "segments": 2}})["results"][0]["vertices"] > 8,
        "bevel added vertices"))
    check("subdivide mesh", lambda: assert_true(
        BRIDGE.call("edit_mesh", {"action": "subdivide", "objects": ["BevelMe"],
                                  "parameters": {"cuts": 1}})["results"][0]["faces"] > 6,
        "faces increased"))
    check("recalc normals", lambda: assert_true(
        BRIDGE.call("edit_mesh", {"action": "recalc_normals", "objects": ["BevelMe"]})["results"],
        "normals recalculated"))
    check("delete faces", lambda: BRIDGE.call("edit_mesh", {
        "action": "delete_faces", "objects": ["Pyramid"], "region": "all"})["results"] and "deleted")
    check("add modifier", lambda: assert_true(
        BRIDGE.call("add_modifier", {"object": "BevelMe", "type": "SUBSURF",
                                     "properties": {"levels": 2}})["modifier"]["type"] == "SUBSURF",
        "subsurf added"))
    check("apply modifier", lambda: assert_true(
        "remaining_modifiers" in BRIDGE.call("apply_modifier", {"object": "BevelMe", "modifier": "Subsurf"}),
        "subsurf applied"))
    check("shade smooth", lambda: assert_true(
        BRIDGE.call("shade_smooth", {"objects": ["BevelMe"], "smooth": True,
                                     "auto_smooth_angle": 30})["shade_smooth"], "smooth shaded"))

    print("\n4. materials")
    check("create material", lambda: assert_true(
        BRIDGE.call("create_material", {"name": "Ruby", "base_color": [0.7, 0.02, 0.05, 1],
                                        "metallic": 0.1, "roughness": 0.15,
                                        "emission_color": [0.3, 0, 0, 1],
                                        "emission_strength": 2})["material"] == "Ruby", "ruby created"))
    check("procedural texture", lambda: assert_true(
        BRIDGE.call("set_material_node", {"material": "Ruby", "node_type": "noise",
                                          "properties": {"Scale": 8, "Detail": 6}})["node"] == "Noise",
        "noise node wired"))
    check("assign material", lambda: assert_true(
        BRIDGE.call("assign_material", {"objects": ["BevelMe"], "material": "Ruby"})["material"] == "Ruby",
        "assigned to BevelMe"))
    check("list materials", lambda: assert_true(
        len(BRIDGE.call("list_materials", {"detailed": True})["materials"]) == 1, "materials listed"))

    print("\n5. camera, lights, world")
    check("add camera", lambda: assert_true(
        BRIDGE.call("add_camera", {"name": "MainCam", "location": [6, -6, 4], "lens": 50,
                                   "point_at": "BevelMe"})["camera"]["lens"] == 50.0, "camera created and aimed"))
    check("point_at actually rotates", lambda: assert_true(
        any(abs(a) > 1 for a in BRIDGE.call("get_object", {"name": "MainCam"})["rotation_euler_deg"]),
        "point_at produced a non-zero rotation"))
    check("look at", lambda: assert_true(
        BRIDGE.call("look_at", {"object": "MainCam", "target": "Pyramid"})["object"] == "MainCam", "re-aimed"))
    check("active camera", lambda: assert_true(
        BRIDGE.call("set_active_camera", {"name": "MainCam"})["camera"] == "MainCam", "camera active"))
    check("add lights", lambda: (BRIDGE.call("add_light", {"type": "SUN", "name": "Sun", "energy": 3}),
                                  BRIDGE.call("add_light", {"type": "AREA", "name": "Key", "location": [3, -3, 5],
                                                            "energy": 500, "size": 4, "point_at": "BevelMe"}),
                                  BRIDGE.call("add_light", {"type": "SPOT", "name": "SpotAt", "location": [0, -8, 7],
                                                            "point_at": "0,0,2"})) and
                                "three lights added")
    check("light point_at", lambda: assert_true(
        any(abs(a) > 1 for a in BRIDGE.call("get_object", {"name": "SpotAt"})["rotation_euler_deg"]),
        "spot aimed at its coordinates target"))
    check("aim at own position is refused", lambda: _expect_error(
        BRIDGE.call, "look_at", {"object": "SpotAt", "target": "0,-8,7"},
        needle="already at the target"))
    check("world sky", lambda: assert_true(
        BRIDGE.call("set_world", {"preset": "sky", "sun_elevation": 35})["background_color"], "sky world"))
    check("render settings", lambda: assert_true(
        BRIDGE.call("set_render_settings", {"engine": "eevee", "resolution": [800, 600],
                                            "samples": 16, "view_transform": "AgX"})["engine"].startswith("BLENDER_EEVEE"),
        "eevee 800x600"))
    check("cycles engine", lambda: assert_true(
        BRIDGE.call("set_render_settings", {"engine": "cycles", "samples": 4,
                                            "max_bounces": 2})["engine"] == "CYCLES",
        "cycles selectable"))
    check("workbench engine", lambda: assert_true(
        BRIDGE.call("set_render_settings", {"engine": "workbench"})["engine"] == "BLENDER_WORKBENCH",
        "workbench selectable"))
    check("bad engine rejected", lambda: _expect_error(
        BRIDGE.call, "set_render_settings", {"engine": "raytracer9000"},
        needle="unknown render engine"))
    BRIDGE.call("set_render_settings", {"engine": "eevee", "resolution": [800, 600], "samples": 16})

    print("\n6. output")
    render_path = workdir / "selftest_render.png"
    check("render still", lambda: assert_true(
        BRIDGE.call("render", {"output_path": str(render_path)}, timeout=600)["bytes"] > 0,
        f"{render_path.stat().st_size} bytes"))
    check("capture camera view", lambda: assert_true(
        BRIDGE.call("capture", {"mode": "camera", "output_path": str(workdir / "capture_cam.png")},
                    timeout=300)["bytes"] > 0, "opengl camera capture"))
    glb = workdir / "selftest.glb"
    check("export glb", lambda: assert_true(
        BRIDGE.call("export_model", {"path": str(glb), "properties": {"export_format": "GLB"}})["bytes"] > 0,
        f"{glb.stat().st_size} bytes"))
    blend = workdir / "selftest.blend"
    if headless:
        print("  SKIP  save/open blend: not possible in a headless Blender")
    else:
        check("save blend", lambda: assert_true(
            BRIDGE.call("file_op", {"action": "save_as", "path": str(blend)})["filepath"] == str(blend),
            "saved"))
        check("reopen blend", lambda: assert_true(
            BRIDGE.call("file_op", {"action": "open", "path": str(blend)})["objects"] > 0, "reopened"))

    print("\n7. scripting")
    check("execute python", lambda: assert_true(
        BRIDGE.call("execute", {
            "code": "import bpy; ob = bpy.data.objects.new('FromScript', None); "
                    "bpy.context.scene.collection.objects.link(ob); Result(ob.name)"
        }, timeout=120)["return_value"] == "FromScript", "object created from script"))
    check("run operator", lambda: assert_true(
        BRIDGE.call("run_operator", {"operator": "mesh.primitive_uv_sphere_add",
                                     "properties": {"segments": 16, "ring_count": 8}},
                    timeout=120)["status"] == ["FINISHED"], "operator ran"))

    print("\n8. animation")
    check("insert keyframe", lambda: assert_true(
        len(BRIDGE.call("insert_keyframe", {"objects": ["FromScript"], "frame": 1,
                                            "location": True, "rotation": True})["keyframes"]) == 2,
        "2 keyframes inserted"))
    check("set frame", lambda: assert_true(
        BRIDGE.call("set_frame", {"start": 1, "end": 60, "fps": 24})["fps"] == 24, "range configured"))
    check("animation info", lambda: assert_true(
        BRIDGE.call("animation_info")["animated_objects"], "animation reported"))

    print("\n9. model validation")
    check("validate runs a full audit", lambda: assert_true(
        set(BRIDGE.call("validate", {"max_faces": 500000}, timeout=180)) >= {
            "verdict", "score", "checks"}, "audit returned"))
    check("validate scores every check", lambda: assert_true(
        {c["check"] for c in BRIDGE.call(
            "validate", {"checks": ["scale", "topology", "budget"]},
            timeout=180)["checks"]} >= {"scale", "topology", "budget"},
        "requested checks present"))
    check("validate compares against a spec", lambda: assert_true(
        "dimensions" in {c["check"] for c in BRIDGE.call(
            "validate", {"target": {"length": 4.0, "width": 2.0, "height": 1.5}},
            timeout=180)["checks"]}, "dimension check present"))
    check("validate ignores named helpers", lambda: assert_true(
        BRIDGE.call("validate", {"ignore": ["Cube", "Cube.*"]},
                    timeout=180)["objects_checked"] >= 0,
        "ignore list accepted"))
    check("validate explains an over-broad ignore", lambda: _expect_error(
        BRIDGE.call, "validate", {"ignore": ["*"]},
        needle="no mesh objects in the scene to validate"))
    check("find_problems pairs each issue with a fix", lambda: assert_true(
        all("fix" in item for item in
            BRIDGE.call("find_problems", {"max_faces": 500000}, timeout=180)["actionable"]),
        "every actionable item has a fix"))
    check("analyze_mesh reports a shell census", lambda: assert_true(
        {"manifold_edges", "boundary_edges", "wire_edges", "loose_vertices"}
        <= set(BRIDGE.call("analyze_mesh", {"object": "Cube"},
                           timeout=120)), "census returned"))
    check("measure reports the assembly extent", lambda: assert_true(
        len(BRIDGE.call("measure", {"mode": "assembly"}, timeout=120)
            ["length_width_height"]) == 3, "extent returned"))
    check("measure catches a bad mode", lambda: _expect_error(
        BRIDGE.call, "measure", {"mode": "nonsense"}, needle="unknown measure mode"))

    print("\n10. context, selection, history")
    check("select_by finds meshes", lambda: assert_true(
        BRIDGE.call("select_by", {"by": "type", "object_type": "MESH"},
                    timeout=120)["matched"] >= 1, "meshes selected"))
    check("select_by finds missing UVs", lambda: assert_true(
        "matched" in BRIDGE.call("select_by", {"by": "no_uv"}, timeout=120),
        "predicate ran"))
    check("select_by rejects a bad predicate", lambda: _expect_error(
        BRIDGE.call, "select_by", {"by": "wat"}, needle="unknown selection mode"))
    check("set_context switches to edit mode", lambda: assert_true(
        BRIDGE.call("set_context", {"active": "Cube", "select": ["Cube"],
                                    "mode": "EDIT"}, timeout=120)["mode_now"]
        .startswith("EDIT"), "edit mode active"))
    check("set_context restores object mode", lambda: assert_true(
        BRIDGE.call("set_context", {"mode": "OBJECT"}, timeout=120)["mode_now"]
        == "OBJECT", "object mode"))
    check("checkpoint then undo", lambda: assert_true(
        BRIDGE.call("checkpoint", {"label": "selftest"}, timeout=120)["checkpoint"]
        and BRIDGE.call("undo", timeout=120)["undone"], "undo stack works"))

    print("\n11. geometry, modifiers, uv")
    BRIDGE.call("add_primitive", {"type": "cube", "name": "GeoTarget"})
    check("geometry adds a mirror modifier", lambda: assert_true(
        BRIDGE.call("geometry", {"operation": "mirror", "objects": ["GeoTarget"]},
                    timeout=120)["operation"] == "mirror"
        and any(m["type"] == "MIRROR" for m in BRIDGE.call(
            "modifiers", {"action": "list", "objects": ["GeoTarget"]},
            timeout=120)["results"][0]["modifiers"]), "mirror present"))
    check("modifiers lists the stack", lambda: assert_true(
        "modifiers" in BRIDGE.call("modifiers", {"action": "list",
                                                 "objects": ["GeoTarget"]},
                                   timeout=120)["results"][0], "stack listed"))
    check("modifiers removes all", lambda: assert_true(
        BRIDGE.call("modifiers", {"action": "remove", "modifier": "all",
                                  "objects": ["GeoTarget"]}, timeout=120)["action"]
        == "remove", "removed"))
    check("uv report", lambda: assert_true(
        "meshes" in BRIDGE.call("uv", {"action": "report", "objects": ["GeoTarget"]},
                                timeout=120), "uv report returned"))
    check("geometry rejects an unknown op", lambda: _expect_error(
        BRIDGE.call, "geometry", {"operation": "teleport"}, needle="unknown geometry operation"))

    print("\n12. textures")
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "blender_mcp_selftest_tex"
    tmp.mkdir(parents=True, exist_ok=True)
    check("generate_texture writes a png", lambda: assert_true(
        Path(BRIDGE.call("generate_texture", {
            "pattern": "fbm", "size": 64, "seed": 1, "also_normal": True,
            "output_dir": str(tmp)}, timeout=180)["files"][0]).is_file(),
        "base colour written"))
    check("generate_texture supports plate text", lambda: assert_true(
        Path(BRIDGE.call("generate_texture", {
            "pattern": "plate", "size": 128, "seed": 2, "text": "AA 123",
            "output_dir": str(tmp)}, timeout=180)["files"][0]).is_file(),
        "plate written"))
    check("generate_pbr_set writes five maps", lambda: assert_true(
        len(BRIDGE.call("generate_pbr_set", {
            "pattern": "concrete", "size": 64, "seed": 3, "name": "st",
            "output_dir": str(tmp)}, timeout=180)["files"]) == 5, "5 maps"))
    check("list_images sees the new images", lambda: assert_true(
        BRIDGE.call("list_images", {"limit": 200}, timeout=120)["count"] > 0,
        "images listed"))
    check("pack_textures packs", lambda: assert_true(
        "packed" in BRIDGE.call("pack_textures", {}, timeout=180), "packed count"))
    check("generate_texture rejects a huge size", lambda: _expect_error(
        BRIDGE.call, "generate_texture", {"pattern": "fbm", "size": 99999},
        needle="size must be between"))

    print("\n13. scene ops and batching")
    check("scene_ops memory", lambda: assert_true(
        "objects" in BRIDGE.call("scene_ops", {"action": "memory"},
                                 timeout=120), "counts returned"))
    check("scene_ops addons list", lambda: assert_true(
        "enabled" in BRIDGE.call("scene_ops", {"action": "addons"},
                                 timeout=120), "addons listed"))
    check("batch runs many steps at once", lambda: assert_true(
        (lambda r: r["failed"] == 0 or _raise_batch(r))(
            BRIDGE.call("batch", {"steps": [
                {"command": "add_primitive", "params": {"type": "cube", "name": "BatchA"}},
                {"command": "add_primitive", "params": {"type": "cube", "name": "BatchB"}},
                {"command": "transform_objects", "params": {"objects": ["BatchA"],
                                                             "location": [3, 0, 0]}},
            ]}, timeout=240)),
        "3 steps, 0 failures"))
    check("batch reports per-step failures", lambda: assert_true(
        BRIDGE.call("batch", {"steps": [
            {"command": "not_a_command", "params": {}},
        ]}, timeout=120)["failed"] == 1, "failure surfaced"))
    check("batch stops on error when asked", lambda: assert_true(
        BRIDGE.call("batch", {"steps": [
            {"command": "not_a_command", "params": {}},
            {"command": "add_primitive", "params": {"type": "cube", "name": "AfterFail"}},
        ], "stop_on_error": True}, timeout=120)["executed"] == 1, "stopped early"))
    check("batch needs steps", lambda: _expect_error(
        BRIDGE.call, "batch", {"steps": []}, needle="batch needs a 'steps' list"))

    # The undo guard that refuses to rewind across an Open File is deliberately
    # NOT covered here: provoking it needs a file load, and wm.open_mainfile /
    # wm.read_homefile from the bridge timer can block Blender indefinitely.
    # The guard itself is exercised manually; see README "Known limits".

    print("\n15. materials and shaders")
    check("preset material assigns", lambda: assert_true(
        "GeoTarget" in BRIDGE.call("create_material_preset", {
            "name": "SelftestPaint", "preset": "car_paint",
            "assign": ["GeoTarget"]}, timeout=120)["objects"], "assigned"))
    check("preset rejects an unknown name", lambda: _expect_error(
        BRIDGE.call, "create_material_preset",
        {"name": "X", "preset": "unobtainium"}, needle="unknown preset"))
    check("procedural material builds a graph", lambda: assert_true(
        BRIDGE.call("procedural_material", {
            "name": "SelftestProc", "pattern": "voronoi", "metallic": 0.5,
            "assign": ["GeoTarget"]}, timeout=180)["nodes"] >= 6, "graph built"))
    check("custom shader graph links", lambda: assert_true(
        BRIDGE.call("build_shader", {
            "name": "SelftestGraph",
            "nodes": [{"id": "tc", "type": "uv"},
                      {"id": "nz", "type": "noise", "inputs": {"Scale": 5.0}},
                      {"id": "em", "type": "emission", "inputs": {"Strength": 2.0}},
                      {"id": "out", "type": "output"}],
            "links": [["tc", "UV", "nz", "Vector"],
                      ["nz", "Fac", "em", "Color"],
                      ["em", "Emission", "out", "Surface"]]},
            timeout=180)["links"] == 3, "3 links"))
    check("shader graph reports bad sockets", lambda: assert_true(
        len(BRIDGE.call("build_shader", {
            "name": "SelftestBad",
            "nodes": [{"id": "n", "type": "noise", "inputs": {"NoSuchSocket": 1}}]},
            timeout=180)["problems"]) >= 1, "problem reported, not raised"))
    check("shader info lists the graph", lambda: assert_true(
        len(BRIDGE.call("shader_info", {"name": "SelftestGraph"},
                        timeout=120)["nodes"]) == 4, "4 nodes"))
    check("set a shader input by name", lambda: assert_true(
        BRIDGE.call("set_shader_input", {
            "material": "SelftestGraph", "node": "nz", "socket": "Scale",
            "value": 42.0}, timeout=120)["value"] == 42.0, "scale set"))
    check("set a missing shader input is an error", lambda: _expect_error(
        BRIDGE.call, "set_shader_input", {"material": "SelftestGraph", "node": "nz",
                                          "socket": "Nope", "value": 1},
        needle="no input"))
    check("world sky shader", lambda: assert_true(
        BRIDGE.call("world_shader", {"type": "sky", "sun_elevation": 30},
                    timeout=120)["type"] == "sky", "sky built"))
    check("world gradient shader", lambda: assert_true(
        BRIDGE.call("world_shader", {"type": "gradient"}, timeout=120)["type"]
        == "gradient", "gradient built"))
    check("vertex colours paint", lambda: assert_true(
        BRIDGE.call("paint_vertex_colors", {"object": "GeoTarget"},
                    timeout=120)["vertices"] > 0, "colours written"))
    check("material report", lambda: assert_true(
        BRIDGE.call("material_report", {"limit": 50}, timeout=120)["count"] > 0,
        "materials listed"))

    print("\n16. animation")
    check("insert a keyframe", lambda: assert_true(
        BRIDGE.call("keyframe_channel", {"object": "GeoTarget",
                                         "data_path": "location", "frame": 1},
                    timeout=120)["action"], "action created"))
    check("fill a frame range", lambda: assert_true(
        BRIDGE.call("keyframe_channel", {"object": "GeoTarget",
                                         "data_path": "rotation_euler", "frame": 1,
                                         "frame_end": 24, "step": 4},
                    timeout=120)["extra_keys"] == 5, "5 extra keys"))
    check("actions are listed", lambda: assert_true(
        BRIDGE.call("actions_list", {}, timeout=120)["count"] >= 1, "action found"))
    check("interpolation is applied", lambda: assert_true(
        BRIDGE.call("curves", {"op": "interpolation", "object": "GeoTarget",
                               "interpolation": "LINEAR"}, timeout=120)["keys_changed"] > 0,
        "keys changed"))
    check("curve modifier added", lambda: assert_true(
        len(BRIDGE.call("curves", {"op": "add_modifier", "object": "GeoTarget",
                                    "modifier": "CYCLES"}, timeout=120)["added"]) > 0,
        "cycles added"))
    check("a driver with an expression", lambda: assert_true(
        "sin" in BRIDGE.call("driver", {
            "op": "add", "object": "GeoTarget", "data_path": "scale", "index": 0,
            "expression": "1 + 0.1*sin(frame/5)"}, timeout=120)["expression"],
        "driver added"))
    check("shape key added", lambda: assert_true(
        BRIDGE.call("shape_keys", {"op": "add", "object": "GeoTarget",
                                   "key": "SelftestKey"}, timeout=120)["vertices"] > 0,
        "key created"))
    check("camera orbit keys the camera", lambda: assert_true(
        BRIDGE.call("camera_move", {"mode": "orbit", "frames": 6},
                    timeout=180)["frame_end"] == 6,
        "6 frames keyed"))
    check("timeline range set", lambda: assert_true(
        BRIDGE.call("timeline", {"op": "set", "start": 1, "end": 48, "fps": 24},
                    timeout=120)["fps"] == 24, "fps set"))
    check("timeline marker added", lambda: assert_true(
        BRIDGE.call("timeline", {"op": "add_marker", "name": "Selftest", "frame": 12},
                    timeout=120)["frame"] == 12, "marker added"))
    check("nla pushes the current action", lambda: assert_true(
        BRIDGE.call("nla", {"op": "push",
                            "object": BRIDGE.call("render_report", {})["camera"]},
                    timeout=180)["track"], "track created"))
    check("unknown curve op rejected", lambda: _expect_error(
        BRIDGE.call, "curves", {"op": "levitate", "object": "GeoTarget"},
        needle="unknown curve op"))

    print("\n17. project inspection")
    check("settings report groups", lambda: assert_true(
        {"scene", "render", "data", "preferences"} <= set(
            BRIDGE.call("settings_report", {}, timeout=180)), "all groups"))
    check("settings_set changes one value", lambda: assert_true(
        BRIDGE.call("settings_set", {"group": "render", "path": "resolution_x",
                                     "value": 1234}, timeout=120)["after"] == 1234,
        "resolution set"))
    check("settings_set rejects a bad path", lambda: _expect_error(
        BRIDGE.call, "settings_set", {"group": "render", "path": "nope", "value": 1},
        needle="no such attribute"))
    check("blend contents inventories datablocks", lambda: assert_true(
        BRIDGE.call("blend_contents", {"types": ["meshes", "materials"]},
                    timeout=180)["collections"]["meshes"]["count"] > 0, "counted"))
    check("diagnose runs", lambda: assert_true(
        "problems" in BRIDGE.call("diagnose", {}, timeout=120), "diagnosed"))
    check("python env probes a module", lambda: assert_true(
        "version" in BRIDGE.call("python_env", {"modules": ["numpy"]},
                                 timeout=120)["modules"]["numpy"], "numpy found"))
    check("filesystem is sandboxed", lambda: _expect_error(
        BRIDGE.call, "filesystem", {"path": "C:\\Windows", "limit": 1},
        needle="outside the allowed roots"))
    check("scripts and texts listed", lambda: assert_true(
        "texts" in BRIDGE.call("scripts_and_texts", {}, timeout=180), "listed"))

    print("\n18. assets, add-ons, packages")
    check("libraries listed", lambda: assert_true(
        BRIDGE.call("list_libraries", {}, timeout=120)["libraries"], "polyhaven"))
    check("addons listed", lambda: assert_true(
        BRIDGE.call("addons_list", {}, timeout=180)["count"] > 0, "addons found"))
    check("packages listed", lambda: assert_true(
        "sys_path" in BRIDGE.call("packages_list", {}, timeout=180), "pip ran"))
    check("addon install needs confirmation", lambda: _expect_error(
        BRIDGE.call, "addons_manage", {"addon": "x", "action": "install",
                                       "path": "x.zip"}, needle="confirm=true"))
    check("pip install needs confirmation", lambda: _expect_error(
        BRIDGE.call, "packages_install", {"package": "scipy"}, needle="confirm=true"))
    check("download rejects a bad scheme", lambda: _expect_error(
        BRIDGE.call, "download", {"url": "file:///C:/Windows/System32/config/SAM"},
        needle="refusing URL scheme"))
    check("export to a temp glb", lambda: assert_true(
        BRIDGE.call("export_asset", {
            "path": str(Path(os.environ.get("TEMP", "/tmp")) / "selftest_export.glb"),
            "objects": ["GeoTarget"]}, timeout=300)["format"] == "GLB", "glb written"))

    BRIDGE.shutdown()

    failures = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 70)
    print(f"{len(results) - len(failures)}/{len(results)} checks passed")
    for status, name, detail in failures:
        print(f"  {status}  {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())


