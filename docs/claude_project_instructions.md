# Fusion CAD — Claude Desktop project instructions
#
# Paste everything below the line into the custom instructions of a Claude
# Desktop Project, and do your Fusion modeling chats inside that project.
# ---------------------------------------------------------------------------

You can drive Fusion 360 through MCP tools (server name: fusion360). Follow
these conventions.

UNITS
- All tool inputs are in MILLIMETERS. Tools convert to Fusion's internal cm.
- In raw code via run_fusion_code / run_fusion_code_safe, DO NOT hand-divide by
  10. Use the injected helper mm(x) for every length you pass to the API. This
  is the #1 source of 10x-scale bugs. Call assert_scale() after building to
  catch any slip immediately.

OBSERVABILITY (use the injected helpers in raw code — no import needed)
- mm(x)/cm(x) for inputs; to_mm/to_mm2/to_mm3 for reporting.
- For ANY cut or join: capture _b = total_volume() before, then call
  report_volume_delta(_b, 'label') after. A delta of ~0 means the boolean did
  nothing — fix it before moving on, don't stack more operations.
- Use sliver_face_count(body) after booleans to catch bad geometry.
- Use log_sketch_areas(sketch) right after sketching to catch scale slips early.
- The built-in cut tools (drill_hole, hole_on_face, tapped_hole, shell_body)
  already auto-report volume delta and warn on no-ops — read those warnings.

STATE & DIAGNOSIS (prefer these over hand-rolled introspection)
- get_state() is cheap and ALWAYS responsive (served off the main thread): use
  it first for crash recovery and to confirm the active document/lineage.
- get_model_state() gives per-body detail AND leads with the active document
  name + lineage id. Check that identity before any clear/save.
- cross_section(...) prints an ASCII solid/void scan + boundary coordinates:
  the fastest way to verify wall thickness, trapped voids, or a missed cut.
- list_faces()/list_edges() return stable tempIds for re-selecting entities.

LONG OPERATIONS & HANGS
- For anything likely to exceed ~90s, use run_async(code) -> returns a job_id;
  poll job_status(job_id) for incremental progress and the final envelope. Emit
  progress(msg) in your code to mark stages.
- If run_fusion_code returns "STILL RUNNING" or a timeout envelope, the job is
  STILL EXECUTING - poll job_status(job_id). Do NOT re-issue the same call; that
  stacks another job behind the stuck one.
- ping_fusion()/heartbeat() stay responsive during a running op (separate
  channel). busy=True means a job is running; an error means the listener is
  down (restart the add-in).

REPLACING A BODY / STARTING OVER
- Do NOT reach for clear_document to start over. The safe idiom is: build the
  new body, then retire the old one with retire_body(old) (removeFeatures).
  It keeps history and cannot destroy unrelated work.
- clear_document is now reverse-order timeline deletion and runs in the
  background by default (returns a job_id - poll job_status). The old bulk
  approach could hang for 10+ minutes and take Fusion down with it.

DOCUMENT SAFETY (prevents overwriting the wrong design)
- Before clear_document or save_document, check get_state()/get_model_state()
  for the ACTIVE document name + lineage. open_document changes which document
  is active.
- For a NEW part, save with create_new=True so it gets its own lineage and never
  versions an unrelated design. save_document reports which lineage it wrote -
  read it back and confirm it matches intent.
- list_versions() shows what is recoverable if a save went wrong; guide the user
  to the specific version to promote in the Data Panel.

START OF SESSION
- If any tool reports a connection error, call ping_fusion and tell the user to
  open Fusion and start the MCPListener add-in if it fails.

WORKFLOW PER PART
1. Start from a known state: clear_document for a fresh single part, or build
   additively into named components for assemblies.
2. Build parametric-first. For any dimension the user might tweak, create a
   user parameter with set_parameter and reference it rather than hardcoding.
3. After any non-trivial operation, call view_model to SEE the result. Don't
   trust a success message alone. Check more than one orientation (iso, then
   top/front) when placement or alignment matters.
4. Use get_model_state and measure_distance to confirm dimensions match intent.

SAFETY
- For multi-step or risky operations, prefer run_fusion_code_safe so a partial
  failure rolls back instead of leaving orphaned geometry.
- If an operation errors, read the traceback, fix the code, and retry — but
  clear or roll back first so retries don't stack on half-finished geometry.

FACES & TAPPED HOLES
- To work on a side/top face, call sketch_on_face FIRST, then hole_on_face
  immediately after (the face selection is single-slot — don't interleave).
- For tapped holes the user will physically tap, use tap_drill_only=True so the
  hole is sized for a real tap. Reserve modeled threads for CNC/visualization.

ASSEMBLIES
- One component per distinct part. Repeated identical hardware = one component
  with multiple occurrences via copy_component (edit once, all update).
- Ground exactly one base component first, position other parts, THEN joint.
- Joints win over transforms: don't move a part after jointing it.

PERSISTENCE
- For ongoing projects, save_document with a clear name early, then save new
  versions as you iterate. Use list_recent_designs + open_document to resume
  prior work instead of rebuilding. Confirm the intended Fusion project is
  active before a named save.

CURVED SURFACES & MANY-CUT PATTERNS
- For a pattern/text/texture that must CONFORM to a curved or revolved face,
  use wrap_cut. Do NOT hand-roll it: closed splines on curved faces,
  createRuledSurface, loft between non-planar closed splines and
  BRepBodyDefinition are all unreliable here.
- OMIT face_tempids so wrap_cut auto-targets the largest curved face. tempIds
  die when an operation splits faces; do not cache them across calls.
- wrap_cut reports ok/no-op/failed per profile. READ THOSE COUNTS. A no-op
  profile almost always fell outside the face's angular sector - one flat sketch
  wraps onto only ~70-100 degrees. Split big patterns into sectors at 0/90/180/
  270 deg using construction_plane_tangent, and wrap each sector separately.
- Before placing geometry on any offset or tangent plane, run sketch_axis_map -
  local axes rarely match world, and guessing costs a rebuild.
- Use construction_plane_offset / construction_plane_tangent instead of raw
  ConstructionPlaneInput.setByPlane and ConstructionPoints.add, both of which
  raise "Environment is not supported" on this build.
- For CELL/LATTICE patterns (many prism cuts), prefer cut_prisms over wrap_cut:
  it uses TemporaryBRepManager, needs no construction planes and no face
  tempIds, and lands hundreds of cuts as ONE timeline feature. In raw code the
  same path is available via the injected tbm() and commit_tool_body() helpers -
  build throwaway solids in memory, boolean them, commit one result.

WHEN TO USE RAW CODE
- The canned tools cover boxes, cylinders, holes, fillets, shells, tapped holes,
  components, and joints on principal planes. For angled faces, lofts, sweeps,
  organic surfaces, or features referenced to specific existing faces/edges,
  write API code via run_fusion_code_safe and verify with view_model.
- NEVER use move-face via defineAsFreeMove (it hard-crashes the host) or
  defineAsOffset (missing on this build). The listener refuses both. Use
  parametric feature edits, offsetFeatures, or a rebuild instead.

STYLE
- State the key dimensions and parameter names you'll use before building, so
  the user can correct you before geometry exists.
- Keep the user informed which body/component you're operating on.
