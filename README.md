# mcdx-toolbox

[![CI](https://github.com/scodge-24/mcdx-toolbox/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/scodge-24/mcdx-toolbox/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Agent-oriented tooling for Mathcad Prime `.mcdx` worksheets: inspect, create,
modify, validate, import/export and visually verify worksheets, with optional
integration with a locally installed copy of Mathcad Prime.

The Python package and CLI work independently of Claude. A Claude plugin bundle
adds the same local MCP tools and a Mathcad authoring skill.

## Install

This is a source release candidate; no PyPI publication is assumed. From this
checkout, install Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked
uv run pymcdx --help
uv run pymcdx build examples/basic.yaml example.mcdx --preview example.svg
```

For an isolated CLI installation, run `uv tool install .` or `pipx install .`.
For a Python environment, use `pip install .` or install the built wheel. The
distribution name is `mcdx-toolbox`; the import name and command are `pymcdx`.
CLI stdout and stderr are UTF-8, including when redirected on Windows.
Registry name availability is not a reservation or publication guarantee.

## Capabilities and evidence

| Capability | Requirements | What it establishes |
|---|---|---|
| Read, inspect, extract, generate, semantic authoring/import | Python on Linux, macOS or Windows | Package contents and authored document structure |
| Bundled schema validation | No Mathcad installation | Checks against project-authored, deliberately incomplete format schemas |
| Layout checking and SVG preview | No Mathcad installation | Estimated geometry, not Prime typography or numerical correctness |
| Prime open/synchronise/resave, PNG render | Windows with Prime/Express; native Python or same-host WSL | Actual Prime format/presentation behaviour |
| Render comparison and raster PDF | PNG pages; actual Prime needed to produce Prime renders | Pixel differences or image-only PDF assembly |

Schema-valid does not necessarily mean Prime-valid. The real Prime render is the
presentation acceptance oracle. Inspect every relevant rendered page before
claiming visual acceptance. Express licence limitations may show red regions or
`= ?` for premium features; distinguish those from malformed-file/repair errors.

There is no independent Mathcad calculation engine in this distribution. Licensed
Prime numerical evaluation is not exposed as a verification service. Generated
engineering calculations require appropriate independent engineering verification.

## Worksheet workflow

```bash
pymcdx inspect input.mcdx --outline --full
pymcdx inspect input.mcdx --formatting
pymcdx import-yaml input.mcdx -o editable.yaml
pymcdx build editable.yaml revised.mcdx --report build.json --preview layout.svg
pymcdx audit revised.mcdx
pymcdx layout-check revised.mcdx --strict
pymcdx render revised.mcdx -o render --resave prime-resaved.mcdx --pdf worksheet.pdf
pymcdx render-diff old-render render -o differences
```

Import reports unsupported regions explicitly; it is not a lossless arbitrary
Prime round-trip. Keep originals. See [authoring](docs/authoring.md) for the public
YAML schema. Python-to-Mathcad generation accepts a supported syntax subset and
does not execute input Python. Mathcad-to-Python transpilation is not included.

Prime automation always operates on a staged copy. It does not hide, quit or kill
Prime, because COM may attach to a user's session and PID differences do not prove
ownership. Only the staged worksheet is closed. On timeout, the owned PowerShell
worker stops and recovery files remain at the path in the error; resolve any Prime
prompt manually. Render DPI is 36–600 and timeout is finite, up to 3600 seconds.
Rendering may open a visible Prime window. Do not automate application cleanup.

## MCP and Claude plugin

Run `pymcdx mcp` for an MCP stdio server. Tools: `inspect_worksheet`,
`validate_worksheet`, `generate_worksheet`, `build_worksheet`, `import_worksheet`,
`layout_check`, `layout_preview`, `render_worksheet` and `compare_renders`.
`render_worksheet` optionally writes a Prime-resaved worksheet and raster PDF.
File paths refer to the machine running the server, not a remote chat client.

For Claude Code, test with `claude --plugin-dir <absolute-checkout-path>`.
The bundle uses `.claude-plugin/plugin.json`, root `.mcp.json`, and `skills/`.
Install uv on the host running Claude. The launcher uses this checkout and its
committed lockfile; it does not fetch a similarly named unpublished PyPI package.
First launch may download Python packages. Review the source and local filesystem
permissions before enabling this local server; it reads and writes requested files.

On native Windows, install Windows Python/uv and use Windows paths. Under WSL,
install Linux Python/uv and use Linux paths; Prime must be on that same Windows
machine with `powershell.exe`, `cmd.exe`, and `wslpath` available. Cloud Linux
cannot reach a user's local Prime COM server. A plugin installed into a different
runtime does not automatically cross that boundary.

Local plugin MCP is supported in Claude Code and Cowork, not normal Claude chat.
Skill availability and host permissions vary; Cowork-to-Windows COM integration
is not validated here. Do not assume a sandbox can access the desktop host.
See [plugin/release validation](docs/releasing.md) for the distinction between
local validation and Anthropic directory review. This project is not affiliated
with or endorsed by PTC, Mathcad or Anthropic.

## Development

```bash
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -m "not prime"
uv build
claude plugin validate .
```

Prime tests are opt-in (`PYMCDX_TEST_PRIME=1 uv run pytest -m prime`) and otherwise
skip. A passing no-Prime CI run does not establish Windows/Prime acceptance.
The public tree has its own CI and synthetic examples; it needs no other checkout.

Original code is MIT licensed. Font-derived measurement data retain OFL 1.1
notices; dependencies retain their own licences. See [NOTICE](NOTICE.md).
