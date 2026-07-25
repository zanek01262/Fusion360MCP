#!/usr/bin/env python3
"""
launch.py — Fusion 360 MCP preflight & helper
=============================================

What this does (and what it can't):

  This setup has TWO processes that are NOT started from a shell:
    1. fusion_mcp_server.py  -> launched automatically by Claude Desktop
                                (stdio). You don't run it yourself normally.
    2. MCPListener           -> runs INSIDE Fusion 360 as an add-in.
                                It cannot be started from a terminal.

  So this script does the useful things a launcher *can* do:
    - check    : verify Python, deps, the listener, and Claude config
    - ping     : just ping the Fusion listener
    - serve    : run the MCP server standalone for DEBUGGING (not needed for
                 normal Claude Desktop use; useful with MCP Inspector)

Usage:
    python3 launch.py check     # full preflight (default if no arg)
    python3 launch.py ping      # quick listener ping
    python3 launch.py serve     # run server in foreground for debugging
"""

import json
import os
import sys
import subprocess

LISTENER_URL = "http://127.0.0.1:8765"
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "fusion_mcp_server.py")

# ---- tiny ANSI helpers (no deps) ----
def _c(code, s):
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return s
    return "\033[{}m{}\033[0m".format(code, s)

def ok(s):   return _c("32", "OK   ") + s
def warn(s): return _c("33", "WARN ") + s
def bad(s):  return _c("31", "FAIL ") + s
def head(s): return _c("1;36", s)


def check_python():
    v = sys.version_info
    if v >= (3, 10):
        print(ok("Python {}.{}.{} ({})".format(v.major, v.minor, v.micro, sys.executable)))
        return True
    print(bad("Python {}.{}.{} — need 3.10+".format(v.major, v.minor, v.micro)))
    return False


def check_deps():
    all_ok = True
    try:
        import mcp  # noqa: F401
        print(ok("mcp SDK importable"))
    except Exception as e:
        print(bad("mcp not importable: {}".format(e)))
        print("       fix: pip install \"mcp[cli]\" requests")
        all_ok = False
    try:
        import requests  # noqa: F401
        print(ok("requests importable"))
    except Exception as e:
        print(bad("requests not importable: {}".format(e)))
        all_ok = False
    return all_ok


def check_server_file():
    if os.path.exists(SERVER):
        print(ok("server file found: {}".format(SERVER)))
        return True
    print(bad("server file missing: {}".format(SERVER)))
    return False


def check_listener():
    try:
        import requests
    except Exception:
        print(warn("can't check listener (requests missing)"))
        return False
    try:
        r = requests.get(LISTENER_URL, timeout=5)
        msg = r.json().get("output", "")
        print(ok("Fusion listener reachable — {}".format(msg)))
        return True
    except Exception:
        print(bad("Fusion listener NOT reachable at {}".format(LISTENER_URL)))
        print("       fix: open Fusion 360 -> Shift+S -> Add-Ins ->")
        print("            select MCPListener -> Run")
        return False


def _claude_config_path():
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/claude_desktop_config.json")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", ""),
                            "Claude", "claude_desktop_config.json")
    # linux / other
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def check_claude_config():
    path = _claude_config_path()
    if not os.path.exists(path):
        print(warn("Claude Desktop config not found at {}".format(path)))
        print("       (fine if you haven't set it up yet — see README Part C)")
        return False
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception as e:
        print(bad("Claude config exists but won't parse: {}".format(e)))
        return False
    servers = cfg.get("mcpServers", {})
    if "fusion360" in servers:
        entry = servers["fusion360"]
        args = entry.get("args", [])
        configured = args[0] if args else "(none)"
        print(ok("Claude config has 'fusion360' -> {}".format(configured)))
        if os.path.abspath(configured) != os.path.abspath(SERVER):
            print(warn("   configured path differs from this folder's server:"))
            print("        configured: {}".format(configured))
            print("        this file : {}".format(SERVER))
        return True
    print(warn("Claude config found but no 'fusion360' server entry"))
    print("       see README Part C")
    return False


def cmd_check():
    print(head("\n== Fusion 360 MCP preflight ==\n"))
    print(head("Local environment"))
    p = check_python()
    d = check_deps()
    s = check_server_file()
    print(head("\nClaude Desktop"))
    check_claude_config()
    print(head("\nFusion listener"))
    listener = check_listener()

    print(head("\nSummary"))
    core = p and d and s
    if core and listener:
        print(ok("All systems go. Ask Claude: \"Ping Fusion, then make a 20mm cube and show me.\""))
    elif core and not listener:
        print(warn("Local setup good; just start the MCPListener add-in in Fusion."))
    else:
        print(bad("Fix the FAIL items above, then re-run: python3 launch.py check"))
    print()
    return 0 if (core and listener) else 1


def cmd_ping():
    return 0 if check_listener() else 1


def cmd_serve():
    # Run the MCP server in the foreground. Normal Claude Desktop use does NOT
    # need this — Claude launches the server itself. Use this for debugging,
    # e.g. with:  npx @modelcontextprotocol/inspector python3 launch.py serve
    if not check_server_file():
        return 1
    print(head("Running MCP server standalone (Ctrl+C to stop)…"), file=sys.stderr)
    print("Note: over stdio this just waits for JSON-RPC; that's expected.",
          file=sys.stderr)
    # exec the server with the same interpreter
    os.execv(sys.executable, [sys.executable, SERVER])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        sys.exit(cmd_check())
    elif cmd == "ping":
        sys.exit(cmd_ping())
    elif cmd == "serve":
        sys.exit(cmd_serve())
    else:
        print("usage: python3 launch.py [check|ping|serve]")
        sys.exit(2)


if __name__ == "__main__":
    main()
