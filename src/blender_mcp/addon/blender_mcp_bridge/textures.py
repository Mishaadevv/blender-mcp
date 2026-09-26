"""Procedural texture generation for the Blender MCP bridge.

Real geometry and real image files: every generator produces a float field with
numpy, converts it to a Blender image datablock, and writes a PNG. That means
the result can be inspected on disk, packed into the .blend, and shipped to an
engine - it is not a viewport-only trick.

Numpy ships with Blender, so there is no dependency to install.
"""

from __future__ import annotations

import math
import os
from typing import Any

import bpy
import numpy as np

MAX_SIZE = 4096
DEFAULT_SIZE = 1024

# --------------------------------------------------------------------------- #
# tiny 5x7 bitmap font, enough for licence plates and stencils
# --------------------------------------------------------------------------- #
FONT: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
}

STOCK = {
    "solid": (0.5, 0.5),
    "noise": (0.35, 0.65),
    "fbm": (0.30, 0.70),
    "voronoi": (0.15, 0.85),
    "checker": (0.0, 1.0),
    "grid": (0.0, 1.0),
    "stripes": (0.0, 1.0),
    "gradient": (0.0, 1.0),
    "radial": (0.0, 1.0),
    "brushed_metal": (0.25, 0.60),
    "rust": (0.0, 1.0),
    "scratches": (0.0, 1.0),
    "grunge": (0.0, 1.0),
    "concrete": (0.25, 0.70),
    "wood": (0.0, 1.0),
    "leather": (0.0, 1.0),
    "carbon": (0.0, 1.0),
    "hazard": (0.0, 1.0),
    "asphalt": (0.10, 0.55),
    "plate": (0.0, 1.0),
}

COLOR_MAPS = ("base_color", "roughness", "metallic", "normal", "ao", "height", "emission")


# --------------------------------------------------------------------------- #
# field helpers
# --------------------------------------------------------------------------- #
def _grid(size: int) -> np.ndarray:
    t = np.linspace(0.0, 1.0, size, dtype=np.float32)
    return t


def _value_noise(size: int, cells: int, rng: np.random.Generator) -> np.ndarray:
    """Bilinear value noise on a cells x cells lattice, smoothstep interpolated."""
    lattice = rng.random((cells + 1, cells + 1)).astype(np.float32)
    lattice[-1, :] = lattice[0, :]
    lattice[:, -1] = lattice[:, 0]
    t = _grid(size) * cells
    i0 = np.floor(t).astype(np.int32)
    f = t - i0
    f = f * f * (3.0 - 2.0 * f)
    i1 = np.minimum(i0 + 1, cells)
    out = (lattice[np.ix_(i0, i0)] * (1 - f)[:, None]
           + lattice[np.ix_(i1, i0)] * f[:, None]) * (1 - f)[None, :]
    out2 = (lattice[np.ix_(i0, i1)] * (1 - f)[:, None]
            + lattice[np.ix_(i1, i1)] * f[:, None]) * (1 - f)[None, :]
    return out * (1 - f)[None, :] + out2 * f[None, :]


def _fbm(size: int, octaves: int, base_cells: int, gain: float,
         lacunarity: float, rng: np.random.Generator) -> np.ndarray:
    total = np.zeros((size, size), dtype=np.float32)
    amp, norm, cells = 1.0, 0.0, base_cells
    for _ in range(max(1, octaves)):
        total += amp * _value_noise(size, max(2, int(cells)), rng)
        norm += amp
        amp *= gain
        cells *= lacunarity
    return total / max(norm, 1e-6)


def _voronoi(size: int, cells: int, rng: np.random.Generator) -> np.ndarray:
    points = rng.random((cells, cells, 2)).astype(np.float32)
    t = _grid(size)
    xs = t * cells
    ys = t * cells
    cx = np.clip(xs.astype(np.int32), 0, cells - 1)
    cy = np.clip(ys.astype(np.int32), 0, cells - 1)
    fx, fy = xs - cx, ys - cy
    best = np.full((size, size), 9.0, dtype=np.float32)
    second = np.full((size, size), 9.0, dtype=np.float32)
    for ox in (-1, 0, 1):
        for oy in (-1, 0, 1):
            gx, gy = cx + ox, cy + oy
            gx_c = np.mod(gx, cells)
            gy_c = np.mod(gy, cells)
            px = points[gx_c, gy_c, 0] + (gx - gx_c)
            py = points[gx_c, gy_c, 1] + (gy - gy_c)
            d = np.sqrt((px - fx[:, None]) ** 2 + (py - fy[None, :]) ** 2)
            closer = d < best
            second = np.where(closer, best, np.minimum(second, d))
            best = np.where(closer, d, best)
    return np.clip((second - best) * float(cells) * 0.5, 0.0, 1.0)


def _norm01(a: np.ndarray, lo: float, hi: float) -> np.ndarray:
    amin, amax = float(a.min()), float(a.max())
    if amax - amin < 1e-9:
        return np.full_like(a, (lo + hi) * 0.5)
    return lo + (a - amin) / (amax - amin) * (hi - lo)


def _draw_text(field: np.ndarray, text: str, cx: float, cy: float,
               scale: float) -> None:
    """Stamp 5x7 glyphs into a height field, centred on (cx, cy) in 0..1 space."""
    size = field.shape[0]
    glyph_w, glyph_h = 5, 7
    text = text.upper()
    step = max(1, int(round(scale)))
    total_w = len(text) * (glyph_w + 1) - 1
    x0 = int((cx - total_w * step / 2 / size) * size)
    y0 = int((cy + glyph_h * step / 2 / size) * size)
    for gi, ch in enumerate(text):
        rows = FONT.get(ch)
        if rows is None:
            continue
        gx = x0 + gi * (glyph_w + 1) * step
        for ry, row in enumerate(rows):
            for rx, bit in enumerate(row):
                if bit != "1":
                    continue
                px = gx + rx * step
                py = y0 - ry * step
                y0i, y1i = max(py - step, 0), min(py + step, size)
                x0i, x1i = max(px - step, 0), min(px + step, size)
                if y1i > y0i and x1i > x0i:
                    field[y0i:y1i, x0i:x1i] = 1.0


def height_field(kind: str, size: int, p: dict, rng: np.random.Generator) -> np.ndarray:
    """Every generator returns a 0..1 float field; the caller decides its meaning."""
    scale = float(p.get("scale", 8.0))
    contrast = float(p.get("contrast", 1.0))
    lo, hi = STOCK.get(kind, (0.0, 1.0))

    if kind == "solid":
        return np.full((size, size), 0.5, dtype=np.float32)

    if kind == "noise":
        f = _value_noise(size, max(2, int(scale)), rng)
        return _norm01(f, lo, hi)

    if kind == "fbm":
        f = _fbm(size, int(p.get("octaves", 6)), max(2, int(scale)),
                 float(p.get("gain", 0.5)), float(p.get("lacunarity", 2.0)), rng)
        return _norm01(f, lo, hi)

    if kind == "voronoi":
        f = _voronoi(size, max(2, int(scale)), rng)
        return _norm01(f, lo, hi)

    if kind == "checker":
        n = max(1, int(scale))
        t = np.floor(_grid(size) * n).astype(np.int32)
        f = ((t[:, None] + t[None, :]) % 2).astype(np.float32)
        return f

    if kind == "grid":
        n = max(1, int(scale))
        width = max(1e-4, float(p.get("line_width", 0.08)))
        t = _grid(size) * n
        f = np.minimum(np.abs(t - np.round(t)), 1.0)
        m = (f < width / 2.0).astype(np.float32)
        return 1.0 - m

    if kind == "stripes":
        n = max(1, int(scale))
        angle = math.radians(float(p.get("angle", 45.0)))
        x = _grid(size)[:, None]
        y = _grid(size)[None, :]
        v = x * math.cos(angle) + y * math.sin(angle)
        return ((np.floor(v * n).astype(np.int32) % 2)).astype(np.float32)

    if kind == "gradient":
        angle = math.radians(float(p.get("angle", 90.0)))
        x = _grid(size)[:, None]
        y = _grid(size)[None, :]
        v = x * math.cos(angle) + y * math.sin(angle)
        return _norm01(v, 0.0, 1.0)

    if kind == "radial":
        x = (_grid(size) - 0.5)[:, None] * 2.0
        y = (_grid(size) - 0.5)[None, :] * 2.0
        return _norm01(np.sqrt(x * x + y * y), 0.0, 1.0)

    if kind == "brushed_metal":
        rows = _fbm(size, 3, max(4, int(scale * 4)), 0.5, 2.2, rng)
        streak = rng.random((size, 1)).astype(np.float32) * 0.35
        f = rows * 0.65 + streak
        return _norm01(f, lo, hi)

    if kind == "rust":
        base = _fbm(size, int(p.get("octaves", 7)), max(2, int(scale)), 0.55, 2.1, rng)
        spots = _norm01(_voronoi(size, max(2, int(scale * 2)), rng), 0.0, 1.0)
        f = np.clip(base * 0.6 + spots * 0.6 - 0.15, 0.0, 1.0)
        f = np.clip((f - 0.5) * contrast + 0.5, 0.0, 1.0)
        return f

    if kind == "scratches":
        f = np.full((size, size), lo, dtype=np.float32)
        count = int(p.get("count", 220))
        for _ in range(count):
            x0 = rng.random() * size
            y0 = rng.random() * size
            length = rng.random() * size * float(p.get("max_length", 0.35)) + 4
            ang = rng.random() * math.tau
            depth = rng.random() * (hi - lo) + lo
            steps = int(length)
            for s in range(steps):
                px = int(x0 + math.cos(ang) * s) % size
                py = int(y0 + math.sin(ang) * s) % size
                w = 1 if rng.random() > 0.75 else 0
                f[max(py - w, 0):py + w + 1, max(px - w, 0):px + w + 1] = depth
        return f

    if kind == "grunge":
        a = _fbm(size, int(p.get("octaves", 8)), max(2, int(scale)), 0.6, 2.2, rng)
        b = _norm01(_voronoi(size, max(2, int(scale * 3)), rng), 0.0, 1.0)
        f = np.clip(a * 0.7 + b * 0.45 - 0.1, 0.0, 1.0)
        return np.clip((f - 0.5) * contrast + 0.5, 0.0, 1.0)

    if kind == "concrete":
        a = _fbm(size, int(p.get("octaves", 8)), max(2, int(scale)), 0.55, 2.0, rng)
        pits = _norm01(_voronoi(size, max(2, int(scale * 4)), rng), 0.0, 1.0)
        f = np.clip(0.55 + (a - 0.5) * 0.5 - (1.0 - pits) * 0.18, 0.0, 1.0)
        return _norm01(f, lo, hi)

    if kind == "asphalt":
        a = _fbm(size, int(p.get("octaves", 9)), max(4, int(scale * 3)), 0.6, 2.3, rng)
        grit = rng.random((size, size)).astype(np.float32)
        f = np.clip(a * 0.8 + grit * 0.2, 0.0, 1.0)
        return _norm01(f, lo, hi)

    if kind == "wood":
        x = _grid(size)[:, None]
        y = _grid(size)[None, :]
        warp = _fbm(size, 4, max(2, int(scale)), 0.5, 2.0, rng) * 0.35
        rings = np.sin((y * scale * math.pi * 2.0) + warp * 6.0) * 0.5 + 0.5
        fine = _fbm(size, 3, max(8, int(scale * 6)), 0.5, 2.0, rng) * 0.12
        return np.clip(rings * 0.85 + fine, 0.0, 1.0)

    if kind == "leather":
        cells = _norm01(_voronoi(size, max(6, int(scale * 8)), rng), 0.0, 1.0)
        grain = _fbm(size, 5, max(4, int(scale * 4)), 0.55, 2.1, rng)
        f = np.clip(cells * 0.75 + grain * 0.35, 0.0, 1.0)
        return np.clip((f - 0.5) * contrast + 0.5, 0.0, 1.0)

    if kind == "carbon":
        n = max(4, int(scale * 2))
        n = n + (n % 2)
        cell = size // n
        weave = np.zeros((size, size), dtype=np.float32)
        for gy in range(n):
            for gx in range(n):
                y0, x0 = gy * cell, gx * cell
                yy, xx = np.mgrid[0:cell, 0:cell]
                diag = ((xx + yy) % (cell // 2 * 2)) < cell // 2
                block = np.where(diag, 0.25, 0.75).astype(np.float32)
                weave[y0:y0 + cell, x0:x0 + cell] = block
        return weave

    if kind == "hazard":
        n = max(1, int(scale))
        stripe = np.floor((_grid(size)[:, None] + _grid(size)[None, :]) * n).astype(np.int32)
        return (stripe % 2).astype(np.float32)

    if kind == "plate":
        f = np.full((size, size), 0.0, dtype=np.float32)
        margin = float(p.get("margin", 0.12))
        f[int(margin * size):int((1 - margin) * size),
          int(margin * size):int((1 - margin) * size)] = 0.12
        f[int(margin * size) + 3:int((1 - margin) * size) - 3,
          int(margin * size) + 3:int((1 - margin) * size) - 3] = 0.18
        _draw_text(f, str(p.get("text", "0000AA000")), 0.5,
                   0.5, float(p.get("font_scale", size / 150.0)))
        return f

    f = _fbm(size, int(p.get("octaves", 6)), max(2, int(scale)), 0.5, 2.0, rng)
    return _norm01(f, lo, hi)


def height_to_normal(height: np.ndarray, strength: float = 2.0) -> np.ndarray:
    """Sobel the height field into a tangent-space normal map (RGB, 0..1)."""
    h = height.astype(np.float32)
    dx = np.zeros_like(h)
    dy = np.zeros_like(h)
    dx[:, 1:-1] = (h[:, 2:] - h[:, :-2]) * 0.5
    dy[1:-1, :] = (h[2:, :] - h[:-2, :]) * 0.5
    n = np.stack((-dx * strength, -dy * strength, np.ones_like(h)), axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True) + 1e-8
    return n * 0.5 + 0.5


# --------------------------------------------------------------------------- #
# image plumbing
# --------------------------------------------------------------------------- #
def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def field_to_image(name: str, field: np.ndarray, path: str, colorspace: str,
                   linear: bool) -> bpy.types.Image:
    size = field.shape[0]
    if field.ndim == 2:
        rgba = np.empty((size, size, 4), dtype=np.float32)
        rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = field
        rgba[..., 3] = 1.0
    else:
        rgba = np.empty((size, size, 4), dtype=np.float32)
        rgba[..., :3] = field[..., :3]
        rgba[..., 3] = 1.0
    if linear:
        rgba[..., :3] = _srgb_to_linear(np.clip(rgba[..., :3], 0.0, 1.0))
    else:
        rgba[..., :3] = np.clip(rgba[..., :3], 0.0, 1.0)
    img = bpy.data.images.get(name)
    if img is not None:
        bpy.data.images.remove(img)
    img = bpy.data.images.new(name, width=size, height=size, alpha=False)
    img.colorspace_settings.name = colorspace
    img.pixels.foreach_set(rgba.ravel())
    img.update()
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
    return img


def _tint(field: np.ndarray, color: str) -> np.ndarray:
    """Map a 0..1 mask onto an sRGB colour, keeping alpha."""
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    rgb = np.array([int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float32)
    out = np.empty(field.shape + (4,), dtype=np.float32)
    out[..., :3] = rgb[None, None, :] * field[..., None]
    out[..., 3] = 1.0
    return out


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def generate(params: dict) -> dict:
    """One texture, written to disk as a PNG."""
    pattern = str(params.get("pattern", "fbm"))
    size = int(params.get("size", DEFAULT_SIZE))
    if size < 8 or size > MAX_SIZE:
        raise ValueError(f"size must be between 8 and {MAX_SIZE}")
    seed = int(params.get("seed", 0))
    rng = np.random.default_rng(seed)
    out_path = params.get("output_path") or ""
    name = params.get("name") or f"TEX_{pattern}_{seed}"

    height = height_field(pattern, size, params, rng)
    contrast = float(params.get("contrast", 1.0))
    if contrast != 1.0 and pattern not in {"solid", "checker", "grid", "stripes",
                                           "hazard", "carbon", "plate", "radial"}:
        height = np.clip((height - 0.5) * contrast + 0.5, 0.0, 1.0)

    written = []
    if not out_path:
        out_dir = params.get("output_dir") or os.path.join(
            os.path.expanduser("~"), "BlenderMCP_Textures")
        out_path = os.path.join(out_dir, f"{name}.png")
    field_to_image(name, height, out_path, "Non-Color" if pattern in
                   {"height", "roughness", "metallic", "ao"} else "sRGB",
                   linear=False)
    written.append(out_path)

    result = {"pattern": pattern, "size": size, "seed": seed,
              "field_min": float(height.min()), "field_max": float(height.max()),
              "field_mean": round(float(height.mean()), 5),
              "files": written, "image": name}
    if params.get("also_normal"):
        npath = os.path.splitext(out_path)[0] + "_normal.png"
        field_to_image(name + "_Normal", height_to_normal(height,
                       float(params.get("normal_strength", 2.0))),
                       npath, "Non-Color", linear=False)
        result["files"].append(npath)
    return result


def generate_pbr_set(params: dict) -> dict:
    """A matched BaseColor / Roughness / Metallic / Normal / AO set from one seed.

    The five maps are driven by the same height field, which is what makes them
    read as one material instead of five unrelated pictures.
    """
    pattern = str(params.get("pattern", "fbm"))
    size = int(params.get("size", DEFAULT_SIZE))
    seed = int(params.get("seed", 0))
    rng = np.random.default_rng(seed)
    height = height_field(pattern, size, params, rng)
    contrast = float(params.get("contrast", 1.0))
    if contrast != 1.0:
        height = np.clip((height - 0.5) * contrast + 0.5, 0.0, 1.0)

    out_dir = params.get("output_dir") or os.path.join(
        os.path.expanduser("~"), "BlenderMCP_Textures")
    os.makedirs(out_dir, exist_ok=True)
    base_name = params.get("name") or f"{pattern}_{seed}"
    color = str(params.get("color", "#808080"))
    color2 = str(params.get("color2", "#ffffff"))
    metallic = float(params.get("metallic", 0.0))
    rough_lo = float(params.get("roughness_min", 0.25))
    rough_hi = float(params.get("roughness_max", 0.85))
    strength = float(params.get("normal_strength", 2.0))

    c = color.lstrip("#")
    c = "".join(ch * 2 for ch in c) if len(c) == 3 else c
    c2 = color2.lstrip("#")
    c2 = "".join(ch * 2 for ch in c2) if len(c2) == 3 else c2
    rgb_a = np.array([int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float32)
    rgb_b = np.array([int(c2[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], dtype=np.float32)

    albedo = np.empty(height.shape + (4,), dtype=np.float32)
    blend = height[..., None]
    albedo[..., :3] = rgb_a[None, None, :] * (1 - blend) + rgb_b[None, None, :] * blend
    albedo[..., 3] = 1.0

    rough = (rough_lo + (rough_hi - rough_lo) * height).astype(np.float32)
    metal = np.full(height.shape, metallic, dtype=np.float32)
    ao = np.clip(0.55 + 0.45 * height, 0.0, 1.0).astype(np.float32)

    files = {}
    files["base_color"] = os.path.join(out_dir, f"{base_name}_BaseColor.png")
    field_to_image(f"{base_name}_BaseColor", albedo, files["base_color"], "sRGB", False)
    files["roughness"] = os.path.join(out_dir, f"{base_name}_Roughness.png")
    field_to_image(f"{base_name}_Roughness", rough, files["roughness"], "Non-Color", False)
    files["metallic"] = os.path.join(out_dir, f"{base_name}_Metallic.png")
    field_to_image(f"{base_name}_Metallic", metal, files["metallic"], "Non-Color", False)
    files["normal"] = os.path.join(out_dir, f"{base_name}_Normal.png")
    field_to_image(f"{base_name}_Normal", height_to_normal(height, strength),
                   files["normal"], "Non-Color", False)
    files["ao"] = os.path.join(out_dir, f"{base_name}_AO.png")
    field_to_image(f"{base_name}_AO", ao, files["ao"], "Non-Color", False)

    out = {"pattern": pattern, "size": size, "seed": seed, "files": files,
           "note": "Normal and AO are Non-Color; BaseColor is sRGB."}

    if params.get("material"):
        assigned = apply_pbr_set(params["material"], files)
        out["material"] = assigned
    return out


def apply_pbr_set(material_name: str, files: dict) -> dict:
    """Wire a generated set into an existing Principled BSDF material."""
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        raise ValueError(f"no material named {material_name!r}")
    if not mat.use_nodes:
        mat.use_nodes = True
    tree = mat.node_tree
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        raise ValueError(f"material {material_name!r} has no Principled BSDF node")
    mapping = {
        "base_color": "Base Color",
        "roughness": "Roughness",
        "metallic": "Metallic",
        "normal": "Normal",
        "ao": None,          # AO needs a dedicated multiply, wired below
    }
    wired = {}
    for key, socket_name in mapping.items():
        path = files.get(key)
        if not path or socket_name is None:
            continue
        img = bpy.data.images.load(path, check_existing=True)
        tex = next((n for n in tree.nodes if n.type == "TEX_IMAGE"
                    and n.image == img), None)
        if tex is None:
            tex = tree.nodes.new("ShaderNodeTexImage")
            tex.image = img
            tex.location = (-700, 200 * list(mapping).index(key))
        tree.links.new(tex.outputs["Color"], bsdf.inputs[socket_name])
        wired[key] = socket_name

    ao_path = files.get("ao")
    if ao_path and "Base Color" in bsdf.inputs:
        ao_img = bpy.data.images.load(ao_path, check_existing=True)
        ao_tex = next((n for n in tree.nodes if n.type == "TEX_IMAGE"
                       and n.image == ao_img), None)
        if ao_tex is None:
            ao_tex = tree.nodes.new("ShaderNodeTexImage")
            ao_tex.image = ao_img
            ao_tex.location = (-700, -400)
        base_src = bsdf.inputs["Base Color"].links[0].from_socket if \
            bsdf.inputs["Base Color"].links else None
        mix = next((n for n in tree.nodes if n.type == "MIX"
                    and n.blend_type == "MULTIPLY"), None)
        if mix is None:
            mix = tree.nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "MULTIPLY"
            mix.location = (-380, 0)
        if base_src:
            tree.links.new(base_src, mix.inputs[1])
        tree.links.new(ao_tex.outputs["Color"], mix.inputs[2])
        tree.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
        wired["ao"] = "Base Color (multiply)"

    if "normal" in files:
        nrm = next((n for n in tree.nodes if n.name.startswith("NormalMap")), None)
        if nrm is None:
            nrm = tree.nodes.new("ShaderNodeNormalMap")
            nrm.location = (-380, -400)
        nrm.inputs["Strength"].default_value = 1.0
    return {"material": material_name, "wired": wired}


def bake(params: dict) -> dict:
    """Bake the active material's procedural nodes down to image files."""
    target = str(params.get("target", "DIFFUSE"))
    obj = bpy.data.objects.get(params.get("object", "")) or bpy.context.view_layer.objects.active
    if obj is None or obj.type != "MESH":
        raise ValueError("bake needs a mesh object (pass 'object' or select one)")
    mat = obj.data.materials[0] if obj.data.materials else None
    if mat is None:
        raise ValueError(f"{obj.name} has no material to bake")
    if not mat.use_nodes:
        raise ValueError(f"material {mat.name!r} is not node-based; nothing to bake")
    out_dir = params.get("output_dir") or os.path.join(
        os.path.expanduser("~"), "BlenderMCP_Textures")
    os.makedirs(out_dir, exist_ok=True)

    engine_items = {i.identifier for i in
                    bpy.types.CyclesBakeSettings.bl_rna.properties["target"].enum_items}
    target = target.upper()
    if target not in engine_items:
        raise ValueError(f"bake target must be one of {sorted(engine_items)}")

    scene = bpy.context.scene
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    images = {}
    node = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if node is None:
        raise ValueError("material has no output node")
    for suffix in ("_BaseColor", "_Normal"):
        img = bpy.data.images.get(f"BAKE_{obj.name}{suffix}")
        if img:
            bpy.data.images.remove(img)
        img = bpy.data.images.new(f"BAKE_{obj.name}{suffix}", width=int(params.get("size", 1024)),
                                  height=int(params.get("size", 1024)))
        img.filepath_raw = os.path.join(out_dir, f"{obj.name}{suffix}.png")
        img.file_format = "PNG"
        images[suffix] = img
    try:
        bpy.ops.object.bake(type=target, use_clear=True)
    except Exception as exc:  # noqa: BLE001
        return {"baked": False, "reason": f"{type(exc).__name__}: {exc}",
                "hint": "Cycles is the reliable bake engine; ensure the object has "
                        "UVs and a material, and that the scene engine is CYCLES."}
    saved = []
    for suffix, img in images.items():
        if img.has_data:
            img.save()
            saved.append(img.filepath_raw)
    del scene
    return {"baked": True, "target": target, "object": obj.name,
            "material": mat.name, "files": saved}


def pack_textures(params: dict) -> dict:
    """Pack every loose image into the .blend so the file is self-contained."""
    packed, failed = [], []
    for img in bpy.data.images:
        if img.source not in {"FILE", "GENERATED"}:
            continue
        if img.packed_file is not None and not params.get("repack"):
            continue
        try:
            img.pack()
            packed.append(img.name)
        except Exception as exc:  # noqa: BLE001
            failed.append({"image": img.name, "error": str(exc)})
    return {"packed": len(packed), "images": packed[:60], "failed": failed[:20]}


def list_images(params: dict) -> dict:
    rows = []
    for img in bpy.data.images:
        rows.append({
            "name": img.name,
            "size": list(img.size),
            "source": img.source,
            "colorspace": img.colorspace_settings.name,
            "packed": img.packed_file is not None,
            "filepath": img.filepath,
            "has_data": img.has_data,
        })
    return {"count": len(rows), "images": rows[:int(params.get("limit", 100))]}
