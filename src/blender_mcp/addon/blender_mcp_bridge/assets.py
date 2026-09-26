"""Asset acquisition and environment control for the Blender MCP bridge.

Three jobs: bring files in from the internet, manage add-ons and Python
packages, and move models in and out of Blender.

Security posture, deliberately conservative:

* downloads are https/http only, size-capped, and never executed;
* add-on installs and ``pip install`` are refused unless the caller passes
  ``confirm=true``, because both run third-party code inside Blender;
* Python installs go to a project-local target directory, never into Blender's
  own bundled ``site-packages``, so a bad package cannot break the application.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import bpy

from .bridge import CommandError

ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
USER_AGENT = "blender-mcp/2.0 (+https://github.com/Mishaadevv/blender-mcp)"

IMPORT_EXTENSIONS = {
    ".fbx": "FBX", ".obj": "OBJ", ".gltf": "GLTF", ".glb": "GLTF",
    ".stl": "STL", ".ply": "PLY", ".usd": "USD", ".usda": "USD",
    ".usdc": "USD", ".usdz": "USDZ", ".abc": "ALEMBIC", ".dae": "COLLADA",
    ".3ds": "3DS", ".blend": "BLEND",
}

ASSET_ROOT = os.path.join(os.path.expanduser("~"), "BlenderMCP_Assets")

# Free, key-less asset APIs. Adding a source that needs a key would put a
# secret in the opencode config for very little gain.
LIBRARIES = {
    "polyhaven": {
        "label": "Poly Haven (CC0 HDRIs, textures, models)",
        "api": "https://api.polyhaven.com/assets",
        "file_api": "https://api.polyhaven.com/files",
        "types": ("hdris", "textures", "models"),
        "needs_key": False,
    },
}


def _root(sub: str = "") -> str:
    path = os.path.join(ASSET_ROOT, sub) if sub else ASSET_ROOT
    os.makedirs(path, exist_ok=True)
    return path


def _guard_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise CommandError(
            f"refusing URL scheme {parsed.scheme!r}; only http and https are allowed"
        )
    if not parsed.netloc:
        raise CommandError(f"URL has no host: {url!r}")
    return url


def _download(url: str, destination: str, max_bytes: int,
              timeout: float = 60.0) -> dict:
    _guard_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = destination + ".part"
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = response.headers.get("Content-Length")
            total = int(total) if total else None
            if total and total > max_bytes:
                raise CommandError(
                    f"file is {total / 1048576:.1f} MB, over the "
                    f"{max_bytes / 1048576:.0f} MB cap; raise max_bytes if you "
                    "really want it"
                )
            os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
            with open(tmp, "wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise CommandError(
                            f"download exceeded the {max_bytes / 1048576:.0f} MB cap"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise CommandError(f"HTTP {exc.code} fetching {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CommandError(f"could not reach {url}: {exc.reason}") from exc
    except CommandError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, destination)
    return {"path": destination, "bytes": written, "sha256": digest.hexdigest(),
            "url": url}


# --------------------------------------------------------------------------- #
# download + import
# --------------------------------------------------------------------------- #
def download(params: dict) -> dict:
    """Fetch a URL to a local file. Nothing is executed; it just lands on disk."""
    url = str(params.get("url", ""))
    if not url:
        raise CommandError("download needs a 'url'")
    name = params.get("filename") or os.path.basename(
        urllib.parse.urlparse(url).path) or "download.bin"
    sub = str(params.get("directory", "downloads"))
    destination = os.path.join(_root(sub), name)
    result = _download(url, destination,
                       int(params.get("max_bytes", DEFAULT_MAX_BYTES)),
                       float(params.get("timeout", 60.0)))
    expected = params.get("sha256")
    if expected and result["sha256"].lower() != str(expected).lower():
        raise CommandError(
            f"checksum mismatch for {name}: expected {expected}, "
            f"got {result['sha256']}"
        )
    return result


def _import_one(path: str, params: dict) -> dict:
    """Route a file to the right importer based on extension."""
    extension = os.path.splitext(path)[1].lower()
    fmt = IMPORT_EXTENSIONS.get(extension)
    if fmt is None:
        raise CommandError(
            f"do not know how to import {extension!r}; supported: "
            + ", ".join(sorted(IMPORT_EXTENSIONS))
        )
    before = set(bpy.data.objects)
    options = dict(params.get("import_options") or {})
    try:
        if fmt == "FBX":
            bpy.ops.import_scene.fbx(filepath=path, **options)
        elif fmt == "OBJ":
            bpy.ops.wm.obj_import(filepath=path, **options)
        elif fmt in {"GLTF", "GLB"}:
            bpy.ops.import_scene.gltf(filepath=path, **options)
        elif fmt == "STL":
            bpy.ops.wm.stl_import(filepath=path, **options)
        elif fmt == "PLY":
            bpy.ops.wm.ply_import(filepath=path, **options)
        elif fmt in {"USD", "USDZ"}:
            bpy.ops.wm.usd_import(filepath=path, **options)
        elif fmt == "ALEMBIC":
            bpy.ops.wm.alembic_import(filepath=path, **options)
        elif fmt == "COLLADA":
            bpy.ops.wm.collada_import(filepath=path, **options)
        elif fmt == "BLEND":
            with bpy.data.libraries.load(path, link=False) as (source, target):
                target.objects = source.objects
            for ob in source.objects:
                if ob is not None:
                    bpy.context.scene.collection.objects.link(ob)
        else:
            raise CommandError(f"importer for {fmt} is not available in this build")
    except CommandError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CommandError(f"{fmt} import failed: {type(exc).__name__}: {exc}") from exc
    created = [o.name for o in bpy.data.objects if o not in before]
    collection = None
    if created and params.get("into_collection"):
        col = bpy.data.collections.get(str(params["into_collection"]))
        if col is None:
            col = bpy.data.collections.new(str(params["into_collection"]))
            bpy.context.scene.collection.children.link(col)
        for name in created:
            ob = bpy.data.objects.get(name)
            if ob:
                for existing in list(ob.users_collection):
                    existing.objects.unlink(ob)
                col.objects.link(ob)
        collection = col.name
    return {"file": path, "format": fmt, "objects": created,
            "count": len(created), "collection": collection}


def import_asset(params: dict) -> dict:
    """Import a model from a URL or a local path.

    `url` downloads first (respecting `max_bytes`), `path` reads from disk.
    Everything lands in the scene as ordinary, editable, separate objects.
    """
    path = params.get("path")
    downloaded = None
    if params.get("url"):
        name = params.get("filename") or os.path.basename(
            urllib.parse.urlparse(str(params["url"])).path) or "asset.glb"
        if not os.path.splitext(name)[1]:
            name += ".glb"
        sub = str(params.get("directory", "models"))
        downloaded = _download(str(params["url"]),
                               os.path.join(_root(sub), name),
                               int(params.get("max_bytes", DEFAULT_MAX_BYTES)),
                               float(params.get("timeout", 120.0)))
        path = downloaded["path"]
    if not path:
        raise CommandError("import_asset needs either 'url' or 'path'")
    path = str(path)
    if not os.path.isfile(path):
        raise CommandError(f"no such file: {path}")
    result = _import_one(path, params)
    if downloaded:
        result["downloaded"] = {k: downloaded[k] for k in ("bytes", "sha256")}
    return result


def export_asset(params: dict) -> dict:
    """Export objects to a file, with sensible per-format defaults."""
    path = str(params.get("path", "")).strip()
    if not path:
        raise CommandError("export needs a 'path'")
    extension = os.path.splitext(path)[1].lower()
    fmt = params.get("format") or IMPORT_EXTENSIONS.get(extension)
    if not fmt:
        raise CommandError(f"no format for {extension!r}; pass 'format' explicitly")
    fmt = str(fmt).upper()
    # .glb is binary glTF: same importer/exporter, different container, and the
    # caller deserves to be told which one they actually got
    if fmt == "GLTF" and extension == ".glb":
        fmt = "GLB"
    names = params.get("objects")
    selected = []
    if names:
        names = [names] if isinstance(names, str) else list(names)
        for name in names:
            ob = bpy.data.objects.get(name)
            if ob is None:
                raise CommandError(f"no object named {name!r}")
            selected.append(ob)
        for ob in bpy.context.view_layer.objects:
            ob.select_set(False)
        for ob in selected:
            ob.select_set(True)
        bpy.context.view_layer.objects.active = selected[0]
        only_selection = True
    else:
        only_selection = False
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    options = dict(params.get("export_options") or {})
    if fmt == "FBX":
        options.setdefault("use_selection", only_selection)
        options.setdefault("apply_scale_options", "FBX_SCALE_ALL")
        options.setdefault("mesh_smooth_type", "FACE")
        options.setdefault("path_mode", "COPY")
        options.setdefault("embed_textures", True)
        bpy.ops.export_scene.fbx(filepath=path, **options)
    elif fmt == "OBJ":
        options.setdefault("export_selected_objects", only_selection)
        options.setdefault("export_materials", True)
        options.setdefault("export_normals", True)
        options.setdefault("export_uv", True)
        bpy.ops.wm.obj_export(filepath=path, **options)
    elif fmt in {"GLTF", "GLB"}:
        options.setdefault("export_format",
                           "GLB" if fmt == "GLB" else "GLTF_SEPARATE")
        options.setdefault("use_selection", only_selection)
        options.setdefault("export_apply", True)
        options.setdefault("export_materials", "EXPORT")
        options.setdefault("export_texcoords", True)
        options.setdefault("export_normals", True)
        bpy.ops.export_scene.gltf(filepath=path, **options)
    elif fmt == "USD":
        bpy.ops.wm.usd_export(filepath=path, **options)
    elif fmt == "STL":
        bpy.ops.wm.stl_export(filepath=path, **options)
    elif fmt == "ALEMBIC":
        bpy.ops.wm.alembic_export(filepath=path, **options)
    else:
        raise CommandError(f"export format {fmt!r} is not wired up in this build")
    if not os.path.isfile(path):
        raise CommandError(f"{fmt} export reported success but {path} is missing")
    return {"file": path, "format": fmt,
            "bytes": os.path.getsize(path),
            "objects": [o.name for o in selected] if selected else "all selected"}


# --------------------------------------------------------------------------- #
# asset libraries
# --------------------------------------------------------------------------- #
def _api_get(url: str, timeout: float = 30.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CommandError(f"HTTP {exc.code} from {url}: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise CommandError(f"library request failed: {type(exc).__name__}: {exc}") from exc


def list_libraries(params: dict) -> dict:
    return {"libraries": [
        {"id": key, "label": spec["label"], "types": list(spec["types"]),
         "needs_key": spec["needs_key"]}
        for key, spec in LIBRARIES.items()],
        "asset_root": ASSET_ROOT,
        "supported_import": sorted(IMPORT_EXTENSIONS)}


def search_library(params: dict) -> dict:
    """Search a free CC0 asset library. Poly Haven needs no API key."""
    library = str(params.get("library", "polyhaven"))
    if library not in LIBRARIES:
        raise CommandError(f"unknown library {library!r}; have "
                           + ", ".join(LIBRARIES))
    spec = LIBRARIES[library]
    kind = str(params.get("type", "hdris")).lower()
    if kind not in spec["types"]:
        raise CommandError(f"{library} has no {kind!r}; types: "
                           + ", ".join(spec["types"]))
    query = str(params.get("query", "")).strip().lower()
    limit = int(params.get("limit", 25))
    assets = _api_get(f"{spec['api']}?t={kind}")
    if not isinstance(assets, dict):
        raise CommandError(f"unexpected response from {library}")
    rows = []
    for asset_id, meta in assets.items():
        if query and query not in asset_id.lower():
            continue
        tags = [t for t in meta.get("tags", []) if query in t.lower()][:8] \
            if query else meta.get("tags", [])[:8]
        rows.append({"id": asset_id, "name": meta.get("name", asset_id),
                     "tags": tags, "type": kind})
        if len(rows) >= limit:
            break
    return {"library": library, "type": kind, "count": len(rows), "assets": rows}


def fetch_from_library(params: dict) -> dict:
    """Download an asset from a library, and optionally import or shade it."""
    library = str(params.get("library", "polyhaven"))
    asset_id = str(params.get("id", ""))
    if not asset_id:
        raise CommandError("fetch_from_library needs an 'id' from search_library")
    kind = str(params.get("type", "hdris"))
    spec = LIBRARIES[library]
    resolution = str(params.get("resolution", "1k"))
    files = _api_get(f"{spec['file_api']}/{asset_id}")
    if not isinstance(files, dict) or kind not in files:
        raise CommandError(f"{library} has no {kind!r} for {asset_id!r}")
    entry = files[kind]
    urls: list[str] = []

    def collect(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("url"), str):
                urls.append(node["url"])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(entry.get(resolution) or entry)
    if not urls:
        collect(entry)
    if not urls:
        raise CommandError(f"no downloadable file found for {asset_id!r} at {resolution}")
    if kind == "hdris":
        url = next((u for u in urls if u.lower().endswith((".hdr", ".exr"))), urls[0])
    elif kind == "textures":
        url = next((u for u in urls if u.lower().endswith(("_diffuse", "_nor_gl",
                                                          "_rough", ".png"))), urls[0])
    else:
        url = urls[0]
    if params.get("format") == "gltf" or (kind == "models" and
                                           url.lower().endswith((".glb", ".gltf"))):
        return import_asset({"url": url, "filename": f"{asset_id}{os.path.splitext(url)[1]}",
                             "directory": f"{library}/{kind}",
                             "into_collection": params.get("into_collection")})
    target = os.path.join(_root(f"{library}/{kind}"), f"{asset_id}{resolution}"
                          + os.path.splitext(url)[1])
    result = _download(url, target, int(params.get("max_bytes", DEFAULT_MAX_BYTES)),
                       float(params.get("timeout", 180.0)))
    result["asset_id"] = asset_id
    result["library"] = library
    result["type"] = kind
    if kind == "hdris" and params.get("set_as_world", True):
        from . import shading
        shading.world_shader({"type": "image", "path": result["path"],
                              "strength": float(params.get("strength", 1.0)),
                              "rotation": params.get("rotation", 0.0)})
        result["world"] = bpy.context.scene.world.name
    return result


# --------------------------------------------------------------------------- #
# add-ons and Python packages
# --------------------------------------------------------------------------- #
def addons_list(params: dict) -> dict:
    import addon_utils
    rows = []
    for module in addon_utils.modules():
        name = module.__name__
        try:
            loaded_default, loaded_state = addon_utils.check(name)
        except Exception as exc:  # noqa: BLE001
            rows.append({"name": name, "error": str(exc)})
            continue
        rows.append({"name": name, "enabled": bool(loaded_state),
                     "loaded_default": bool(loaded_default),
                     "version": getattr(module, "bl_info", {}).get("version")
                     if hasattr(module, "bl_info") else None,
                     "description": getattr(module, "bl_info", {}).get("description", "")
                     if hasattr(module, "bl_info") else ""})
    rows.sort(key=lambda r: r["name"])
    return {"count": len(rows), "addons": rows}


def addons_manage(params: dict) -> dict:
    """Enable, disable or install an add-on. Installing runs third-party code."""
    action = str(params.get("action", ""))
    name = str(params.get("addon", ""))
    if not name:
        raise CommandError("addons_manage needs an 'addon' module name")
    import addon_utils
    if action in {"enable", "disable"}:
        want = action == "enable"
        try:
            if want:
                addon_utils.enable(name, default_set=True, persistent=True)
            else:
                addon_utils.disable(name, default_set=True)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"could not {action} {name!r}: {exc}") from exc
        return {"addon": name, "action": action, "enabled": want}
    if action == "install":
        if not params.get("confirm"):
            raise CommandError(
                "installing an add-on executes third-party code inside Blender. "
                "Re-run with confirm=true if you trust the source."
            )
        source = params.get("path") or params.get("url")
        if not source:
            raise CommandError("install needs a local 'path' to a .zip or a 'url'")
        if str(source).startswith(("http://", "https://")):
            name_zip = params.get("filename") or f"{name}.zip"
            source = _download(str(source), os.path.join(_root("addons"), name_zip),
                               int(params.get("max_bytes", 64 * 1024 * 1024)))["path"]
        source = str(source)
        if not os.path.isfile(source):
            raise CommandError(f"no such add-on package: {source}")
        if not source.lower().endswith(".zip"):
            raise CommandError("add-ons must be installed from a .zip archive")
        try:
            bpy.ops.preferences.addon_install(filepath=source, overwrite=True)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"add-on install failed: {exc}") from exc
        module = params.get("module") or os.path.splitext(os.path.basename(source))[0]
        if params.get("enable", True):
            try:
                addon_utils.enable(module, default_set=True, persistent=True)
            except Exception as exc:  # noqa: BLE001
                raise CommandError(
                    f"installed, but enabling {module!r} failed: {exc}. The archive "
                    "may use a different module name than the file."
                ) from exc
        return {"addon": module, "action": "install", "source": source,
                "enabled": bool(params.get("enable", True))}
    raise CommandError(f"unknown action {action!r}; use enable, disable or install")


def packages_list(params: dict) -> dict:
    """What Python packages Blender's interpreter can see."""
    target = os.path.join(bpy.utils.user_resource("SCRIPTS", path="addons",
                                                  create=True), "_mcp_packages")
    ok, output = "", ""
    try:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=90, check=False)
        ok, output = completed.returncode == 0, completed.stdout
    except Exception as exc:  # noqa: BLE001
        output = f"pip unavailable: {exc}"
    packages = []
    if ok:
        try:
            packages = [{"name": p["name"], "version": p["version"]}
                        for p in json.loads(output)]
        except ValueError:
            packages = []
    return {
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "blender": bpy.app.version_string,
        "sys_path": list(sys.path),
        "package_count": len(packages),
        "packages": sorted(packages, key=lambda p: p["name"].lower())[:400],
        "writable_target": target,
        "target_exists": os.path.isdir(target),
    }


def packages_install(params: dict) -> dict:
    """pip install into a project-local directory, not into Blender itself.

    Refuses without ``confirm=true``: this downloads and runs package code.
    """
    requirement = str(params.get("package", ""))
    if not requirement:
        raise CommandError("packages_install needs a 'package', e.g. 'numpy==2.1'")
    if not params.get("confirm"):
        raise CommandError(
            f"installing {requirement!r} downloads and executes third-party code. "
            "Re-run with confirm=true if you trust it. Packages go into a "
            "project-local directory, not Blender's own site-packages."
        )
    target = os.path.join(bpy.utils.user_resource("SCRIPTS", path="addons",
                                                  create=True), "_mcp_packages")
    os.makedirs(target, exist_ok=True)
    command = [sys.executable, "-m", "pip", "install", "--upgrade",
               "--target", target, requirement]
    if params.get("no_deps"):
        command.insert(-1, "--no-deps")
    try:
        completed = subprocess.run(command, capture_output=True, text=True,  # noqa: S603
                                   timeout=int(params.get("timeout", 600)),
                                   check=False)
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"pip install timed out after {exc.timeout}s") from exc
    if completed.returncode != 0:
        raise CommandError(f"pip install failed:\n{completed.stderr[-1500:]}")
    if target not in sys.path:
        sys.path.insert(0, target)
    installed = []
    try:
        check = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pip", "list", "--format=json",
             "--path", target],
            capture_output=True, text=True, timeout=60, check=False)
        installed = [{"name": p["name"], "version": p["version"]}
                     for p in json.loads(check.stdout or "[]")]
    except Exception:  # noqa: BLE001
        pass
    return {"package": requirement, "target": target, "installed": installed,
            "note": "add the target directory to sys.path in a later session to use it"}


def node_group_append(params: dict) -> dict:
    """Append a node group from a .blend file - shader or geometry nodes."""
    path = str(params.get("path", ""))
    if not os.path.isfile(path):
        raise CommandError(f"no such blend file: {path}")
    wanted = params.get("names")
    before = set(bpy.data.node_groups)
    with bpy.data.libraries.load(path, link=False) as (source, target):
        target.node_groups = source.node_groups
    created = [g.name for g in bpy.data.node_groups if g not in before]
    if wanted:
        keep = {n for n in (wanted if isinstance(wanted, list) else [wanted])}
        for name in created:
            if name not in keep:
                group = bpy.data.node_groups.get(name)
                if group and group.users == 0:
                    bpy.data.node_groups.remove(group)
        created = [n for n in created if n in keep]
    if params.get("assign_to_material") and created:
        from . import shading
        graph = bpy.data.node_groups[created[0]]
        mat = bpy.data.materials.get(str(params["assign_to_material"]))
        if mat is None or not mat.use_nodes:
            raise CommandError("assign_to_material needs an existing node-based material")
        used = None
        for node in mat.node_tree.nodes:
            if node.type in {"GROUP", "GEOMETRY"}:
                used = node
                break
        if used is not None:
            used.node_tree = graph
    return {"source": path, "node_groups": created,
            "types": [bpy.data.node_groups[n].bl_idname for n in created
                      if n in bpy.data.node_groups]}
