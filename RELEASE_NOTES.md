# v1.9.0 — Localhost hardening and honest failure reporting

A review of the whole codebase found one security hole, and a set of places
where a tool reported a success it hadn't earned or quietly did something other
than what was asked. This release fixes them, and adds a test suite that runs
without Fusion.

## ⚠️ Upgrading — read this

**Update both files. 1.9.0 changes the wire protocol (2 → 3)**, so a new server
and an old add-in refuse to work together until both are current.

| File | Goes to |
|---|---|
| `fusion_mcp_server.py` | your project folder (whatever `claude_desktop_config.json` points at) → restart Claude Desktop |
| `MCPListener.py` | `%appdata%\Autodesk\Autodesk Fusion 360\API\AddIns\MCPListener\` → Shift+S → MCPListener → **Stop → Run** |

Then run the preflight (`launch.bat` / `./launch.sh`), which now checks the
protocol, version and auth token, or ask Claude to **"Ping Fusion."** Expect
`server v1.9.0 | listener v1.9.0`.

There's nothing to configure for the new auth token: the add-in writes it and
the server reads it.

## Security: web pages could run code through the listener

Binding to `127.0.0.1` kept the network out, but not your browser. Any page you
visited could POST to `http://127.0.0.1:8765/exec`. A plain-text request needs
no CORS preflight, so the browser sends it without asking, and the listener
ran the code with your full user permissions. A DNS-rebinding site could reach
it the same way.

Every request now needs:

- a **Host** header of `127.0.0.1` or `localhost`, which refuses DNS rebinding;
- an **`X-MCP-Token`** header matching a random token the add-in writes to
  `~/.fusion_mcp/listener_token` each time it starts. A page can't read that
  file, and the custom header forces a CORS preflight the listener never
  approves.

`/ping` stays open (liveness and version only), so a version mismatch can
still be diagnosed.

## Fixes: tools that reported success without earning it

- **`run_fusion_code_safe` never rolled anything back.** It called `deleteMe()`
  on timeline items, which don't have one, swallowed the error, and printed
  `ROLLED BACK` anyway. It now deletes through `.entity`, checks the timeline is
  back where it started, and says `ROLLBACK INCOMPLETE` when it isn't.
- **`ping_fusion` warned on every correct install.** 1.8.x changed only the
  server, so the listener still reported 1.7.0. The two versions now move in
  lockstep, enforced by a test.
- **`get_model_state` said UNSAVED when it couldn't tell** (app.data down). That
  misreading is what leads to saveAs and a duplicate design. It now says
  UNKNOWN.
- **A job that ran past its budget stayed `timeout` forever**, even after it
  finished, so callers retried work that had completed. The overrun is now a
  separate `budget_exceeded` flag, and the status still ends OK or ERROR.
- **Exports could print CONFIRMED for a file from an earlier run.** They now
  require the file to have been written by this call, and DXF export checks
  `saveAsDXF`'s return value, which it used to ignore.
- **`tapped_hole` silently used the wrong drill.** `m4`, `M4X0.7`, `M20`,
  `#2-56` and `3/8-16` all got 2.5 mm, and fine-pitch `M8x1` got the coarse
  6.8 mm.
  - Metric drills are now major − pitch; unified sizes #0-80 to 3/4-16 come
    from a table.
  - An unknown size is an ERROR and nothing is cut. `tap_drill_mm` lets you
    size the hole yourself.
  - The modeled-thread option now matches the designation properly and uses the
    inch thread type for unified sizes.
- **Component names fell back to the root component on a typo**, and copies
  couldn't be addressed, because every lookup returned the first copy. All
  lookups now go through `find_component` / `find_occurrence`. They accept
  occurrence names like `Screw:3`, and refuse misses and ambiguous names with a
  list of valid ones.
- **`run_batch(stop_on_error=True)` only stopped on exceptions.** Most tools
  report failure by printing, so it now also stops on `ERROR`, `WARNING`, `***`,
  `NOT CREATED` and similar.
- **`drill_hole` and `tapped_hole` ignored `body_index`** and cut every body the
  hole crossed. They, and `hole_on_face`, now cut only the target body.
- **`launch.bat` reported "no python found"** on machines without the `py`
  launcher, even with `python` on PATH.
- **After a transport timeout, the message pointed `job_status` at the previous
  job.**

## Smaller fixes

- `run_fusion_code_safe` and `run_batch` no longer re-indent your code, which
  rewrote multi-line strings and shifted traceback line numbers.
- **Unknown values are now errors instead of silent defaults:**
  - joint `motion='slider'` used to build a rigid joint and print
    "CONFIRMED: slider joint";
  - joint `axis`, offset-plane `base`, `fillet_edges` `which` and STL `quality`
    also fell back silently.
- `move_component` keeps the occurrence's rotation, and `copy_component` copies
  keep the source's.
- `fillet_edges_smart` and `fillet_preview` take `component_name`, matching
  `select_edges`.
- The sliver-face threshold is 0.05 mm² everywhere; two tools still used 0.01.
- `construction_plane_tangent`'s reference sketch is named and hidden.
- `view_model` uses a per-call temp file instead of `~/fusion_view.png`.
- Two open documents with the same name are no longer both marked active.
- Stop → Run no longer leaks a watchdog thread.
- A fillet error hint named a tool that doesn't exist (`sweep_tube`).
- **Preflight:**
  - checks protocol, version and token;
  - finds the server path anywhere in the config's args;
  - `serve` now works with MCP Inspector on Windows.

## Project

- **Test suite** (`tests/`, 178 tests, needs no Fusion):
  - compiles every tool's generated code;
  - runs the rollback and batch logic against fakes;
  - drives the listener's request guard over real HTTP.

  CI runs it on Windows and Linux for every push.
- **One version number**, enforced by a test.
- **PDFs are release assets** now, not committed files.
- `mcp[cli]` is capped below 2.

## Known limitations

- **Not yet checked in a live Fusion session.** Three suspected issues from the
  same review still need a real Fusion run:
  - whether `drill_hole` / `tapped_hole` (sketch at z=0, cut toward −Z) reach a
    box built by `create_box` (z from 0 upward);
  - whether `sweep_tube_check` unpacks `getCurvature` correctly;
  - whether `joint_at_point`'s construction points work in parametric designs.

  The new modeled-thread matching and `participantBodies` targeting follow the
  API documentation but are also untested live.
- **`open_document` / `list_recent_designs` / `list_versions` require
  `app.data`.** When it's broken for a session there is no workaround — open the
  design in the Fusion UI.
- **`defineAsFreeMove` and `defineAsOffset` are refused** by the listener; the
  former hard-crashes the Fusion host.
- **The tests use simulated `adsk` objects.** Treat the first run of any release
  as a shakedown and keep your work saved.

Full per-version history: `CHANGELOG.md`. The v1.8.1 notes (everything from
v1.4.1 to v1.8.1) remain on the v1.8.1 GitHub release.
