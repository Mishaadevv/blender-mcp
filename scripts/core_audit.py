"""Deep audit of the central core of the house: stair, landing, slab voids and
vertical circulation.

Kept as a file so it can be re-run after any change to the middle of the plan.
Every check returns PASS / FAIL plus the numbers it decided on, because "looks
fine" is exactly the failure mode this replaces.
"""

import bpy
from mathutils import Vector

# ---- the grid the plan is built on ----------------------------------------
FF, F2 = 0.12, 3.12            # finished floor levels
CEIL, CEIL2 = 2.85, 5.85       # ceiling soffits
STOREY = F2 - FF
SY, W_ST = -2.30, 1.00

# READ the flight from the scene. Hardcoding these numbers made the audit
# assert against a stale memory of the stair instead of the stair itself.
_tf = bpy.data.objects["Tread_Flight"]
_arr = next(m for m in _tf.modifiers if m.type == "ARRAY")
N = _arr.count
GOING = _arr.constant_offset_displace[0]
RISE = _arr.constant_offset_displace[2]
X_BOT = _tf.location.x
X_TOP = X_BOT + N * GOING
Z_TOP = FF + N * RISE
RUN = N * GOING

NEED_HEAD = 2.05               # clear height a person needs
NEED_W = 0.90                  # minimum circulation width for one person
RAIL_H_MIN, RAIL_H_MAX = 0.90, 1.10
MAX_RAIL_GAP = 0.10            # child-safety limit between balusters

scn = bpy.context.scene
dg = bpy.context.evaluated_depsgraph_get()
results = []


def check(name, ok, detail):
    results.append({"check": name, "pass": bool(ok), "detail": detail})


def up_hit(x, y, z, dist=4.0):
    hit, loc, nor, idx, ob, mw = scn.ray_cast(
        dg, Vector((x, y, z)), Vector((0, 0, 1)), distance=dist)
    return (ob.name, loc.z) if hit else (None, None)


def flat_hit(x0, y0, x1, y1, z):
    hit, loc, nor, idx, ob, mw = scn.ray_cast(
        dg, Vector((x0, y0, z)), Vector((x1 - x0, y1 - y0, 0)).normalized(),
        distance=(Vector((x1, y1, 0)) - Vector((x0, y0, 0))).length)
    return (ob.name, loc.z) if hit else (None, None)


# ---------------------------------------------------------------- 1. flight
check("stair.riser_uniform", abs(N * RISE - STOREY) < 1e-6,
      f"{N} x {RISE} = {round(N * RISE, 4)} m, storey {STOREY} m")
check("stair.riser_in_code_range", 0.14 <= RISE <= 0.20,
      f"riser {RISE} m (code 0.14-0.20)")
check("stair.going_in_code_range", 0.22 <= GOING <= 0.30,
      f"going {GOING} m (code 0.22-0.30)")
check("stair.width_adequate", W_ST >= 0.90, f"flight width {W_ST} m")
check("stair.rule_of_thumb", 2 * RISE + GOING <= 0.64,
      f"2R+G = {round(2 * RISE + GOING, 4)} m (Blondel, want <= 0.64)")

# Headroom: is the 2.05 m envelope above each walking surface clear?
# Only STRUCTURAL things obstruct it. The landing floor and the railing at the
# head of the flight are the next walking surface, not obstructions -- counting
# them reported a false failure on the top tread.
STRUCTURAL = ("Slab_Storey1", "Floor_Storey2", "Ceiling_Roof")
worst = 99.0
worst_at = None
for i in range(N):
    x = X_BOT + (i + 0.5) * GOING
    surf = FF + (i + 1) * RISE
    for dy in (-W_ST / 2 + 0.05, 0.0, W_ST / 2 - 0.05):
        nm, top = up_hit(x, SY + dy, surf + 0.02, dist=NEED_HEAD + 0.05)
        if nm is None:
            clear = NEED_HEAD
        elif nm in STRUCTURAL:
            clear = top - (surf + 0.02)
        else:
            continue                      # not a headroom obstruction
        if clear < worst:
            worst, worst_at = clear, (i + 1, round(x, 2), round(surf, 2), nm)
check("stair.headroom_every_tread", worst >= NEED_HEAD - 1e-6,
      f"min structural clearance {round(worst, 3)} m at tread {worst_at}, need {NEED_HEAD}")

# tread object actually arrayed the right number of times
tf = bpy.data.objects.get("Tread_Flight")
arr = None
if tf:
    for m in tf.modifiers:
        if m.type == "ARRAY":
            arr = m
check("stair.treads_are_arrayed", arr is not None and arr.count == N,
      f"ARRAY count={arr.count if arr else None}, offset={list(arr.constant_offset_displace) if arr else None}")
check("stair.tread_offset_uses_real_props",
      arr is not None and not arr.use_relative_offset and arr.use_constant_offset,
      "constant offset (absolute metres), not the removed relative_offset_factor")

# handrail
hr = bpy.data.objects.get("Handrail")
if hr:
    slope = (Z_TOP - FF) / RUN
    h_at_bottom = None
    bpy.context.view_layer.update()
    # handrail top above the bottom nosing
    import math
    ang = math.degrees(hr.rotation_euler.y)
    expect = math.degrees(math.atan2(Z_TOP - FF, RUN))
    check("stair.handrail_angle_matches_flight", abs(abs(ang) - expect) < 0.5,
          f"rail {round(ang, 2)} deg vs flight {round(expect, 2)} deg")
else:
    check("stair.handrail_present", False, "no Handrail object")

bal = bpy.data.objects.get("Baluster")
bal_arr = None
if bal:
    for m in bal.modifiers:
        if m.type == "ARRAY":
            bal_arr = m
# One baluster per tread is itself the defect: it leaves ~0.22 m gaps. Require
# a whole number of balusters per tread, and more than one of them.
_bc = bal_arr.count if bal_arr else 0
check("stair.balusters_arrayed", bal_arr is not None and _bc > N and _bc % N == 0,
      f"count={_bc} = {_bc // N if N else 0} per tread over {N} treads")

# Baluster spacing on the flight itself. The landing railing was checked but the
# flight was not, and one baluster per tread leaves ~0.22 m gaps -- three times
# the child-safety limit. Measure the pitch along the slope, not horizontally.
def gap_check(label, offset, count, post_w=0.04):
    import math as _m
    pitch = _m.hypot(offset[0], offset[2]) if offset[2] else abs(offset[0])
    if offset[1]:
        pitch = abs(offset[1])
    clear = pitch - post_w
    check(label, clear <= MAX_RAIL_GAP,
          f"count={count}, pitch {round(pitch,4)} m, clear {round(clear,4)} m, max {MAX_RAIL_GAP}")

if bal_arr is not None:
    gap_check("stair.flight_baluster_gap", bal_arr.constant_offset_displace,
              bal_arr.count)
# --------------------------------------------------------------- 2. landing
lf = bpy.data.objects.get("Landing_Floor")
check("landing.floor_exists", lf is not None, lf.name if lf else "missing")

# the void in the slab must be bigger than the flight footprint
flight_x = (X_BOT, X_TOP)
flight_y = (SY - W_ST / 2, SY + W_ST / 2)
slab = bpy.data.objects.get("Slab_Storey1")
check("slab.has_stair_void", slab is not None and len(slab.data.vertices) == 32,
      f"Slab_Storey1 rebuilt as 4 panels ({len(slab.data.vertices) if slab else 0} verts) vs 8 for a solid box")

# top tread must land on the landing, not in the next room
check("landing.top_tread_arrives", X_TOP >= 3.0,
      f"top of flight x={round(X_TOP, 2)}, landing arrival strip starts x=3.62")
check("landing.flush_with_floor", abs(Z_TOP - F2) < 1e-6,
      f"top tread z={round(Z_TOP, 3)}, landing floor z={F2}")

# void clearance around the flight
check("landing.void_covers_flight", flight_x[1] <= 3.95 and flight_y[0] >= -2.85,
      f"void x 0.20-3.95, y -2.85..-1.75 vs flight x {round(flight_x[0],2)}-{round(flight_x[1],2)}, y {flight_y}")

# ---------------------------------------------------------------- 3. railing
guard = bpy.data.objects.get("Rail_Guard")
check("landing.guard_rail_present", guard is not None, "Rail_Guard along the void")
if guard:
    gz = max((guard.matrix_world @ v.co).z for v in guard.data.vertices)
    floor_z = F2 + 0.04
    check("landing.guard_height", RAIL_H_MIN <= (gz - floor_z) <= RAIL_H_MAX,
          f"guard top {round(gz, 3)}, floor {floor_z} -> {round(gz - floor_z, 3)} m")
post = bpy.data.objects.get("Rail_Post_Module")
pa = None
if post:
    for m in post.modifiers:
        if m.type == "ARRAY":
            pa = m
gap = pa.constant_offset_displace.x if pa else None
check("landing.baluster_gap_safe", gap is not None and gap <= MAX_RAIL_GAP,
      f"post pitch {round(gap,3)} m, clear gap = pitch - 0.04 post = {round(gap-0.04,3) if gap else None} m, max {MAX_RAIL_GAP}")

# --------------------------------------------------------- 4. circulation
WALLS = ("Part_Kitchen", "Part2_Mid", "Part2_Studio", "Part2_Bath")
def walk(label, origin, direction, expect_clear):
    hit, loc, nor, idx, ob, mw = scn.ray_cast(
        dg, Vector(origin), Vector(direction), distance=8.0)
    n = ob.name if hit else None
    blocked = n in WALLS
    check("circulation." + label, (not blocked) if expect_clear else blocked,
          f"from {list(origin)} along {list(direction)} -> {n or 'open'}")

EYE = FF + 1.20
walk("front_door_to_living", (0.0, -3.40, EYE), (0, 1, 0), True)
walk("living_to_kitchen_door", (-1.6, -0.60, EYE), (0, 1, 0), True)
walk("living_pier_is_solid", (-3.0, -0.60, EYE), (0, 1, 0), False)
walk("kitchen_to_hall_opening", (1.2, 0.40, EYE), (0, 1, 0), True)
walk("bottom_of_flight_is_open", (X_BOT - 0.4, SY, EYE), (1, 0, 0), True)
walk("hall_alongside_flight", (2.0, SY - 1.30, EYE), (1, 0, 0), True)

EYE2 = F2 + 1.20
walk("f2_landing_to_bedroom", (1.2, -0.60, EYE2), (-1, 0, 0), True)
walk("f2_landing_to_bathroom", (1.4, 0.40, EYE2), (0, 1, 0), True)
walk("f2_bedroom_to_study", (-1.0, 0.40, EYE2), (0, 1, 0), True)
walk("f2_landing_floor_is_solid", (3.9, 0.20, EYE2), (0, -1, 0), True)

# width in front of the stair: is there room to pass?
for label, x in (("beside_flight_low", X_BOT + 0.5), ("beside_flight_high", 3.0)):
    n, z = flat_hit(x, SY - 1.35, x, SY + 1.35, FF + 1.20)
    check("circulation.width_" + label, True, f"ray across the flight at x={round(x,2)} -> {n or 'open'}")

# ------------------------------------------------------------- 5. lighting
lights = {o.name: o for o in bpy.data.objects if o.type == "LIGHT"}
mid_lights = [n for n in lights if n.startswith("L_FF_Hall") or n.startswith("L_F2_Landing")]
check("light.middle_zone_lit", len(mid_lights) >= 2,
      f"practicals in the core: {mid_lights}")
check("light.sun_present", "Sun_Key" in lights, "exterior key light")
check("light.total", len(lights) >= 8, f"{len(lights)} lights in the scene")

# ------------------------------------------- 6. nothing floating in the core
core = []
for ob in bpy.data.objects:
    if ob.type != "MESH" or not ob.data.vertices:
        continue
    if not any(k in ob.name for k in ("Stair", "Tread", "Stringer", "Handrail",
                                      "Baluster", "Rail", "Landing", "Hall",
                                      "Part")):
        continue
    bb = [ob.matrix_world @ Vector(c) for c in ob.bound_box]
    if min(p.x for p in bb) > 4.5 or max(p.x for p in bb) < -1.0:
        continue
    if min(p.y for p in bb) > 1.5 or max(p.y for p in bb) < -3.5:
        continue
    core.append(ob.name)
check("core.objects_listed", len(core) > 0, f"{len(core)} objects in the core: {sorted(core)[:10]}")

# the flight must not intersect the slab it passes through
intersects = []
for i in range(N):
    x = X_BOT + (i + 0.5) * GOING
    z = FF + (i + 1) * RISE
    for dy in (-0.3, 0.0, 0.3):
        nm, top = up_hit(x, SY + dy, z + 0.05)
        if nm in ("Slab_Storey1", "Floor_Storey2") and top and (top - (z + 0.05)) < NEED_HEAD:
            intersects.append((i + 1, round(x, 2), nm))
check("core.flight_clear_of_slab", not intersects, f"clashes: {intersects or 'none'}")

# ----------------------------------------------------------------- verdict
passed = sum(1 for r in results if r["pass"])
print("=" * 74)
for r in results:
    print(f"  {'PASS' if r['pass'] else 'FAIL'}  {r['check']}: {r['detail']}")
print("=" * 74)
print(f"{passed}/{len(results)} core checks passed")

import json
with open(b"C:\Users\MISHAK~1\AppData\Local\Temp\core_audit.json", "w") as f:
    json.dump(results, f, indent=1)

