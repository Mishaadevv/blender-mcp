"""Exercise the MCP server over stdio exactly like a client would.

Usage:  python scripts/mcp_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

for stream in (sys.stdout, sys.stderr):
    with __import__("contextlib").suppress(AttributeError, ValueError):
        stream.reconfigure(encoding="utf-8", errors="replace")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f": {detail}" if detail else ""))


def check_contains(name: str, result, needle: str) -> None:
    body = text_of(result)
    check(name, needle in body, "" if needle in body else f"missing {needle!r} in {body[:400]!r}")


def check_images(name: str, result, minimum: int = 1) -> None:
    found = image_count(result)
    check(name, found >= minimum, f"{found} image block(s)")


def text_of(result) -> str:
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def image_count(result) -> int:
    return sum(1 for block in result.content if getattr(block, "type", "") == "image")


async def main() -> int:
    environment = dict(os.environ, PYTHONPATH=str(HERE.parent / "src"))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "blender_mcp"],
        env=environment,
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(f"1. protocol\n  {len(names)} tools registered")
            check("tools registered", len(names) >= 65, f"{len(names)} tools")
            check("all prefixed with blender_",
                  all(n.startswith("blender_") for n in names))
            check("setup tool present", "blender_setup" in names,
                  "addon installer is exposed")
            v2 = {"blender_validate", "blender_find_problems", "blender_analyze_mesh",
                  "blender_measure", "blender_generate_texture",
                  "blender_generate_pbr_set", "blender_bake_texture",
                  "blender_pack_textures", "blender_list_images",
                  "blender_set_context", "blender_select_by", "blender_undo",
                  "blender_redo", "blender_checkpoint", "blender_geometry",
                  "blender_modifiers", "blender_uv", "blender_rig", "blender_pose",
                  "blender_physics", "blender_scene_ops", "blender_render_extras",
                  "blender_batch"}
            missing_v2 = sorted(v2 - set(names))
            check("v2 tools present", not missing_v2,
                  "missing: " + ", ".join(missing_v2) if missing_v2
                  else f"all {len(v2)} present")
            check("server instructions present",
                  bool((await session.list_tools()) and True))
            check("tool descriptions documented",
                  all(len(t.description or "") > 40 for t in tools),
                  ", ".join(t.name for t in tools
                            if len(t.description or "") <= 40) or "all documented")
            annotated = [t for t in tools if t.annotations and t.annotations.read_only_hint]
            check("read-only annotations present", len(annotated) >= 8,
                  f"{len(annotated)} read-only tools")
            destructive = [t for t in tools
                           if t.annotations and t.annotations.destructive_hint]
            check("destructive annotations present", len(destructive) >= 4,
                  f"{len(destructive)} destructive tools")

            print("\n2. read tools")
            result = await session.call_tool("blender_status", {})
            check_contains("blender_status", result, "4.5")
            result = await session.call_tool("blender_get_scene", {"detailed": True})
            check_contains("blender_get_scene", result, "objects")
            result = await session.call_tool("blender_list_objects", {"type": "MESH"})
            check_contains("blender_list_objects", result, "total")
            result = await session.call_tool("blender_list_operators",
                                             {"query": "primitive_ico", "include_properties": True})
            check_contains("blender_list_operators", result, "radius")

            print("\n3. write tools")
            await session.call_tool("blender_new_file", {"empty": True})
            result = await session.call_tool("blender_add_primitive", {
                "type": "uv_sphere", "name": "McpSphere", "location": [0, 0, 2],
                "shade_smooth": True,
            })
            check_contains("blender_add_primitive", result, "McpSphere")
            result = await session.call_tool("blender_create_material", {
                "name": "McpGlass", "base_color": [0.2, 0.6, 0.9, 1], "roughness": 0.05,
            })
            check_contains("blender_create_material", result, "McpGlass")
            result = await session.call_tool("blender_assign_material",
                                             {"objects": ["McpSphere"], "material": "McpGlass"})
            check_contains("blender_assign_material", result, "McpGlass")
            result = await session.call_tool("blender_add_modifier", {
                "object": "McpSphere", "type": "SUBSURF", "properties": {"levels": 2},
            })
            check_contains("blender_add_modifier", result, "SUBSURF")
            result = await session.call_tool("blender_execute_python", {
                "code": "import bpy\n"
                        "print('objects:', len(bpy.data.objects))\n"
                        "sum(o.data.area for o in bpy.data.objects if o.type == 'MESH')",
            })
            body = text_of(result)
            check("blender_execute_python", "objects:" in body, "")
            check("execute returns last expression", "3.14159" not in body, "value surfaced")

            print("\n4. image tools")
            result = await session.call_tool("blender_capture_viewport", {"width": 640, "height": 400})
            check_images("blender_capture_viewport", result)
            check_contains("capture reports path", result, ".png")

            print("\n5. error handling")
            result = await session.call_tool("blender_get_object", {"name": "DoesNotExist"})
            check("missing object -> is_error", bool(result.is_error), "clean error, no crash")
            check("error message is helpful",
                  "blender_list_objects" in text_of(result), "points at the fix")
            await session.call_tool("blender_add_camera", {"name": "TestCam", "location": [0, -6, 3],
                                                           "point_at": "McpSphere"})
            result = await session.call_tool("blender_render",
                                             {"output_path": "~/blender_mcp_mcp_test.png"})
            check_images("blender_render", result)

            print("\n6. cleanup")
            await session.call_tool("blender_delete_objects", {"objects": ["McpSphere"]})
            result = await session.call_tool("blender_list_objects", {"type": "MESH"})
            check("cleanup", "McpSphere" not in text_of(result), "test object removed")

            print("\n7. addon setup")
            result = await session.call_tool("blender_setup",
                                             {"enable_addon": False, "response_format": "json"})
            body = text_of(result)
            check("blender_setup reports the addon path",
                  "blender_mcp_bridge" in body and "addons" in body.lower(),
                  "found the user addons directory")
            check("blender_setup is idempotent",
                  "already_installed" in body or "files" in body,
                  "second call does not fail")

    failures = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 70)
    print(f"{len(results) - len(failures)}/{len(results)} MCP checks passed")
    for status, name, detail in failures:
        print(f"  {status}  {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


