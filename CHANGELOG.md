# Changelog

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
