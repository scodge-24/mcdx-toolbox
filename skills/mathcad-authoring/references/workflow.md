# Worksheet workflow and host setup

## Choose the execution host

The package, import name and CLI are `mcdx-toolbox`, `pymcdx` and `pymcdx`
respectively. Use an installed CLI, or prefix commands with
`uv run --project <plugin-directory> --locked` when using the plugin checkout.
CLI stdout and stderr are UTF-8, including when redirected on Windows.

On native Windows, install Windows Python/uv and use Windows paths. Under WSL,
install Linux Python/uv and use Linux paths. Prime must be on the same Windows
machine with `powershell.exe`, `cmd.exe` and `wslpath` available. Cloud Linux
cannot reach a user's local Prime COM server. Installing a plugin into a separate
runtime does not automatically give it access to the desktop host.

## Inspect, import and build

For an existing worksheet, preserve the original and review both content and
formatting before importing:

```bash
pymcdx inspect input.mcdx --outline --full
pymcdx inspect input.mcdx --formatting
pymcdx import-yaml input.mcdx -o editable.yaml
pymcdx build editable.yaml revised.mcdx --report build.json --preview layout.svg
pymcdx audit revised.mcdx
pymcdx layout-check revised.mcdx --strict
```

Read unsupported-region and approximation warnings. Import is not a lossless
round-trip for arbitrary Prime files. Edit the semantic YAML using the
[authoring reference](authoring.md). Python-to-Mathcad generation accepts a
supported syntax subset without executing the input Python; Mathcad-to-Python
transpilation is not included.

Bundled schema checks use project-authored, deliberately incomplete schemas.
Stored region boxes and SVG layout estimates do not prove Prime typography,
format acceptance or numerical correctness.

## Render and review

```bash
pymcdx render revised.mcdx -o render --resave prime-resaved.mcdx --pdf worksheet.pdf
pymcdx render-diff old-render render -o differences
```

Follow [Prime render visual checks](prime-render-visual-check.md) and inspect
every page. The optional PDF assembles raster images; its text is not searchable.
Prime/Express rendering may open a visible window. Render DPI is 36–600 and
timeout is finite, up to 3600 seconds.

Automation uses a staged copy and closes only that worksheet. Never hide, quit
or kill Prime: COM can attach to the user's session, and PID differences do not
prove ownership. On timeout the owned PowerShell worker stops, recovery files
remain at the reported path, and Prime stays open. Let the user resolve remaining
dialogs. Do not automate application cleanup.

## MCP and plugin execution

`pymcdx mcp` starts a stdio server exposing `inspect_worksheet`,
`validate_worksheet`, `generate_worksheet`, `build_worksheet`, `import_worksheet`,
`layout_check`, `layout_preview`, `render_worksheet` and `compare_renders`.
`render_worksheet` optionally produces a Prime-resaved worksheet and raster PDF.
Paths refer to the machine running the server, not a remote chat client.

Claude Code loads the checkout with `claude --plugin-dir <absolute-checkout-path>`.
Install uv on the host running Claude. The bundle's `.mcp.json` uses the checkout
and committed lockfile, not an unpublished package from a registry. First launch
may download dependencies. Review local filesystem permissions: tools read and
write the files requested by the caller.

Local plugin MCP is supported in Claude Code and Cowork, not normal Claude chat.
Skill availability and host permissions vary. Cowork-to-Windows COM integration
has not been validated here; do not assume its sandbox can access local Prime.
For plugin distribution checks rather than worksheet use, see
[Release validation](releasing.md).
