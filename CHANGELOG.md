# Changelog

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
