# v1.9.1 — Fixes found by a live Fusion run

1.9.0's test suite runs without Fusion, against simulated API objects. Running
the tools in a real Fusion session found six bugs those simulations couldn't
catch. Each came from code written against the wrong API behaviour, or from a
success check that trusted the wrong signal, and all predate 1.9.0. All six are
fixed and re-verified in Fusion.

## Upgrading

Update both files, as with every release (the versions move in lockstep):

| File | Goes to |
|---|---|
| `fusion_mcp_server.py` | your project folder (whatever `claude_desktop_config.json` points at) → restart Claude Desktop |
| `MCPListener.py` | `%appdata%\Autodesk\Autodesk Fusion 360\API\AddIns\MCPListener\` → Shift+S → MCPListener → **Stop → Run** |

- **From 1.9.0:** nothing else changes — same protocol (3), same auth token.
- **From 1.8.x or earlier:** 1.9.0 changed the wire protocol and added an auth
  token; see the v1.9.0 release notes. A new server and an old add-in refuse
  to work together until both are current.

Then run the preflight (`launch.bat` / `./launch.sh`) or ask Claude to
**"Ping Fusion."** Expect `server v1.9.1 | listener v1.9.1`.

## Fixes — each verified in a live Fusion session

- **Holes are cut from the body's top.** `drill_hole` and `tapped_hole`
  sketched at z = 0 and cut toward −Z, so they missed every body built upward
  from XY, including everything `create_box` makes. Their own "no effect"
  warning fired, so nothing was damaged, but the holes never landed. They now
  start at the target body's top. Verified:
  - a 5 mm hole removes exactly the expected 35.3 mm³;
  - a 15 mm hole into the upper of two stacked bodies removes 70.69 mm³ from
    it, while the body below keeps its exact volume and stays solid at the
    hole position.
- **Modeled threads work.** `tapped_hole(tap_drill_only=False)` called thread
  lookup methods that don't exist (`getAllSizes` and friends), so a modeled
  thread never applied. It now uses the real API and Fusion's actual size
  naming. Verified: `M3x0.5 6H` and `1/4-20 UNC 2B`.
- **`sweep_tube_check` measures curvature.** It read Fusion's curvature result
  as two values when Fusion returns three, so every path reported "could not
  measure curvature". Verified on a path with a 10 mm arc: it reports
  10.000 mm, and warns that a 12 mm tube won't fit. Evaluation errors now print
  a WARNING instead of being swallowed.
- **`joint_at_point` works.** Fusion refuses construction points built from
  raw coordinates in parametric designs ("Environment is not supported"). It
  now places a hidden sketch point in each component. Verified end to end in an
  Assembly design: the joint solves, and the two points coincide.
- **Joints no longer claim success when they didn't solve.** Fusion will add a
  joint it can't solve and only flag its health, so "the joint count went up"
  proved nothing. CONFIRMED now requires the joint to be healthy **and** its two
  points to coincide; otherwise it says NOT SOLVED or UNCONFIRMED.
  - Fusion moves `component_a` onto `component_b`. An explicitly grounded
    `component_a` is now refused up front with that explanation, instead of
    failing inside Fusion.
  - A rejected joint no longer leaves helper geometry behind.
- **`ground_component(grounded=False)` really frees the part.** Fusion has a
  second flag, "ground to parent", and a part can carry it with `isGrounded`
  off and still be fixed. The tool cleared only `isGrounded` and printed
  `grounded=False` anyway. It now clears both, reads them back, and says
  CONFIRMED or NOT APPLIED. The joint tools free a `component_a` that is only
  grounded to its parent, and say so.

## Also

- **Assembly tools need an Assembly design.** Fusion's Part designs hold a
  single component; `create_component` fails there with "Part Design documents
  can only contain one component". Now documented in TOOLS, INSTALL and the
  project instructions.
- **`list_components` shows grounding** for each part: `grounded`,
  `grounded to parent` or `free`.
- **Docs corrected:** `rigid_joint` moves component_a's origin onto
  component_b's. It was documented as locking the parts "at their current
  relative position", which it never did.
- **Drilling planes are shared.** Each is a hidden construction plane named
  `mcp_hole_plane_z<height>mm`, reused by every hole at that height.
- **Tests: 204** (up from 178). The new ones reproduce the API behaviour Fusion
  actually showed, and fail against 1.9.0.

## Known limitations

- **`open_document` / `list_recent_designs` / `list_versions` require
  `app.data`.** When it's broken for a session there is no workaround — open the
  design in the Fusion UI.
- **`defineAsFreeMove` and `defineAsOffset` are refused** by the listener; the
  former hard-crashes the Fusion host.
- **The tests use simulated `adsk` objects.** This release is the case for
  still doing live runs: treat the first run of any release as a shakedown and
  keep your work saved.

Full per-version history: `CHANGELOG.md`. The v1.9.0 notes (the security fix
and the protocol change) remain on the v1.9.0 GitHub release.
