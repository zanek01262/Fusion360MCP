# Architecture

For the curious, and for anyone wanting to extend the toolset.

## The processes

```
┌─────────────┐   stdio    ┌──────────────────────┐   HTTP POST    ┌────────────────────┐
│   Claude    │ ─────────► │  fusion_mcp_server.py │ ─────────────► │   MCPListener       │
│   Desktop   │ ◄───────── │  (MCP server)         │ ◄───────────── │   (add-in in Fusion)│
└─────────────┘            └──────────────────────┘  JSON result   └────────────────────┘
                                                                              │
                                                                     executes on Fusion's
                                                                       main thread, then
                                                                       returns stdout /
                                                                       traceback / image
```

Three participants:

1. **Claude Desktop** reads `claude_desktop_config.json`, launches the MCP
   server as a child process, and speaks the MCP protocol to it over stdio.
2. **`fusion_mcp_server.py`** is a FastMCP server. Each tool builds a small
   Python snippet (or passes user code through) and HTTP-POSTs it as JSON to the
   listener, returning the listener's response to Claude.
3. **`MCPListener`** is a Fusion add-in. It runs an `http.server` on
   `127.0.0.1:8765` with async endpoints: `POST /exec` (enqueue, returns job id),
   `GET /job/<id>` (envelope), `GET /ping` and `GET /state` (served without the
   main thread), and `POST /exec_sync` (bounded blocking, back-compat). Incoming
   code runs on the main thread; status, stdout, and tracebacks go into a job
   store.

## Why two processes instead of one?

Fusion's Python API can only be called **from inside Fusion's own interpreter**,
and only on its **main thread**. An external MCP server can't touch the API
directly. So the add-in lives inside Fusion to hold API access, and the MCP
server lives outside (where Claude Desktop can spawn it) and talks to the add-in
over localhost HTTP.

## The async job model and the main-thread handoff

An HTTP server needs a background thread to listen, but Fusion's API rejects
calls from any thread except its main one. And a *hung* geometry call blocks
that main thread — so no HTTP cleverness can make a queued geometry call jump
ahead. The design works around this:

**Jobs are async.** `POST /exec` enqueues a job, fires a Fusion custom event
(`MCPListenerExec`) with the job id, and returns the id **immediately**. The
event is dispatched on the main thread, where a handler runs the code and writes
status + stdout into a shared job store. `GET /job/<id>` returns a structured
envelope (`status`, `stdout`, `traceback`, `elapsed_ms`) — and reads partial
stdout while the job is still running (incremental progress via a live buffer).

**Ping and state never touch the main thread.** `GET /ping` and `GET /state` are
answered entirely from cached data on the HTTP thread. So even while a geometry
call churns (or hangs) on the main thread, liveness and cheap document state stay
responsive. This is what makes "is it busy or dead?" answerable.

**A watchdog flags overruns.** A background thread sets `budget_exceeded` on any
job that runs past its budget. It can't force-kill a hung main thread (Fusion's
API has no such hook), but the caller gets an immediate, unambiguous signal
instead of a dead socket. It only *flags*: the status still moves to `ok` or
`error` when the job ends, so the caller learns whether a long operation
actually landed instead of retrying one that had completed. The thread exits
when the add-in stops.

**Sync path for convenience.** `POST /exec_sync` enqueues then waits up to
`wait_s` and returns the envelope — used by `run_fusion_code` for short ops. If
the op outlives the wait, it keeps running and stays pollable by job id; the
response says so and returns partial stdout. The server's HTTP client timeout
sits above `wait_s`, so the agent always gets a structured envelope, never a
bare socket timeout.

## Units

Fusion's API is internally in **centimeters**. Every tool exposes **millimeters**
to Claude and divides by 10 before talking to the API. When writing raw code via
`run_fusion_code`, you're in cm — divide mm by 10 yourself.

## The safety wrapper

`run_fusion_code_safe` records the timeline count and marker position, runs your
code as a compiled string (never re-indented, so strings and traceback line
numbers are untouched), and on exception deletes the features the call created —
via `timeline.item(i).entity.deleteMe()`, since a timeline item itself has no
`deleteMe`. It then checks the timeline count is back where it started and says
`ROLLED BACK` only if it is (`ROLLBACK INCOMPLETE` otherwise). It's effective on
parametric documents (design history on, the default). Direct-modeling docs have
no timeline; the wrapper says `NO ROLLBACK` rather than implying one happened.

## Visual feedback

`view_model` orients the camera, fits, saves a PNG via
`Viewport.saveAsImageFile`, reads the bytes back, and returns them as an MCP
`Image`. Because the MCP server and Fusion are on the same machine, the file
round-trips through a per-call temp file that is deleted afterwards. (This is
the one tool that assumes shared local disk.)

## Security model

The listener **executes arbitrary Python** it receives — that's the whole
mechanism — so who can reach it is the entire security question.

- **Bound to `127.0.0.1` only**, so it isn't reachable off-machine.
- **Loopback alone is not enough.** A web page open in your browser can send a
  POST to `127.0.0.1:8765`: a `text/plain` request needs no CORS preflight, so
  the browser sends it without asking. Before 1.9.0 the listener executed such
  a request. A DNS-rebinding site can reach it too, under its own hostname.
- **Two checks now guard every request:**
  1. **Host header must be `127.0.0.1` or `localhost`.** Refuses DNS rebinding,
     where the Host is the attacker's domain.
  2. **`X-MCP-Token` header must match a random token** the listener writes at
     startup to `~/.fusion_mcp/listener_token` (user-only permissions; a new
     token every time the add-in starts). The server reads that file on every
     request. A web page can't read the file, and a custom header forces a CORS
     preflight that the listener never approves — so a page can't even send
     the request.
  `GET /ping` skips the token (liveness and version only, no document data), so
  the preflight and a stale server can still diagnose a version mismatch.
- **Other local users** can't read your token file, so they can't drive your
  Fusion session either. A process running as *you* can, but it could already
  do anything you can.
- **Do not** change the bind address to `0.0.0.0` or forward the port.
- The dangerous-call refusals in `_DANGEROUS_PATTERNS` are a **stability**
  guard against host crashes, not a security boundary.

## Extending the toolset

Add a new tool by writing a function in `fusion_mcp_server.py` decorated with
`@mcp.tool()`. Build a Fusion API snippet as a string, `print()` whatever you
want Claude to see, and return `_send(code)`. Patterns to copy:

- **Take mm, convert to cm** at the top (`x = x_mm / 10.0`).
- **`print()` a clear success line** — that string is what Claude reads back.
- **Resolve components with `find_component(name)` / `find_occurrence(name)`**
  (injected helpers). They raise with a list of valid names on a miss; never
  loop over occurrences yourself and fall back to the root.
- **Validate arguments server-side** and return an `ERROR: ...` string before
  generating any code, rather than defaulting an unknown value silently.
- **Report failure with a marker** (`ERROR`, `WARNING`, `***`) — `run_batch`
  stops on those, and terse mode never hides them.
- If the tool is a pure geometry/parameter operation, add it to `BATCHABLE`.
- Run `python -m pytest`: it compiles every tool's generated code, so a quoting
  or `.format()` slip in a template fails the build instead of failing inside
  Fusion.

Restart Claude Desktop after editing the server so it reloads the tool list.
If you change the wire format, bump `PROTOCOL` in `MCPListener.py` **and**
`EXPECTED_PROTOCOL` in the server; bump the version in both for every release
(`tests/test_versions.py` enforces it).

## Files

| File | Runs where | Role |
|------|-----------|------|
| `MCPListener.py` | Inside Fusion | HTTP listener + main-thread executor |
| `fusion_mcp_server.py` | Spawned by Claude Desktop | MCP tools → HTTP relay |
| `launch.py` | Your terminal | Preflight/health checks only |
| `tests/` | Your terminal / CI | Test suite — needs no Fusion (`python -m pytest`) |
