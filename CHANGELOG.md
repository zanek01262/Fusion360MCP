# Changelog

## 1.8.1

Two regressions found by stress-testing 1.7.0/1.8.0 rather than assuming they
were sound. Both were introduced by the optimization work itself.

### Fixed - `run_batch` could fire a destructive operation during collection
`run_batch` used a DENYLIST of non-batchable tools, so anything not listed ran
for real during what should have been a dry collection pass. Verified: batching
`clear_document` actually WIPED the document and then reported the batch as
failed - data loss plus a misleading error.

Now fail-safe:
- An explicit **allowlist** (27 geometry/parameter tools). A tool omitted simply
  cannot be batched, instead of silently executing.
- Every operation is validated **before** collection mode is entered, so an
  invalid batch has no side effect at all.
- `_send_async` refuses outright while collecting, so the async path cannot fire.
- A reentrant lock around collection, so a concurrent call cannot be swallowed
  into the batch buffer and silently never executed.

### Fixed - terse mode hid the body of report tools
The 1.8.0 compaction dropped indented continuation lines, but the entire output
of `document_report` is indented - so the tool built specifically to prevent
wrong-document overwrites was reduced to its first line. `get_state`'s
open-document list, `list_faces`, `cross_section` boundaries and others were
affected the same way.

Compaction is now opt-in per call: 18 report tools pass `compact=False`, because
their body IS the detail. Action tools still compact on clean success, so the
token saving is retained where it was actually free.


## 1.8.0

The remaining measured optimizations from the 1.7.0 profiling pass.

### Claude tokens
- **Terse-on-success output (`set_verbosity`).** Detail is valuable exactly when
  something is WRONG; on a clean success it is pure token cost paid on every
  call. Indented explanatory lines are now hidden when the payload contains no
  trouble marker (***, WARNING, ERROR, FAILED, UNCONFIRMED, MISMATCH, REFUSED,
  Traceback, no-op, STALE). Any one of those disables compaction for the whole
  payload, so failures always print in full. `set_verbosity('full')` restores
  everything.

### Host compute
- **Face-iteration caps.** `get_model_state` computed the area of every face of
  every body to count slivers, and `list_faces` measured every face before
  capping the output - each area/centroid read is a kernel query. Both now bound
  at 4000 faces and say so, instead of stalling on a dense model.
- **`cross_section` refines boundaries by bisection** instead of scanning
  uniformly. A uniform scan wastes queries deep inside solid regions and still
  only resolves boundaries to span/N. Now: coarse pass, then ~8 bisections per
  transition. Measured on a hollow box with walls at 10.700/49.300 mm: reported
  10.699/49.301 mm from 47 kernel queries, versus 1.94 mm precision from the old
  31-query uniform scan - roughly 1900x better precision for 16 extra queries.
  Also reports span widths and the query count.

### Round trips
- **`job_status` documents polling backoff** (~5s, 10s, 20s, then 30s). Each
  poll is a round trip that returns the whole accumulated stdout again;
  get_state is cheaper when only liveness is needed.


## 1.7.0

Optimization pass - measured, not guessed.

### Host CPU (a regression introduced in 1.6.0)
- **The identity refresh was a 4-second heartbeat.** It queued a Fusion
  main-thread job every 4 seconds for as long as Fusion was open (~21,600
  interrupts/day), each enumerating every open document and touching dataFile -
  which raises, expensively, on builds where app.data is broken. Cost was paid
  continuously even when nobody was using the tool. It is now LAZY: /state
  requests a refresh only when its cache is actually stale, coalesced to at most
  one in flight and one per 2 s. Idle cost is now zero.

### Speed - round trips
- **`run_batch(operations_json)`** fuses N tool calls into ONE round trip and ONE
  Fusion job. Every tool call otherwise costs a full hop (model -> server ->
  HTTP -> main thread -> back), so ten holes cost ten hops. Each operation still
  prints its own output and per-step volume deltas; stop_on_error aborts at the
  first failure. Verified: a 5-operation batch used 1 round trip instead of 5.
  Not for steps whose arguments depend on an earlier step's result.

### Claude tokens
- **Tool schemas are sent on EVERY request.** Measured at ~5,488 tokens. Seven
  of the largest docstrings were trimmed to their imperative warnings, with the
  reference material moved to a new on-demand **`fusion_help(topic)`** tool
  (topics: helpers, planes, traps, workflow). Now ~4,677 tokens - a saving of
  **~811 tokens on every request**, roughly 40k over a 50-turn session.
- **`view_model` resolution is now a parameter, defaulting to 800x600 instead of
  1200x900.** An image costs about (w*h)/750 tokens, so the default drops from
  ~1,440 to ~640 - **~800 tokens saved per screenshot**, the single biggest
  per-call saving. Raise it only when judging fine detail; 640x480 (~410 tokens)
  suffices for a quick sanity look.


## 1.6.1

Audit for other instances of the bug class behind 1.6.0 (implicit target /
unverified success). Found four more, all real, all fixed.

- **40 tools assumed `app.activeProduct` IS a Design.** It is not, in the
  Drawing, CAM, Render or Simulation workspace - `.rootComponent` then fails
  obscurely or targets something unexpected. All 40 now route through an
  injected `_require_design()` that raises a clear error naming the actual
  product type and document, and telling the user to switch workspace.
- **Exports announced success without checking.** `em.execute()`'s return value
  was discarded and the tool printed "exported ..." regardless - the same
  "thinks it saved but it didn't" failure applied to files. STEP/STL/DXF now
  verify the return value AND that the file exists with non-zero size, printing
  CONFIRMED with the byte count or an explicit failure.
- **`set_parameter` never read back.** Fusion silently rejects or clamps an
  expression it does not accept, so a parameter could report as set to 50 mm
  while holding something else. It now reads the stored expression back and
  reports CONFIRMED or MISMATCH with both values.
- **Joints reported success without verifying.** `joints.add()` returning
  without error does not mean a joint exists. Now checks the joint count
  increased.
- **`clear_document` - the most destructive tool - checked nothing.** It now
  prints the target document name, lineage, and current contents before
  deleting anything.


## 1.6.0

Document identity. Claude could not reliably tell which document was open, which
produced the worst failures in the project: overwriting an unrelated design,
starting a new document instead of continuing in the open one, and reporting a
save that never happened. Four compounding root causes, all fixed.

- **`doc.dataFile` routes through `app.data`.** When app.data is dead (common on
  this build, survives a Fusion restart), lineage read as missing - and the old
  logic treated "no lineage" as "never saved", so it called `saveAs` and created
  a DUPLICATE of a document that was already saved. `save_document` now tries the
  NON-DESTRUCTIVE `doc.save()` first whenever lineage is unreadable, and refuses
  to fall back to `saveAs` without explicit `force_new_lineage=True`. Verified:
  in that scenario it now makes zero saveAs calls.
- **Identity was a stale cache.** `get_state` only refreshed after a job ran, so
  switching documents in the Fusion UI left it reporting the previous one. The
  listener now refreshes identity on a timer, and `/state` reports its own
  snapshot age and a `fresh` flag.
- **`isSaved` was the sole branch signal.** Every identity signal (`isSaved`,
  `isModified`, `dataFile`, version, folder) is now read INDEPENDENTLY and
  reported side by side, so a contradiction is visible instead of collapsed.
- **No visibility of open documents.** `document_report()` is a live,
  authoritative read listing EVERY open document and which is active, ending in
  an explicit verdict that distinguishes "never saved" from "cannot tell".
  `activate_document(name_or_index)` binds work to the user's document.
- **Saves are now verified, not assumed.** `save_document` reports version
  before/after and prints CONFIRMED only when an independent signal agrees
  (version bump / modified cleared / lineage appeared); otherwise UNCONFIRMED,
  with instructions not to report the document as saved.


## 1.5.0

Round-2 field report (organic modelling: spline surfaces of revolution, lofted
bifurcation, swept elbows, ~15 fillet campaigns).

### Added - edge selection & fillet verification (the biggest gap)
- **`select_edges(...)`** - predicate selection by adjacent **surface-type pair**,
  z range, and length, returning tempIds. `which=all/vertical/top` could not
  express what every real fillet needs, so the good search logic in
  `fillet_edges_smart` was locked behind a selector that couldn't reach it - the
  same ~30-line predicate was hand-rolled about fifteen times in one session.
- **`list_edges` now reports the surface-type pair** (e.g. `Cylinder|Cone`), so
  callers stop re-implementing the classifier.
- **`fillet_edges_smart` accepts `edge_tempids`** and now verifies **topology as
  well as volume**: it reports which faces disappeared and warns when a face
  smaller than `4r^2` was consumed. Round 1's failure was a fillet that ADDED
  geometry far away; this session's was the inverse - a 2.0 mm fillet that ate a
  1.3 mm rim annulus and rounded the tube ends away while the volume delta
  (-18 mm^3 vs ~28 expected) passed every threshold.
- **`fillet_preview(...)`** - shows the affected faces and flags any smaller than
  `4r^2` BEFORE committing.
- **A zero volume delta is now reported as a RESULT, not a failure** - it means
  the surfaces already meet tangentially and need no fillet.

### Added - escape hatches
- **`force_delete_feature(name)`** - resolves a MUTUAL-REFERENCE DEADLOCK where
  two features each blame the other and neither suppress nor `deleteMe()` works
  in any order. Rolls the marker back before deleting (what the Fusion UI does),
  then reports what else broke. This turned a blocked session into a 2-minute fix.
- **`sweep_tube_check(...)`** - measures a path's minimum curvature radius against
  the tube radius. `ASM_SELF_INTER` blames the profile/taper, but on a circular
  profile the PATH is the cause.
- **`respline(...)`** - rebuild a fitted spline with new fit points (Fusion can
  move but not add them). States honestly that downstream references break, and
  reports which features became unhealthy.

### Fixed
- **Degenerate-edge crash.** Tangentially-meeting lofts leave zero-length edges
  with <2 faces; `e.faces.item(1)` raised `Bad index parameter`. Guards are now
  inside the listener helpers (`edge_ok`, `safe_edges`, `edge_face_pair`) and
  every edge-walking tool, so callers stop rediscovering it via traceback.
- **`explain_fillet_error` knew almost none of the codes.** Added
  `ASM_ABL_NO_GEOM_EF`, `ASM_ABL_CVXTY_VIOLATION`, `ASM_BL_NO_VTX_GEOM`,
  `ASM_BL_CHG_PT_COMPLEX_VERT`, `ASM_BL_CANNOT_REORDER`, `ASM_INCONS_VERT_ATT`,
  `ASM_SELF_INTER`, `FILLET_NO_EDGE_FOUND`, `EDGE_REFERENCE_LOST`.
- **`refresh_geometry()`** - `computeAll()` does not guarantee a fresh
  `body.volume`; identical readings after a sketch edit nearly led to concluding
  a correct edit had failed.
- **`path_report(path)`** - segment count + length, so a mis-chained path (which
  silently produces a short sweep) is visible.

### Notes on "did not land" items
- **clear_document (item 5):** real, and fixed in 1.4.1 - the 1.4.0 rewrite was
  correct but kept a fallback that still bulk-deleted bodies, the exact hang path.
- **YZ docstring (item 7):** corrected in 1.4.0. The docstring now carries its
  own version stamp, because Claude Desktop caches tool schemas at startup - a
  fixed docstring can still *display* stale without a full restart.
- **app.data (item 6):** `save_document`'s fallback works and is verified.
  `open_document` / `list_recent_designs` genuinely cannot be worked around; this
  is now stated plainly rather than implied.
- **`ping_fusion` reports both server and listener versions** and warns when they
  differ (added 1.4.1) - a stale *server* previously produced no mismatch signal.


## 1.4.1

Follow-up to two field reports that turned out to be **version mismatch** - both
symptoms were v1.2.1 behaviour, and both were already fixed in 1.4.0. The real
finding was that nothing made that diagnosable, plus one genuine remaining
hazard.

- **`clear_document` fallback sweep hardened.** The 1.4.0 rewrite used
  reverse-order `entity.deleteMe()` correctly, but kept a fallback that still
  bulk-deleted `bRepBodies` - the exact path that hung for 10+ minutes on a
  parametric document. That sweep now runs ONLY when there is no timeline
  (direct-modelling docs). On a parametric doc with leftovers it reports what
  remains and points at `retire_body()` instead of grinding. Verified: zero bulk
  deletes on a parametric doc.
- **`ping_fusion` now reports BOTH server and listener versions** and warns when
  they differ. The existing protocol handshake only caught a stale *listener*;
  a stale *server* (this case) produced no mismatch signal at all, so an old
  docstring and an old code path looked like live bugs.


## 1.4.0

Fixes from a full modelling session (splitter funnel). Several of these were
silent-wrong-geometry bugs, which are the expensive kind.

### Fixed (P0 - wrong facts that produced wrong geometry)
- **The documented YZ sketch-plane mapping was WRONG.** Docs claimed
  `sketch X -> world Y, sketch Y -> world Z`; measured behaviour is
  `sketch X -> world -Z, sketch Y -> world +Y`. Building off the wrong mapping
  yields a body with a correct bounding box and correct profile area that only
  reveals itself several operations later. Docstring corrected, and new injected
  helpers `P_xy/P_xz/P_yz` (world mm in, correct sketch point out) plus
  `verify_plane_mapping(sketch)` mean nobody has to reason about it.
- **`report_volume_delta` cried wolf on every Join.** It summed ALL bodies, so a
  Join that merges two bodies is volume-neutral and got flagged as a no-op -
  training you to ignore the one warning that matters. Now takes a
  `capture_state(target_body)` snapshot and warns only when body count AND
  volume are both unchanged.
- **Nothing detected silent geometry destruction by fillets.** A tangent-chained
  1.5 mm fillet propagated ~20 mm and reverted a bore radius, reporting success
  with a valid body and no slivers. `fillet_edges` now checks the volume delta
  against `~0.1*r^2*L` and warns on an order-of-magnitude overshoot;
  `fillet_volume_sanity()` exposes the same check to raw code.

### Fixed (P1 - broken tools)
- **`export_stl` was unusable at every quality setting.** The enum names
  `*QualityTriangleMeshRefinement` do not exist on this build; the real members
  are `MeshRefinementLow/Medium/High/Custom`. Now uses those, with a graceful
  fallback that prints the available members if they change again.
- **`app.data` failures killed save/list/open for a whole session.**
  `save_document` now detects `InternalValidationError`, falls back to
  `doc.save()` for an already-saved document (the path that does work), and says
  clearly that a new lineage needs the Fusion UI. `list_recent_designs` explains
  the failure instead of returning a bare error.
- **`clear_document` hung for 10+ minutes and took Fusion down.** Reimplemented
  as reverse-order `item(i).entity.deleteMe()` (TimelineObject has no
  `deleteMe`) with `progress()` per feature, and it now runs through the async
  job system by default so the channel is never blocked.

### Fixed (P2)
- **Bodies added via `BaseFeature` return a stale reference** -
  `ALL_TOOL_BODY_REFERENCE_LOST` when reused as a Combine tool. `cut_prisms` and
  `commit_tool_body()` now re-fetch by name after `finishEdit()`. This was a real
  bug in code shipped in 1.3.0.

### Improved (P3 - observability)
- **`view_model`**: added `bottom`; an unknown orientation is now an ERROR
  instead of silently returning the previous view; added
  `zoom_x/y/z_mm` + `zoom_radius_mm` to frame a small detail instead of always
  fitting the whole model.
- **`cross_section`**: added `at_a_mm` / `at_b_mm` to pin the held axes, so you
  can scan through a specific feature rather than the bbox mid-plane.
- **`fillet_edges_smart`** - searches tangent-chain on/off x descending radii,
  verifies volume delta, rolls back overshoots, reports what worked.
  **`explain_fillet_error`** translates raw ACIS/ASM codes into fixes.
- **`smallest_faces(body)`** prints the actual smallest face areas;
  `sliver_face_count` threshold raised to 0.05 mm^2 (0.010-0.014 mm^2 faces are
  visible artifacts that the old cutoff missed).
- **Transport timeouts** now return a structured RUNNING status naming the job
  id, with an explicit "do not re-issue" note - a timed-out call had already
  completed a save in one case.
- **`retire_body()`** + docs for the replace-a-body idiom
  (`removeFeatures.add(old)`), which is strictly safer than clearing.

### Docs
- New "API traps on this build" section in TOOLS.md covering the plane mapping
  table, `TimelineObject.entity`, stale BaseFeature handles, `is`-comparison on
  sketch entities, `addByCenterStartSweep` normalisation, `setMidRadii` list
  types, `app.data` failures, and silent fillet propagation.


## 1.3.0

Security fix plus the curved-surface toolset the docs already promised.

### Fixed (P0)
- **SECURITY: argument injection.** Tool arguments were pasted directly into
  Python source text, so a filepath or name containing a quote broke out of the
  literal and executed arbitrary code inside Fusion. All ~22 interpolation sites
  now emit proper Python literals via `repr()`. Verified by executing generated
  code for 9 tools x 7 adversarial payloads (63 runs) with an injection canary:
  zero escapes.
- **P0: Windows paths raised SyntaxError.** Same root cause - `C:\parts\x.step`
  was pasted raw, so backslashes became escape sequences. Fixed by the same
  change; paths with backslashes, trailing separators and apostrophes now work.

### Added (P1)
- **`wrap_cut`** - wrap a flat sketch onto a non-planar face and cut/emboss to
  depth. Previously referenced by the docs but MISSING from the build, so
  following the instructions led straight into a wall. Uses the correct
  `EmbossFeatures.createInput(profiles, faces, depth)` signature (negative depth
  cuts), cuts per-profile with individual volume checks, and reports
  ok/no-op/failed counts with sector guidance.
- **`project_to_surface`**, **`construction_plane_offset`**,
  **`construction_plane_tangent`**, **`sketch_axis_map`** - the supporting cast
  the wrap workflow needs. The construction-plane tools replace
  `ConstructionPlaneInput.setByPlane`, which is environment-blocked here, and
  echo world position so unit slips surface immediately.
- **`cut_prisms`** + injected **`tbm()`** / **`commit_tool_body()`** helpers -
  many prism cuts in ONE timeline feature via `TemporaryBRepManager`. Depends on
  neither construction planes nor face tempIds, so it sidesteps the two most
  fragile things in this environment.

### Docs
- TOOLS.md gains a curved-surface section and a TemporaryBRepManager section
  with a worked example.


## 1.2.1

Fixes from a project-wide review triggered by a real degraded-state incident
(old listener + new server = every payload empty while status read OK).

- **CRITICAL: switched to ThreadingHTTPServer.** The v1.2.0 listener used a
  single-threaded HTTPServer, so while `/exec_sync` held the one HTTP thread
  waiting on a job (up to ~110s), `/ping` and `/state` could not answer -
  silently defeating the "separate always-responsive channel" design. Verified
  fixed: ping now answers instantly during a blocked sync call.
- **Version/protocol handshake.** The listener now reports
  `listener_version` + `protocol` in every response; the server checks it and
  returns a loud PROTOCOL MISMATCH message with exact fix steps when the add-in
  is stale - instead of silently rendering empty payloads as STATUS: OK. This
  makes the incident that prompted this release impossible to miss.
- **Fail-loud capture health.** Envelopes include `stdout_bytes`; the server
  marks ok-but-silent results with "(no output captured)".
- **Job-store pruning.** Finished jobs are capped at the 50 most recent so
  long sessions don't grow memory unboundedly.
- **Docs:** INSTALL now warns that Fusion loads the add-in from its AddIns
  folder, not from wherever the project files live - updating one does not
  update the other.


## 1.2.0

Async execution and document safety — driven by a second round of real-use
feedback. The core fix: a 4-minute timeout that told you nothing about what
actually happened in Fusion. Execution is now decoupled from the HTTP response.

### Added — async job model (the big one)
- **Listener is now async.** `POST /exec` enqueues a job and returns a `job_id`
  immediately; the operation runs on the main thread and is polled via
  `GET /job/<id>`, which returns a structured envelope
  (`status`, `stdout`, `traceback`, `elapsed_ms`) and **partial stdout while
  still running** (incremental progress).
- **`run_async(code)` + `job_status(job_id)`** tools for long operations that
  shouldn't block. Plus a `progress(msg)` helper to mark stages visible mid-run.
- **Ping/state on a separate channel.** `GET /ping` and `GET /state` are served
  from cache without the main thread, so they STAY RESPONSIVE during (or after)
  a hung operation. A hung op and a dead listener are now distinguishable.
- **`get_state()`** — cheap, guaranteed-fast document identity + counts (no
  geometry work). The safe first call for crash recovery.
- **Structured result envelope, always.** Every execution returns
  status/stdout/traceback/elapsed; no more bare "no result received". The sync
  path returns partial output and a job id if the op outlives the wait.
- **Watchdog** flags jobs that exceed their budget as `timeout` (clear signal
  rather than a dead channel).

### Added — document safety
- **Document identity surfaced everywhere.** `get_state` and `get_model_state`
  lead with the active document **name + lineage id**, preventing wrong-document
  overwrites.
- **`save_document(name, create_new=False)`** now reports **which lineage it
  versioned**, and `create_new=True` saves a brand-new lineage so a new part
  never buries an unrelated design's history.
- **`list_versions(design_id="")`** — read-only enumeration of a lineage's saved
  versions, so a bad save is recoverable and the user can be pointed to the exact
  version to promote.
- `open_document` reports the new active lineage.

### Added — documentation
- Sketch-plane / axis mapping (XY/XZ/YZ + offset planes, incl. the XZ→world-−Z
  inversion) documented in `run_fusion_code`'s docstring.

### Notes
- `run_fusion_code` uses the bounded sync path (~100s) then hands off to polling;
  no behavior change for short ops.
- Volume-delta no-op detection and the `mm()` unit helper shipped in 1.1.0 and
  already cover two earlier feedback items.

## 1.1.0

Observability, diagnostics, and stability. Injected unit/observability helpers
(`mm()`, `assert_scale()`, `report_volume_delta()`, etc.); auto volume-delta
reporting with no-op warnings on cut/shell tools; `cross_section()`,
`list_faces()`, `list_edges()`, `heartbeat()`; listener-level refusal of known
host-crashing calls; richer `get_model_state()`.

## 1.0.0

First public release. Fusion 360 add-in listener + MCP server with ~30 tools
(primitives, parameters, faces, assemblies/joints, measurement, visual feedback,
export, persistence), preflight launcher, and full docs.
