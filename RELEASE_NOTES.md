# v1.8.1 — Document identity, fillet verification, and optimization

Five releases' worth of work since **v1.4.1**, driven by two long real-world
modelling sessions. **60 tools** now, up from 50.

The theme across all of it: *"no error" is not "it worked."* Most of the
expensive failures in this project weren't crashes — they were operations that
reported success while doing nothing, doing it to the wrong document, or
silently destroying geometry elsewhere. Most of this release is about making
those visible.

## ⚠️ Upgrading — read this

**Two files, two different folders.** They are separate installs and updating
one does not update the other:

| File | Goes to |
|---|---|
| `fusion_mcp_server.py` | your project folder (whatever `claude_desktop_config.json` points at) → restart Claude Desktop |
| `MCPListener.py` | `%appdata%\Autodesk\Autodesk Fusion 360\API\AddIns\MCPListener\` → Shift+S → MCPListener → **Stop → Run** |

Then run `ping_fusion`. It now reports **both** versions and warns if they
differ — expect `server v1.8.1 | listener v1.8.1`. A stale add-in reports
`PROTOCOL MISMATCH` instead of silently returning empty payloads.

---

## Highlights

### Document identity (1.6.0) — the most damaging bug class, fixed

Claude couldn't reliably tell which document was open. Symptoms: overwriting an
unrelated design, starting a new document instead of continuing in the open one,
and reporting saves that never happened. Four compounding causes:

- **`doc.dataFile` routes through `app.data`**, which can throw for an entire
  session on some builds. A *saved* document therefore looked unsaved, and the
  old logic responded by calling `saveAs` — **creating a duplicate**.
  `save_document` now tries the non-destructive `doc.save()` first when lineage
  is unreadable, and refuses to fall back to `saveAs` without explicit
  `force_new_lineage=True`.
- **Identity was a stale cache** that only refreshed after a job ran. Switching
  documents in the Fusion UI left it reporting the previous one. It now
  refreshes on demand and reports its own snapshot age and a `fresh` flag.
- **`isSaved` was the sole branch signal.** Every signal is now read
  independently so contradictions are visible.
- **No visibility of open documents.** New **`document_report()`** — an
  authoritative live read listing every open document, distinguishing
  *"never saved"* from *"cannot tell"*. New **`activate_document()`** binds work
  to the right file.

Saves now print **CONFIRMED** only when an independent signal agrees (version
bump / modified cleared / lineage appeared), otherwise **UNCONFIRMED**.

### Verification everywhere (1.6.1)

Audited for the same pattern elsewhere and found four more:

- **40 tools assumed `app.activeProduct` is a Design.** It isn't in the
  Drawing/CAM/Render workspace. All now route through a guard that names the
  actual product type instead of failing obscurely.
- **Exports announced success without checking.** `em.execute()`'s return value
  was being *discarded*. STEP/STL/DXF now verify the return value **and** that
  the file exists with non-zero size.
- **`set_parameter` never read back.** Fusion silently clamps expressions it
  dislikes, so a parameter could report as 50 mm while holding 2 mm. Now reads
  back and reports `MISMATCH`.
- **Joints reported success without verifying.** Now checks the joint count
  increased.
- **`clear_document`** — the most destructive tool — checked nothing. It now
  prints the target document name and lineage before deleting.

### Fillets that can actually be aimed (1.5.0)

`which=all/vertical/top` couldn't express what real fillets need. New
**`select_edges(face_pair="Cylinder|Cone", z_min_mm=…, min_len_mm=…)`** returns
tempIds straight into `fillet_edges_smart`. `list_edges` now reports each edge's
adjacent surface-type pair.

`fillet_edges_smart` now verifies **topology as well as volume** — two opposite
failure modes:

- a blend that propagates and **adds** geometry far away (volume overshoot);
- an over-large radius that **consumes** a narrow feature (a 1.3 mm rim annulus
  under a 2 mm fillet) — where the volume delta looks perfectly reasonable while
  the feature is rounded away.

New **`fillet_preview()`** flags at-risk faces before committing. A **zero
volume delta is now reported as a result, not a failure** — it means the
surfaces already meet tangentially.

### Escape hatches (1.5.0)

- **`force_delete_feature()`** — resolves a mutual-reference deadlock where two
  features each blame the other and neither suppress nor delete works in any
  order. Rolls the marker back before deleting, which is what the Fusion UI does.
- **`sweep_tube_check()`** — `ASM_SELF_INTER` blames the profile, but on a
  circular profile the *path* is the cause. Measures minimum path curvature
  against tube radius.
- **`respline()`** — rebuild a fitted spline with new fit points (Fusion can
  move but not add them). Honest about breaking downstream references.

---

## Performance (1.7.0 / 1.8.0)

Measured, not guessed:

| Change | Effect |
|---|---|
| **Lazy identity refresh** | Removed a 4-second main-thread heartbeat (~21,600 interrupts/day). **Idle host cost now zero.** |
| **`run_batch()`** | Fuses N operations into **one round trip and one Fusion job** |
| **Docstring trim + `fusion_help()`** | **~811 tokens saved on every request** (~40k per 50-turn session) |
| **`view_model` resolution** | Default 1200×900 → 800×600: **~800 tokens saved per screenshot** |
| **Terse-on-success output** | Detail hidden on clean success; **always shown in full on any warning** |
| **`cross_section` bisection** | Boundary precision **1.94 mm → 0.001 mm** for 16 extra kernel queries |
| **Face-iteration caps** | Bounded at 4,000 faces instead of stalling on dense models |

---

## Fixes in 1.8.1

Two regressions the optimization work itself introduced, found by stress-testing
rather than assuming:

- **`run_batch` could fire a destructive operation during collection.** It used
  a *denylist*, so anything unlisted executed for real during what should have
  been a dry pass — batching `clear_document` actually wiped the document, then
  reported the batch as failed. Now an explicit **allowlist** of 27
  geometry/parameter tools, with all operations validated *before* collection
  begins, the async path hard-refused while collecting, and a lock so concurrent
  calls can't be swallowed.
- **Terse mode hid the body of report tools.** `document_report`'s entire output
  is indented, so the tool built to prevent wrong-document overwrites was
  reduced to one line. Compaction is now opt-in per call; 18 report tools opt out.

---

## Other fixes since 1.4.1

- Degenerate edges (zero-length, <2 faces) no longer crash edge iteration with
  `Bad index parameter` — guards are inside the listener helpers.
- `explain_fillet_error` gained nine more ASM codes (`ASM_ABL_NO_GEOM_EF`,
  `ASM_ABL_CVXTY_VIOLATION`, `ASM_BL_NO_VTX_GEOM`, `EDGE_REFERENCE_LOST`, …).
- `refresh_geometry()` — `computeAll()` doesn't guarantee a fresh `body.volume`;
  identical readings after a sketch edit can make a correct edit look failed.
- `path_report()` — a mis-chained path silently produces a short sweep; segment
  count is the only cheap tell.
- Tool docstrings carry a version stamp, because Claude Desktop caches tool
  schemas at startup and a corrected docstring can still *display* stale.

---

## Known limitations

- **`open_document` / `list_recent_designs` / `list_versions` require
  `app.data`.** When it's broken for a session there is no workaround — open the
  design in the Fusion UI.
- **`defineAsFreeMove` and `defineAsOffset` are refused** by the listener; the
  former hard-crashes the Fusion host.
- **This is verified against mocks, not a live Fusion session.** The `adsk` API
  surface is exercised through simulated objects, so treat the first run of any
  release as a shakedown and keep your work saved.
