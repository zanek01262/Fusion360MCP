# Publishing to GitHub

Quick steps to put this online so friends can clone it.

## One-time setup

From inside the `fusion-mcp/` folder:

```bash
git init
git add .
git commit -m "Initial release: Fusion 360 MCP v1.0.0"
```

Create an empty repo on GitHub (no README/license — this package has its own),
then:

```bash
git branch -M main
git remote add origin https://github.com/<your-username>/fusion-mcp.git
git push -u origin main
```

## Before you push — check for personal paths

The code itself contains no personal paths, but double-check you haven't pasted
your real `claude_desktop_config.json` (with your username) into the repo. The
`.gitignore` already excludes `fusion_view.png`, `__pycache__`, and
`claude_desktop_config.local.json`. The config lives in Claude Desktop's own
directory, not here, so it won't be committed unless you copy it in.

## Suggested repo description

> Drive Autodesk Fusion 360 from Claude via MCP — parametric parts, assemblies,
> joints, tapped holes, export, and a visual feedback loop.

## Suggested topics/tags

`fusion360` `mcp` `model-context-protocol` `claude` `cad` `autodesk`
`automation` `parametric-design`

## A note for contributors

If people send PRs, the easiest things to review are new `@mcp.tool()` functions
that follow the existing patterns (mm in, cm to the API, print a clear result,
guard lookups). See `docs/ARCHITECTURE.md` → "Extending the toolset."

## Releases

Tag versions so people can grab a stable zip:

```bash
git tag -a v1.0.0 -m "v1.0.0"
git push origin v1.0.0
```

GitHub will auto-generate a downloadable archive for each tag under "Releases."
