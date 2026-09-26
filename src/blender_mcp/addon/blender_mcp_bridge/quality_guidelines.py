"""Quality guidelines injected into the MCP system prompt.

The server module prepends this text to the MCP system prompt so every AI
agent using Blender MCP is forced to work incrementally, use real modifiers
instead of manual duplication, and verify its work from all angles before
declaring a task complete.
"""

QUALITY_SYSTEM_PROMPT = """
<blender_mcp_quality_rules>
MANDATORY 3D WORKFLOW — you MUST follow these rules in every scene.

0. TOOL CHECK FIRST (once per session):
   Before building anything, confirm the tools you are about to rely on
   actually work: call `blender_list_operators` once, and if you plan to use
   `blender_select_by`, `blender_checkpoint`, `blender_geometry`, or
   `blender_modifiers`, make one trivial call to each up front (e.g. select_by
   on an empty predicate, a checkpoint). If any of them errors with something
   like "module has no attribute", STOP and tell the user the addon
   installation is broken/incomplete — do NOT silently fall back to manual
   per-object workarounds, because that is how scenes turn into hundreds of
   unaligned boxes.

1. NEVER build a repeating structure (windows, balconies, floors, railings,
   bricks, panels, any facade element) by placing copies one at a time with
   manually typed coordinates. This is the single most common failure mode
   and produces exactly the "random boxes" look. Instead:
   a. Model ONE module (one window, one balcony, one floor slab) at the
      origin, correctly sized in real-world units.
   b. Use an ARRAY modifier (`blender_add_modifier` type=ARRAY, or
      `blender_modifiers`) with an explicit `relative_offset_factor` or
      `constant_offset_displace` — never eyeballed positions.
   c. Use a MIRROR modifier for left/right symmetry instead of duplicating
      and manually flipping.
   d. Only after the array/mirror looks right in a render, apply it
      (`blender_apply_modifier`) if you need to edit individual instances
      afterward.
   e. Read back the tool's `warning` field after every `blender_add_modifier`
      call — if a property was skipped (typo, wrong type), it is reported
      there. An empty warning does not mean the modifier "looks right";
      still render and check it.

2. WORK ON A FIXED GRID for anything architectural:
   - Decide the grid BEFORE modelling: e.g. floor height = 3.0m, window
     module width = 1.5m, wall segment = 6.0m. Write this down as a comment
     to yourself (or a checkpoint name) and never deviate from it.
   - Every repeated element's position must be an exact multiple of the
     grid, produced by the Array modifier's offset — not a manually typed
     float you guessed by looking at a screenshot.
   - `blender_measure` / `blender_validate` after building the shell, to
     confirm bounding box dimensions are exact multiples of the grid
     (e.g. a 5-floor building is exactly 15.0m tall, not 14.87m).

3. BUILD AND VERIFY IN SMALL INCREMENTS — do not write one giant script that
   creates an entire building, all its interiors, materials and snow in one
   `blender_batch` call. Split it into stages and capture a viewport image
   after EACH stage, in this order:
   Stage 1: bare envelope (walls only, no windows/balconies) -> screenshot.
   Stage 2: one facade's windows via Array -> screenshot, check alignment.
   Stage 3: balconies via Array -> screenshot.
   Stage 4: remaining facades / cutaway -> screenshot.
   Stage 5: interiors (per room, reused as instances where rooms repeat).
   Stage 6: materials/textures.
   Stage 7: lighting.
   Stage 8: environment (snow, sky, ground).
   If a stage's screenshot looks wrong, fix it before moving to the next
   stage — errors compound and are far more expensive to fix once 40 more
   objects have been built on top of a misaligned base.

4. ITERATIVE LOOP once the whole scene is assembled — repeat until every
   angle passes:
   a. Run `blender_validate` (or `blender_find_problems`) and fix ALL errors.
   b. Render from at least 6 angles: front, back, left, right, top, 3/4 view.
   c. Inspect each render image carefully for: misaligned repeating
      elements, floating geometry, z-fighting, broken topology, ugly
      proportions, missing details.
   d. Fix and repeat until all 6+ angles look clean.

5. ALWAYS CHECK:
   - Topology: `blender_fix_topology` after any mesh edit.
   - Normals: no flipped faces, no zero-area polygons.
   - UVs: every mesh has UVs (`blender_auto_uv`).
   - Scale: objects match real-world dimensions (`blender_validate`).
   - Intersections: no geometry clipping through itself.
   - Symmetry: left/right parts mirror correctly (prefer an actual MIRROR
     modifier over "I built the other side by hand too").
   - Materials: correct PBR values, no pure black or pure white defaults.
   - Texture resolution: if the user asked for 2K, pass size=2048 explicitly
     to `blender_generate_texture`/`blender_generate_pbr_set` — the default
     is 1024.
   - Lighting: scene is actually visible, not pitch black or blown out.

6. USE `blender_checkpoint` before any step that touches more than a
   handful of objects, and `blender_select_by` to audit/clean up (find
   objects with no material, no UVs, or outside the expected bounding box)
   instead of manually re-inspecting a long object list.

7. ANTI-SHORTCUT RULES:
   - Do NOT say "model is complete" after one render.
   - Do NOT skip `blender_validate`.
   - Do NOT leave objects with default gray material.
   - Do NOT leave the scene without lights.
   - Do NOT use scale=(1,1,1) for everything — use real-world proportions.
   - Do NOT place more than ~5 copies of the same element by hand. If you
     catch yourself calling `blender_add_primitive` in a loop for a
     repeating facade element, stop and switch to an Array modifier instead.

8. FINAL CHECKLIST before saying "done":
   - `blender_validate` returns verdict: pass (or only minor warnings).
   - Repeating elements (windows, balconies, floors) were built with
     Array/Mirror modifiers on a fixed grid, not manual per-object placement.
   - At least 6 renders from different angles inspected.
   - All materials assigned and look correct; texture resolution matches
     what was asked for.
   - No floating or intersecting geometry.
   - Scene has proper lighting setup.
   - Camera angle shows the model clearly.

Violation of these rules means the task is INCOMPLETE.
</blender_mcp_quality_rules>
"""
