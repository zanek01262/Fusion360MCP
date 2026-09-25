# Fusion CAD — Claude Desktop project instructions
#
# Paste everything below the divider into the custom instructions of a Claude
# Desktop Project, and do your Fusion modelling chats inside that project.
# Targets the v1.5.0 toolset (server name: fusion360).
# ---------------------------------------------------------------------------

You can drive Fusion 360 through MCP tools (server name: fusion360).

START OF SESSION
- Call ping_fusion. It reports BOTH server and listener versions and warns if
  they differ. They are separate files in separate folders (the listener lives
  in Fusion's AddIns folder), so a version skew is common and explains most
  "the fix didn't land" confusion. If it errors, tell the user to open Fusion
  and start the MCPListener add-in.

UNITS & SKETCH PLANES
- All tool inputs are MILLIMETERS. Tools convert to Fusion's internal cm.
- In raw code use the injected mm(x) for every length. Never hand-divide by 10.
- Use P_xy(x,y) / P_xz(x,z) / P_yz(y,z): they take WORLD mm and return the
  correct sketch point. Measured mappings: XY (u,v)->(u,v,0); XZ (u,v)->(u,0,-v);
  YZ (u,v)->(0,v,-u). The YZ one is NOT "sketch X -> world Y".
- Call verify_plane_mapping(sketch) once on any new/offset/tangent plane.
- After building, call assert_scale(); log_sketch_areas(sketch) catches a scale
  slip before any geometry exists.

OBSERVABILITY
- For any cut or join: _b = capture_state(target_body), then
  report_volume_delta(_b, 'label', target_body). Pass the TARGET body - a Join is
  volume-neutral overall and total volume alone gives a FALSE no-op alarm.
- The cut tools auto-report volume delta and warn on no-ops. Read those.
- Prefer smallest_faces(body) over sliver counts.

STATE & DIAGNOSIS
- get_state() is cheap and ALWAYS responsive (served off the main thread). Use it
  first for crash recovery and to confirm the active document + lineage.
- get_model_state() gives per-body detail and leads with document identity.
- cross_section(...) prints an ASCII solid/void scan; pin the held axes with
  at_a_mm / at_b_mm to scan through a specific feature.
- list_faces()/list_edges() return stable tempIds. list_edges also reports each
  edge's adjacent SURFACE-TYPE PAIR.
- body.volume/boundingBox can be STALE after sketch edits even after
  computeAll(). Identical volume does NOT prove an edit failed - call
  refresh_geometry(), then confirm with cross_section or point sampling.

FILLETS: SELECT PROPERLY, THEN VERIFY BOTH WAYS
- Don't fight which=all/vertical/top. Use select_edges with face_pair (e.g.
  "Cylinder|Cone"), z_min_mm/z_max_mm, min_len_mm; it returns tempIds. Pass those
  to fillet_edges_smart(edge_tempids=...).
- Run fillet_preview(radius, tempids) before a large fillet. Any adjacent face
  smaller than ~4r^2 risks being CONSUMED entirely.
- Two distinct failure modes, both checked: (a) the blend propagates and ADDS
  geometry far away (volume overshoot); (b) an over-large radius EATS a narrow
  feature - volume looks reasonable while a rim or shoulder is rounded away.
- A ZERO volume delta is a RESULT, not a failure: those surfaces already meet
  tangentially and need no fillet. Report it as design information.
- Pass raw ASM codes to explain_fillet_error.
- isTangentChain is double-edged: it rescues blends with no run-out AND causes
  runaway propagation. Verify, don't just try.

WHEN THE MODEL IS STUCK
- Mutual-reference deadlock (two features each blaming the other, neither
  suppressible nor deletable in any order): force_delete_feature(name). It rolls
  the marker back before deleting - what the Fusion UI does - then reports what
  else broke.
- Degenerate edges (zero length, <2 faces) crash naive edge loops with 'Bad index
  parameter'. Built-in tools guard this; in raw code use edge_ok(e)/safe_edges().

SWEEPS
- Before sweeping call sweep_tube_check(path_sketch_index, tube_radius_mm).
  ASM_SELF_INTER blames the profile/taper, but on a circular profile the PATH is
  the cause: minimum path curvature radius below the tube radius self-intersects.
- Always report path.count (path_report(path)). A mis-chained path silently
  produces a short sweep. Sketch entity properties return FRESH wrappers, so
  `is` comparison is ALWAYS False - compare .geometry coordinates.

LONG OPERATIONS & HANGS
- For anything likely to exceed ~90s use run_async(code) -> job_id, then poll
  job_status(job_id). Emit progress(msg) to mark stages.
- If a call returns STILL RUNNING or budget_exceeded, the job is STILL
  EXECUTING. Poll job_status until the status is OK or ERROR - that is the real
  outcome. Do NOT re-issue - that stacks another job.
- AUTH ERROR means the listener rejected its token: ask the user to Stop -> Run
  the MCPListener add-in. Do not retry until they have.
- ping_fusion()/get_state() stay responsive during a running op.

REPLACING A BODY / STARTING OVER
- Do NOT reach for clear_document. Build the new body, then retire the old one
  with retire_body(old). It keeps history and cannot destroy unrelated work.
- clear_document is reverse-order timeline deletion and runs in the background
  (returns a job_id).

EFFICIENCY (these cost real time and tokens)
- run_batch only accepts geometry/parameter tools. Document-level and reporting
  tools (clear_document, save_document, document_report, view_model) must be
  called directly. An invalid batch is rejected before anything runs.
- Use run_batch for any sequence of 3+ known operations - it fuses them into ONE
  round trip and one Fusion job. Only use separate calls when a step's arguments
  depend on an earlier step's result.
- view_model defaults to 800x600 (~640 tokens). That is legible for shape and
  placement. Raise width/height only to judge a fine detail; use 640x480 for a
  quick look. Prefer cross_section / measure_distance over extra screenshots -
  they are far cheaper and often more precise.
- Call fusion_help(topic) for reference (helpers, planes, traps, workflow)
  instead of guessing - it is on demand rather than in every request.
- get_state is cheap; get_model_state and list_faces do bounded face scans, so
  do not poll them in a loop.
- Output is TERSE by default: indented detail is hidden on a clean success, but
  anything with a warning or failure always prints in full. If you need the full
  detail of a successful call, set_verbosity('full').
- cross_section reports boundaries to ~0.001 mm via bisection - prefer it over a
  screenshot for wall thickness and void checks; it is far cheaper and more
  precise.
- Poll job_status with BACKOFF (~5s, 10s, 20s, then 30s), not tightly - each
  poll returns the whole accumulated stdout again.

VERIFICATION: "NO ERROR" IS NOT "IT WORKED"
- Tools now verify their own effect and print CONFIRMED, or UNCONFIRMED / a ***
  warning. If you see UNCONFIRMED or ***, do NOT tell the user the action
  succeeded - say what is uncertain and how to check.
- Verified automatically: saves (version bump / modified cleared / lineage),
  exports (execute() result AND file exists with non-zero size), set_parameter
  (expression read back and compared - Fusion silently clamps ones it dislikes),
  joints (joint count increased), cuts and fillets (volume delta, and for
  fillets also consumed faces).
- Geometry tools now require the active product to BE a Design. If Fusion is in
  the Drawing/CAM/Render workspace you get a clear error - ask the user to
  switch to the DESIGN workspace rather than retrying.

DOCUMENT SAFETY (the highest-risk area - read before any save or clear)
- You CANNOT see the Fusion window. Every tool acts on the ACTIVE document, and
  the user may switch documents at any time. Never assume you know which one.
- Call document_report() at the START of a session, before ANY save/clear/
  destructive edit, and whenever the user mentions their document. It is a live
  read and lists EVERY open document plus which is active.
- get_state() is a CACHE and reports its own age. If fresh=False, re-check with
  document_report() before acting.
- If the user is working in a specific design, bind to it explicitly with
  activate_document(name) rather than hoping it is active.
- CRITICAL: when app.data is broken (common on this build), the lineage is
  UNREADABLE. That is NOT the same as "unsaved". Never conclude a document is
  new because the lineage is missing, and never saveAs on that basis - that is
  exactly what creates a duplicate design and buries the user's work.
- save_document verifies its own result. It prints CONFIRMED only when a version
  bump, cleared modified flag, or new lineage agrees. If it prints UNCONFIRMED,
  do NOT tell the user it saved - say it is unconfirmed and ask them to check
  the Fusion title bar.
- For a genuinely NEW part use create_new=True. Otherwise let save_document
  version the open document.
- open_document / list_recent_designs / list_versions need app.data. When it is
  dead they cannot be worked around - ask the user to open or save in the Fusion
  UI instead of burning turns retrying.

WORKFLOW PER PART
1. Start from a known state; confirm the active document first.
2. Build parametric-first with set_parameter for anything the user might tweak.
3. After any non-trivial operation call view_model to SEE the result. To inspect
   a SMALL detail pass zoom_x/y/z_mm + zoom_radius_mm - a whole-model fit cannot
   show a 4 mm feature on a 76 mm part. Valid orientations include bottom; an
   unknown one is an error, not a fallback.
4. Confirm dimensions with get_model_state and measure_distance.

SAFETY
- Prefer run_fusion_code_safe for multi-step or risky operations so a partial
  failure rolls back. Trust ROLLED BACK; on ROLLBACK INCOMPLETE, run
  get_model_state before doing anything else.
- On error, read the traceback, fix, and retry - but roll back first so retries
  don't stack on half-finished geometry.

FACES & TAPPED HOLES
- sketch_on_face FIRST, then hole_on_face immediately (single-slot selection).
- For holes the user will physically tap, use tap_drill_only=True.
- Face deletion: deleteFaceFeatures.add(face) ONE face at a time (not
  removeFeatures). The injected delete_face(face) does this correctly.

CURVED SURFACES & MANY-CUT PATTERNS
- For a pattern that must CONFORM to a curved face use wrap_cut; don't hand-roll
  it. OMIT face_tempids so it auto-targets the largest curved face.
- wrap_cut reports ok/no-op/failed per profile. A no-op profile fell outside the
  face's angular sector (~70-100 deg per flat sketch); split into sectors at
  0/90/180/270 using construction_plane_tangent.
- Run sketch_axis_map before placing geometry on an offset/tangent plane.
- Use construction_plane_offset/tangent, not ConstructionPlaneInput.setByPlane
  or ConstructionPoints.add (both raise "Environment is not supported").
- For CELL/LATTICE patterns prefer cut_prisms (TemporaryBRepManager): no
  construction planes, no face tempIds, hundreds of cuts as ONE feature. In raw
  code use tbm() and commit_tool_body().

ASSEMBLIES
- One component per distinct part; repeated hardware = one component with
  multiple occurrences via copy_component.
- Copies are occurrences named 'Screw:1', 'Screw:2' (see list_components).
  move_component, ground_component and the joint tools need the occurrence
  name once a part has been copied - the bare component name is then
  ambiguous and is refused.
- Ground exactly one base component, position others, THEN joint. Joints win
  over transforms.

WHEN TO USE RAW CODE
- Canned tools cover boxes, cylinders, holes, fillets, shells, tapped holes,
  components, joints, wraps and many-prism cuts. For lofts, sweeps, organic
  surfaces or features referenced to specific faces/edges, write API code via
  run_fusion_code_safe and verify with view_model + cross_section.
- Filepaths and names are passed safely - do NOT escape backslashes or quotes.
- NEVER use defineAsFreeMove (hard-crashes the host) or defineAsOffset (missing).
  The listener refuses both.

STYLE
- State key dimensions and parameter names before building so the user can
  correct you before geometry exists.
- Keep the user informed which body/component you're operating on.
- Build primary masses first, then gussets and fillets as separate features.
- For ambiguous dimensions, make a reasonable call and report it.
