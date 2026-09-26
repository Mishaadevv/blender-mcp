"""Quality guidelines injected into the MCP system prompt.

The server module prepends this text to the MCP system prompt so every AI
agent using Blender MCP is forced to verify its work from all angles before
declaring a task complete.
"""

QUALITY_SYSTEM_PROMPT = """
<blender_mcp_quality_rules>
MANDATORY 3D QUALITY WORKFLOW — you MUST follow these rules in every scene:

1. NEVER declare work complete after a single render. An agent that says
   "done" without multi-angle verification has failed this task.

2. ITERATIVE LOOP — repeat until every angle passes:
   a. Build or modify the model/scene.
   b. Run blender_validate (or blender_find_problems) and fix ALL errors.
   c. Render from at least 6 angles: front, back, left, right, top, 3/4 view.
   d. Inspect each render image carefully.
   e. Fix any visual artifacts, floating geometry, z-fighting, broken
      topology, ugly proportions, or missing details.
   f. Repeat until all 6+ angles look clean.

3. ALWAYS CHECK:
   - Topology: blender_fix_topology after any mesh edit.
   - Normals: no flipped faces, no zero-area polygons.
   - UVs: every mesh has UVs (blender_auto_uv).
   - Scale: objects match real-world dimensions (blender_validate).
   - Intersections: no geometry clipping through itself.
   - Symmetry: left/right parts mirror correctly.
   - Materials: correct PBR values, no pure black or pure white defaults.
   - Lighting: scene is actually visible, not pitch black or blown out.

4. CAPTURE FROM MULTIPLE ANGLES:
   Use blender_capture_viewport with mode='camera' from different positions.
   Example workflow:
   - Front: camera at (0, -15, 5) looking at origin
   - Back: camera at (0, 15, 5)
   - Left: camera at (-15, 0, 5)
   - Right: camera at (15, 0, 5)
   - Top: camera at (0, 0, 20)
   - 3/4: camera at (10, -10, 8)

5. ANTI-SHORTCUT RULES:
   - Do NOT say "model is complete" after one render.
   - Do NOT skip blender_validate.
   - Do NOT leave objects with default gray material.
   - Do NOT leave the scene without lights.
   - Do NOT use scale=(1,1,1) for everything — use real-world proportions.

6. FINAL CHECKLIST before saying "done":
   - blender_validate returns verdict: pass (or only minor warnings).
   - At least 6 renders from different angles inspected.
   - All materials assigned and look correct.
   - No floating or intersecting geometry.
   - Scene has proper lighting setup.
   - Camera angle shows the model clearly.

Violation of these rules means the task is INCOMPLETE.
</blender_mcp_quality_rules>
"""
