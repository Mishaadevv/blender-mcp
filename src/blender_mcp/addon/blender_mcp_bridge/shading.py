"""Material, shader and world authoring for the Blender MCP bridge.

`blender_execute_python` can technically build a node tree, but an agent has to
guess socket names and node types. This module knows them, so the common cases
are one call and the unusual ones are a declarative graph description.
"""

from __future__ import annotations

import math
from typing import Any

import bpy
from mathutils import Vector

from .bridge import CommandError, view3d_override


def _new_material(name: str) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if mat is not None:
        bpy.data.materials.remove(mat)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    return mat


def _principled(mat: bpy.types.Material) -> bpy.types.Node:
    return next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")


def _clear(mat: bpy.types.Material, keep_output: bool = True) -> None:
    tree = mat.node_tree
    for node in list(tree.nodes):
        if keep_output and node.type == "OUTPUT_MATERIAL":
            continue
        tree.nodes.remove(node)


def _drop_duplicate_outputs(mat: bpy.types.Material) -> int:
    """Remove every material output except the last one added.

    ``use_nodes = True`` leaves a default output behind, and ``_clear`` keeps it,
    so a freshly built graph would otherwise end up with two Output nodes - one
    connected to nothing, which silently breaks the shader.
    """
    tree = mat.node_tree
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    removed = 0
    for extra in outputs[:-1]:
        tree.nodes.remove(extra)
        removed += 1
    return removed


def _set(node, socket: str, value: Any) -> bool:
    if socket in node.inputs:
        _assign(node.inputs[socket], value)
        return True
    return False


def _assign(socket, value: Any) -> None:
    """Write a value into a socket, tolerating sockets that hold none.

    Shader and group sockets (`NodeSocketShader`) have no ``default_value`` at
    all, so writing to one unconditionally raises.
    """
    if not hasattr(socket, "default_value"):
        raise CommandError(
            f"socket {socket.name!r} is a {socket.bl_idname} and holds no value; "
            "connect it to another node instead"
        )
    if socket.type == "RGBA":
        if isinstance(value, (list, tuple)):
            socket.default_value = (*value[:3], 1.0) if len(value) == 3 else value
        else:
            socket.default_value = (value, value, value, 1.0)
    else:
        socket.default_value = value


def _socket_value(socket) -> Any:
    if not hasattr(socket, "default_value"):
        return None
    value = socket.default_value
    if hasattr(value, "__len__"):
        return [round(v, 4) if isinstance(v, float) else v for v in value]
    if isinstance(value, float):
        return round(value, 5)
    return value


# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #
# Each preset is (base_color, metallic, roughness, ior, alpha, transmission,
#                coat, coat_roughness, emission_color, emission_strength)
PRESETS: dict[str, dict[str, Any]] = {
    "plastic":        dict(base=(0.05, 0.05, 0.055), metallic=0.0, rough=0.38, coat=0.2),
    "matte":          dict(base=(0.55, 0.55, 0.55), metallic=0.0, rough=0.85),
    "rubber":         dict(base=(0.018, 0.018, 0.020), metallic=0.0, rough=0.78),
    "glass":          dict(base=(0.92, 0.95, 0.96), metallic=0.0, rough=0.02, ior=1.52,
                           transmission=1.0),
    "frosted_glass":  dict(base=(0.90, 0.93, 0.95), metallic=0.0, rough=0.28, ior=1.52,
                           transmission=0.9),
    "chrome":         dict(base=(0.95, 0.95, 0.96), metallic=1.0, rough=0.04),
    "brushed_steel":  dict(base=(0.62, 0.63, 0.64), metallic=1.0, rough=0.28),
    "gold":           dict(base=(0.94, 0.75, 0.30), metallic=1.0, rough=0.16),
    "copper":         dict(base=(0.72, 0.36, 0.18), metallic=1.0, rough=0.22),
    "aluminium":      dict(base=(0.86, 0.87, 0.88), metallic=1.0, rough=0.36),
    "car_paint":      dict(base=(0.55, 0.02, 0.02), metallic=0.55, rough=0.22, coat=1.0,
                           coat_rough=0.03),
    "car_paint_white": dict(base=(0.85, 0.85, 0.84), metallic=0.35, rough=0.20, coat=1.0,
                            coat_rough=0.03),
    "fabric":         dict(base=(0.18, 0.16, 0.14), metallic=0.0, rough=0.92),
    "leather":        dict(base=(0.09, 0.06, 0.04), metallic=0.0, rough=0.62),
    "wood":           dict(base=(0.24, 0.13, 0.05), metallic=0.0, rough=0.45, coat=0.25),
    "concrete":       dict(base=(0.42, 0.42, 0.41), metallic=0.0, rough=0.88),
    "asphalt":        dict(base=(0.045, 0.045, 0.048), metallic=0.0, rough=0.92),
    "ceramic":        dict(base=(0.88, 0.88, 0.86), metallic=0.0, rough=0.12, coat=0.6),
    "emissive":       dict(base=(0.02, 0.02, 0.02), metallic=0.0, rough=0.5,
                           emit=(1.0, 0.95, 0.85), emit_strength=8.0),
    "emissive_red":   dict(base=(0.05, 0.0, 0.0), metallic=0.0, rough=0.4,
                           emit=(1.0, 0.06, 0.03), emit_strength=6.0),
    "neon":           dict(base=(0.0, 0.0, 0.0), metallic=0.0, rough=0.3,
                           emit=(0.2, 0.9, 1.0), emit_strength=25.0),
    "toon":           dict(base=(0.8, 0.8, 0.8), metallic=0.0, rough=0.6),
    "velvet":         dict(base=(0.05, 0.02, 0.06), metallic=0.0, rough=0.95,
                           sheen=1.0),
    "sponge":         dict(base=(0.85, 0.72, 0.25), metallic=0.0, rough=0.95),
}


def create_material(params: dict) -> dict:
    """Create a material from a named preset, optionally overridden per socket."""
    name = str(params.get("name", "Material"))
    preset = str(params.get("preset", "matte"))
    if preset not in PRESETS:
        raise CommandError(f"unknown preset {preset!r}; available: "
                           + ", ".join(sorted(PRESETS)))
    spec = dict(PRESETS[preset])
    mat = _new_material(name)
    _clear(mat)
    out = mat.node_tree.nodes.new("ShaderNodeOutputMaterial")
    out.location = (300, 0)
    bsdf = mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 0)
    bsdf.label = preset
    mat.node_tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    _drop_duplicate_outputs(mat)

    base = params.get("base_color") or spec.get("base")
    _set(bsdf, "Base Color", (*base, 1.0) if len(base) == 3 else base)
    _set(bsdf, "Metallic", float(params.get("metallic", spec.get("metallic", 0.0))))
    _set(bsdf, "Roughness", float(params.get("roughness", spec.get("rough", 0.5))))
    _set(bsdf, "IOR", float(params.get("ior", spec.get("ior", 1.45))))
    if "transmission" in spec or "transmission" in params:
        _set(bsdf, "Transmission Weight",
             float(params.get("transmission", spec.get("transmission", 0.0))))
    if "coat" in spec or "coat" in params:
        _set(bsdf, "Coat Weight", float(params.get("coat", spec.get("coat", 0.0))))
        _set(bsdf, "Coat Roughness",
             float(params.get("coat_roughness", spec.get("coat_rough", 0.03))))
    if "emit" in spec or "emission_strength" in params:
        emit = params.get("emission_color") or spec.get("emit", (0, 0, 0))
        _set(bsdf, "Emission Color", (*emit, 1.0) if len(emit) == 3 else emit)
        _set(bsdf, "Emission Strength",
             float(params.get("emission_strength", spec.get("emit_strength", 0.0))))
    if "sheen" in spec:
        _set(bsdf, "Sheen Weight", float(params["sheen"]))

    alpha = float(params.get("alpha", 1.0))
    if alpha < 1.0:
        _set(bsdf, "Alpha", alpha)
        mat.blend_method = "BLEND" if hasattr(mat, "blend_method") else mat.blend_method

    if params.get("assign"):
        targets = params["assign"]
        targets = [targets] if isinstance(targets, str) else list(targets)
        assigned = assign({"material": name, "objects": targets})
        return {"material": name, "preset": preset, "nodes": len(mat.node_tree.nodes), **assigned}
    return {"material": name, "preset": preset, "nodes": len(mat.node_tree.nodes),
            "principled": {i.name: _socket_value(i)
                           for i in bsdf.inputs
                           if not i.is_linked and hasattr(i, "default_value")
                           and i.type in {"VALUE", "RGBA"}}}


def assign(params: dict) -> dict:
    """Put a material on objects, optionally appending rather than replacing."""
    mat = bpy.data.materials.get(str(params.get("material", "")))
    if mat is None:
        raise CommandError(f"no material named {params.get('material')!r}")
    names = params.get("objects")
    if not names:
        raise CommandError("assign needs 'objects'")
    names = [names] if isinstance(names, str) else list(names)
    append = bool(params.get("append_slot", False))
    slot = int(params.get("slot", 0))
    done = []
    for name in names:
        ob = bpy.data.objects.get(name)
        if ob is None or ob.type != "MESH":
            continue
        if append:
            ob.data.materials.append(mat)
        else:
            while len(ob.data.materials) <= slot:
                ob.data.materials.append(None)
            ob.data.materials[slot] = mat
        if params.get("select_faces"):
            for polygon in ob.data.polygons:
                polygon.material_index = slot
        done.append(ob.name)
    return {"material": mat.name, "objects": done, "append": append}


# --------------------------------------------------------------------------- #
# node graph authoring
# --------------------------------------------------------------------------- #
NODE_ALIASES = {
    "tex_noise": "ShaderNodeTexNoise",
    "noise": "ShaderNodeTexNoise",
    "tex_voronoi": "ShaderNodeTexVoronoi",
    "voronoi": "ShaderNodeTexVoronoi",
    "tex_wave": "ShaderNodeTexWave",
    "wave": "ShaderNodeTexWave",
    "tex_checker": "ShaderNodeTexChecker",
    "checker": "ShaderNodeTexChecker",
    "tex_gradient": "ShaderNodeTexGradient",
    "gradient": "ShaderNodeTexGradient",
    "tex_musgrave": "ShaderNodeTexMusgrave",
    "tex_image": "ShaderNodeTexImage",
    "image": "ShaderNodeTexImage",
    "tex_coord": "ShaderNodeTexCoord",
    "uv": "ShaderNodeTexCoord",
    "mapping": "ShaderNodeMapping",
    "bump": "ShaderNodeBump",
    "displace": "ShaderNodeDisplacement",
    "mix": "ShaderNodeMix",
    "mix_rgb": "ShaderNodeMixRGB",
    "math": "ShaderNodeMath",
    "ramp": "ShaderNodeValToRGB",
    "colorramp": "ShaderNodeValToRGB",
    "rgb": "ShaderNodeRGB",
    "value": "ShaderNodeValue",
    "fresnel": "ShaderNodeFresnel",
    "layer_weight": "ShaderNodeLayerWeight",
    "separate": "ShaderNodeSeparateXYZ",
    "combine": "ShaderNodeCombineXYZ",
    "hsv": "ShaderNodeHueSaturation",
    "invert": "ShaderNodeInvert",
    "gamma": "ShaderNodeGamma",
    "bright_contrast": "ShaderNodeBrightContrast",
    "normal_map": "ShaderNodeNormalMap",
    "bump_map": "ShaderNodeNormalMap",
    "ao": "ShaderNodeAmbientOcclusion",
    "attribute": "ShaderNodeAttribute",
    "object_info": "ShaderNodeObjectInfo",
    "camera_data": "ShaderNodeCameraData",
    "new_geometry": "ShaderNodeNewGeometry",
    "backfacing": "ShaderNodeNewGeometry",
    "emission": "ShaderNodeEmission",
    "diffuse": "ShaderNodeBsdfDiffuse",
    "glossy": "ShaderNodeBsdfAnisotropic",
    "transmission": "ShaderNodeBsdfTransmission",
    "transparent": "ShaderNodeBsdfTransparent",
    "add_shader": "ShaderNodeAddShader",
    "mix_shader": "ShaderNodeMixShader",
    "output": "ShaderNodeOutputMaterial",
    "volume_absorption": "ShaderNodeVolumeAbsorption",
    "volume_scatter": "ShaderNodeVolumeScatter",
    "light_path": "ShaderNodeLightPath",
    "blackbody": "ShaderNodeBlackbody",
    "sky": "ShaderNodeTexSky",
    "environment": "ShaderNodeEnvironmentTexture",
}


def build_graph(params: dict) -> dict:
    """Build a shader graph declaratively.

    `nodes` is a list of `{"id", "type", "location", "inputs"}`; `links` is a
    list of `[from_id, from_socket, to_id, to_socket]`. Socket names are the
    real Blender names, so anything the manual lists works.
    """
    name = str(params.get("name", "Shader"))
    nodes = params.get("nodes")
    if not nodes:
        raise CommandError("build_graph needs a 'nodes' list")
    links = params.get("links") or []
    mat = _new_material(name)
    _clear(mat)
    tree = mat.node_tree
    built: dict[str, bpy.types.Node] = {}
    problems: list[str] = []
    for spec in nodes:
        node_id = str(spec.get("id") or spec.get("name"))
        raw_type = str(spec.get("type", ""))
        node_type = NODE_ALIASES.get(raw_type, raw_type)
        try:
            node = tree.nodes.new(node_type)
        except RuntimeError as exc:
            problems.append(f"{node_id}: {raw_type} ({exc})")
            continue
        node.name = node_id
        node.label = str(spec.get("label", node_id))
        loc = spec.get("location", [0, 0])
        node.location = (float(loc[0]), float(loc[1]))
        for key, value in (spec.get("inputs") or {}).items():
            if key not in node.inputs:
                problems.append(f"{node_id}.{key}: no such input on {node_type}")
                continue
            try:
                _assign(node.inputs[key], value)
            except CommandError as exc:
                problems.append(f"{node_id}.{key}: {exc}")
            except (TypeError, ValueError) as exc:
                problems.append(f"{node_id}.{key}: {exc}")
        for key, value in (spec.get("properties") or {}).items():
            if hasattr(node, key):
                setattr(node, key, value)
        built[node_id] = node
    for link in links:
        if len(link) < 4:
            problems.append(f"malformed link {link}")
            continue
        src_id, src_sock, dst_id, dst_sock = link[0], link[1], link[2], link[3]
        src, dst = built.get(str(src_id)), built.get(str(dst_id))
        if src is None or dst is None:
            problems.append(f"link {src_id}->{dst_id}: unknown node")
            continue
        if src_sock in src.outputs and dst_sock in dst.inputs:
            tree.links.new(src.outputs[src_sock], dst.inputs[dst_sock])
        else:
            problems.append(f"link {src_id}.{src_sock} -> {dst_id}.{dst_sock}: "
                            "no such socket")
    _drop_duplicate_outputs(mat)
    if params.get("assign"):
        assign({"material": name, "objects": params["assign"]})
    return {"material": name, "nodes": len(built), "links": len(tree.links),
            "problems": problems,
            "available_types": sorted(set(NODE_ALIASES))[:0] or ""}


def graph_info(params: dict) -> dict:
    """Dump a material's node graph: every node, its sockets and connections."""
    name = str(params.get("name", ""))
    mat = bpy.data.materials.get(name)
    if mat is None:
        raise CommandError(f"no material named {name!r}")
    if not mat.use_nodes:
        return {"material": name, "node_based": False}
    tree = mat.node_tree
    nodes = []
    for node in tree.nodes:
        nodes.append({
            "name": node.name,
            "label": node.label,
            "type": node.bl_idname,
            "location": [round(v, 1) for v in node.location],
            "inputs": [{"name": s.name, "type": s.type,
                        "value": _socket_value(s), "linked": s.is_linked}
                       for s in node.inputs if s.enabled and not s.is_linked],
            "outputs": [s.name for s in node.outputs if s.enabled],
        })
    links = [{"from": l.from_node.name, "from_socket": l.from_socket.name,
              "to": l.to_node.name, "to_socket": l.to_socket.name}
             for l in tree.links]
    return {"material": name, "node_based": True, "blend_method":
            getattr(mat, "surface_render_method", getattr(mat, "blend_method", None)),
            "backface_culling": mat.use_backface_culling,
            "nodes": nodes, "links": links}


def set_input(params: dict) -> dict:
    """Set one input socket on a node, by node and socket name."""
    name = str(params.get("material", ""))
    mat = bpy.data.materials.get(name)
    if mat is None or not mat.use_nodes:
        raise CommandError(f"no node-based material named {name!r}")
    node_name = str(params.get("node", ""))
    node = mat.node_tree.nodes.get(node_name)
    if node is None:
        raise CommandError(f"{name} has no node named {node_name!r}; nodes: "
                           + ", ".join(n.name for n in mat.node_tree.nodes))
    socket_name = str(params.get("socket", ""))
    if socket_name not in node.inputs:
        raise CommandError(f"{node_name} has no input {socket_name!r}; inputs: "
                           + ", ".join(s.name for s in node.inputs))
    socket = node.inputs[socket_name]
    _assign(socket, params.get("value"))
    return {"material": name, "node": node_name, "socket": socket_name,
            "value": _socket_value(socket)}


def connect(params: dict) -> dict:
    mat = bpy.data.materials.get(str(params.get("material", "")))
    if mat is None or not mat.use_nodes:
        raise CommandError("no node-based material")
    src = mat.node_tree.nodes.get(str(params.get("from_node", "")))
    dst = mat.node_tree.nodes.get(str(params.get("to_node", "")))
    if src is None or dst is None:
        raise CommandError("both from_node and to_node must exist")
    s_from = str(params.get("from_socket", ""))
    s_to = str(params.get("to_socket", ""))
    if s_from not in src.outputs or s_to not in dst.inputs:
        raise CommandError(f"no socket {s_from!r} on {src.name} or {s_to!r} on {dst.name}")
    mat.node_tree.links.new(src.outputs[s_from], dst.inputs[s_to])
    return {"linked": f"{src.name}.{s_from} -> {dst.name}.{s_to}"}


def procedural_surface(params: dict) -> dict:
    """A ready procedural material: noise/voronoi driven colour + roughness + bump.

    This is the shape most hard-surface and natural surfaces actually need, and
    it is tedious to assemble by hand through the socket API.
    """
    name = str(params.get("name", "Procedural"))
    kind = str(params.get("pattern", "noise"))
    mat = _new_material(name)
    _clear(mat)
    tree = mat.node_tree
    nodes, links = tree.nodes, tree.links

    out = nodes.new("ShaderNodeOutputMaterial"); out.location = (760, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (440, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    coord = nodes.new("ShaderNodeTexCoord"); coord.location = (-880, 0)
    mapping = nodes.new("ShaderNodeMapping"); mapping.location = (-700, 0)
    links.new(coord.outputs["Object"], mapping.inputs["Vector"])

    bump = nodes.new("ShaderNodeBump"); bump.location = (240, -320)
    # a Bump node *feeds* the shader's Normal input; it has no Normal output to
    # read from, so this must not be wired the other way round
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    _set(bump, "Strength", float(params.get("bump_strength", 0.25)))
    _set(bump, "Distance", float(params.get("bump_distance", 0.02)))

    if kind in {"noise", "fbm"}:
        tex = nodes.new("ShaderNodeTexNoise"); tex.location = (-480, 0)
        tex.noise_dimensions = "3D"
        _set(tex, "Scale", float(params.get("scale", 6.0)))
        _set(tex, "Detail", float(params.get("detail", 8.0)))
        _set(tex, "Roughness", float(params.get("noise_roughness", 0.55)))
        if kind == "fbm":
            _set(tex, "Distortion", float(params.get("distortion", 0.0)))
        source = tex.outputs["Fac"]
    elif kind in {"voronoi", "cells"}:
        tex = nodes.new("ShaderNodeTexVoronoi"); tex.location = (-480, 0)
        feature = str(params.get("feature", "F1")).upper()
        if feature in {"F1", "F2", "SMOOTH_F1", "DISTANCE_TO_EDGE", "N_SPHERE_RADIUS"}:
            tex.feature = feature
        _set(tex, "Scale", float(params.get("scale", 6.0)))
        source = tex.outputs["Distance"] if "Distance" in tex.outputs else tex.outputs[0]
    elif kind in {"wave", "stripes"}:
        tex = nodes.new("ShaderNodeTexWave"); tex.location = (-480, 0)
        wave_type = str(params.get("wave_type", "BANDS")).upper()
        if wave_type in {"BANDS", "RINGS", "X", "Y", "Z", "DIAGONAL"}:
            tex.wave_type = wave_type
        _set(tex, "Scale", float(params.get("scale", 6.0)))
        _set(tex, "Distortion", float(params.get("distortion", 0.0)))
        source = tex.outputs["Fac"]
    elif kind in {"checker"}:
        tex = nodes.new("ShaderNodeTexChecker"); tex.location = (-480, 0)
        _set(tex, "Scale", float(params.get("scale", 8.0)))
        source = tex.outputs["Fac"]
    else:
        raise CommandError(f"unknown pattern {kind!r}; use noise, fbm, voronoi, "
                           "wave or checker")

    links.new(mapping.outputs["Vector"], tex.inputs["Vector"])

    ramp = nodes.new("ShaderNodeValToRGB"); ramp.location = (-200, 120)
    links.new(source, ramp.inputs["Fac"])
    elements = ramp.color_ramp.elements
    color_a = params.get("color_a", [0.05, 0.05, 0.05])
    color_b = params.get("color_b", [0.6, 0.6, 0.6])
    elements[0].position = float(params.get("ramp_low", 0.25))
    elements[0].color = (*color_a, 1.0)
    elements[1].position = float(params.get("ramp_high", 0.75))
    elements[1].color = (*color_b, 1.0)
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

    rough_ramp = nodes.new("ShaderNodeMapRange"); rough_ramp.location = (-200, -120)
    links.new(source, rough_ramp.inputs["Value"])
    rough_ramp.inputs["To Min"].default_value = float(params.get("rough_low", 0.25))
    rough_ramp.inputs["To Max"].default_value = float(params.get("rough_high", 0.85))
    links.new(rough_ramp.outputs["Result"], bsdf.inputs["Roughness"])
    links.new(source, bump.inputs["Height"])
    _drop_duplicate_outputs(mat)

    _set(bsdf, "Metallic", float(params.get("metallic", 0.0)))
    if params.get("assign"):
        assign({"material": name, "objects": params["assign"]})
    return {"material": name, "pattern": kind,
            "nodes": len(nodes), "links": len(links)}


# --------------------------------------------------------------------------- #
# world
# --------------------------------------------------------------------------- #
def world_shader(params: dict) -> dict:
    """Build the world shader: sky, gradient, HDRI image or a flat colour."""
    scene = bpy.context.scene
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    tree = world.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    out = tree.nodes.new("ShaderNodeOutputWorld"); out.location = (400, 0)
    background = tree.nodes.new("ShaderNodeBackground"); background.location = (180, 0)
    tree.links.new(background.outputs["Background"], out.inputs["Surface"])

    kind = str(params.get("type", "color"))
    strength = float(params.get("strength", 1.0))
    background.inputs["Strength"].default_value = strength

    if kind == "color":
        color = params.get("color", [0.05, 0.05, 0.06])
        background.inputs["Color"].default_value = (*color[:3], 1.0)
    elif kind == "gradient":
        tex = tree.nodes.new("ShaderNodeTexGradient"); tex.location = (-260, 0)
        tex.gradient_type = str(params.get("gradient_type", "LINEAR")).upper()
        mapping = tree.nodes.new("ShaderNodeMapping"); mapping.location = (-440, 0)
        mapping.inputs["Rotation"].default_value[1] = math.radians(
            float(params.get("angle", 90.0)))
        coord = tree.nodes.new("ShaderNodeTexCoord"); coord.location = (-620, 0)
        ramp = tree.nodes.new("ShaderNodeValToRGB"); ramp.location = (-60, 0)
        top = params.get("top_color", [0.25, 0.35, 0.55])
        bottom = params.get("bottom_color", [0.02, 0.02, 0.03])
        ramp.color_ramp.elements[0].color = (*bottom, 1.0)
        ramp.color_ramp.elements[1].color = (*top, 1.0)
        tree.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
        tree.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        tree.links.new(tex.outputs["Fac"], ramp.inputs["Fac"])
        tree.links.new(ramp.outputs["Color"], background.inputs["Color"])
    elif kind == "sky":
        tex = tree.nodes.new("ShaderNodeTexSky"); tex.location = (-160, 0)
        sky_type = str(params.get("sky_type", "NISHITA")).upper()
        if sky_type in {"NISHITA", "PREETHAM", "HOSEK_WILKIE"}:
            tex.sky_type = sky_type
        if params.get("sun_elevation") is not None:
            tex.sun_elevation = math.radians(float(params["sun_elevation"]))
        if params.get("sun_rotation") is not None:
            tex.sun_rotation = math.radians(float(params["sun_rotation"]))
        if params.get("altitude") is not None and hasattr(tex, "altitude"):
            tex.altitude = float(params["altitude"])
        if params.get("air_density") is not None and hasattr(tex, "air_density"):
            tex.air_density = float(params["air_density"])
        if params.get("sun_disc") is not None and hasattr(tex, "sun_disc"):
            tex.sun_disc = bool(params["sun_disc"])
        tree.links.new(tex.outputs["Color"], background.inputs["Color"])
    elif kind in {"image", "hdri"}:
        path = params.get("path")
        if not path:
            raise CommandError("world type 'image' needs a 'path' to an HDRI or image")
        import os
        if not os.path.isfile(path):
            raise CommandError(f"no such image file: {path}")
        tex = tree.nodes.new("ShaderNodeTexEnvironment"); tex.location = (-160, 0)
        tex.image = bpy.data.images.load(path, check_existing=True)
        if params.get("rotation") is not None:
            mapping = tree.nodes.new("ShaderNodeMapping"); mapping.location = (-340, 0)
            mapping.inputs["Rotation"].default_value[2] = math.radians(
                float(params["rotation"]))
            coord = tree.nodes.new("ShaderNodeTexCoord"); coord.location = (-520, 0)
            tree.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
            tree.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        tree.links.new(tex.outputs["Color"], background.inputs["Color"])
    else:
        raise CommandError(f"unknown world type {kind!r}; use color, gradient, "
                           "sky or image")
    return {"world": world.name, "type": kind, "strength": strength,
            "nodes": len(tree.nodes)}


# --------------------------------------------------------------------------- #
# advanced material helpers
# --------------------------------------------------------------------------- #
def add_triplanar_mapping(mat: bpy.types.Material, texture_node: bpy.types.Node,
                          scale: float = 1.0, blend: float = 0.5) -> bpy.types.Node:
    """Add triplanar mapping to a texture node for distortion-free texturing.

    Triplanar projects the texture along all three axes and blends them,
    eliminating stretching on complex geometry without UVs.
    """
    tree = mat.node_tree
    coord = tree.nodes.new("ShaderNodeTexCoord")
    coord.location = (texture_node.location.x - 400, texture_node.location.y)
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.location = (texture_node.location.x - 200, texture_node.location.y)
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    tree.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], texture_node.inputs["Vector"])
    return mapping


def add_procedural_normal(mat: bpy.types.Material, strength: float = 0.5,
                          distance: float = 0.1) -> bpy.types.Node:
    """Add a procedural bump/normal detail to a material.

    Uses a noise texture through a bump node for surface micro-detail.
    """
    tree = mat.node_tree
    bsdf = _principled(mat)
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.location = (bsdf.location.x - 400, bsdf.location.y - 200)
    noise.inputs["Scale"].default_value = 50.0
    noise.inputs["Detail"].default_value = 8.0
    bump = tree.nodes.new("ShaderNodeBump")
    bump.location = (bsdf.location.x - 200, bsdf.location.y - 200)
    bump.inputs["Strength"].default_value = strength
    bump.inputs["Distance"].default_value = distance
    tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return bump


def add_ambient_occlusion(mat: bpy.types.Material, distance: float = 0.5,
                          color: tuple = (0, 0, 0)) -> bpy.types.Node:
    """Add ambient occlusion to a material for contact shadows and depth."""
    tree = mat.node_tree
    bsdf = _principled(mat)
    ao = tree.nodes.new("ShaderNodeAmbientOcclusion")
    ao.location = (bsdf.location.x - 400, bsdf.location.y + 200)
    ao.inputs["Distance"].default_value = distance
    ao.inputs["Color"].default_value = (*color, 1.0)
    mix = tree.nodes.new("ShaderNodeMixRGB")
    mix.location = (bsdf.location.x - 200, bsdf.location.y + 200)
    mix.blend_type = "MULTIPLY"
    mix.inputs["Fac"].default_value = 1.0
    tree.links.new(bsdf.outputs["BSDF"], mix.inputs[1])
    tree.links.new(ao.outputs["Color"], mix.inputs[2])
    out = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL")
    tree.links.new(mix.outputs["Color"], out.inputs["Surface"])
    return ao


def paint_vertex_colors(params: dict) -> dict:
    """Write per-vertex colours procedurally. Useful as a mask or for toon work."""
    import numpy as np
    name = str(params.get("object", ""))
    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != "MESH":
        raise CommandError(f"no mesh object named {name!r}")
    me = ob.data
    layer = me.color_attributes.get(str(params.get("layer", "Col")))
    if layer is None:
        layer = me.color_attributes.new(name=str(params.get("layer", "Col")),
                                        type="FLOAT_COLOR", domain="POINT")
    axis = str(params.get("axis", "Z")).upper()
    lo = float(params.get("from", 0.0))
    hi = float(params.get("to", 1.0))
    c0 = params.get("color_a", [0, 0, 0])
    c1 = params.get("color_b", [1, 1, 1])
    coords = np.array([getattr(v.co, axis.lower()) for v in me.vertices],
                      dtype=np.float32)
    span = max(coords.max() - coords.min(), 1e-6)
    t = np.clip((coords - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    data = np.empty((len(me.vertices), 4), dtype=np.float32)
    for i in range(3):
        data[:, i] = np.array(c0[i]) * (1 - t) + np.array(c1[i]) * t
    data[:, 3] = 1.0
    layer.data.foreach_set("color", data.ravel())
    me.update()
    return {"object": ob.name, "layer": layer.name, "vertices": len(me.vertices),
            "axis": axis, "range": [round(float(coords.min()), 4),
                                    round(float(coords.max()), 4)]}


def material_report(params: dict) -> dict:
    """Every material in the file with its node count, users and key settings."""
    rows = []
    for mat in bpy.data.materials:
        users = [o.name for o in bpy.data.objects
                 if getattr(o.data, "materials", None)
                 and any(m == mat for m in o.data.materials)]
        images = [n.image.name for n in (mat.node_tree.nodes
                                         if mat.node_tree else [])
                  if n.type == "TEX_IMAGE" and n.image]
        rows.append({
            "name": mat.name,
            "users": len(users),
            "used_by": users[:12],
            "node_based": mat.use_nodes,
            "nodes": len(mat.node_tree.nodes) if mat.node_tree else 0,
            "images": images,
            "blend_method": getattr(mat, "surface_render_method",
                                    getattr(mat, "blend_method", None)),
            "backface_culling": mat.use_backface_culling,
        })
    return {"count": len(rows), "materials": rows[:int(params.get("limit", 200))]}
