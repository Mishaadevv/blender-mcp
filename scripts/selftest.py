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

    if not args.keep:
        BRIDGE.call("delete_objects", {"objects": ["FromScript"]})
        BRIDGE.shutdown()

    failures = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 70)
    print(f"{len(results) - len(failures)}/{len(results)} checks passed")
    for status, name, detail in failures:
        print(f"  {status}  {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())


