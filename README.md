# mcdx-toolbox

[![CI](https://github.com/scodge-24/mcdx-toolbox/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/scodge-24/mcdx-toolbox/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Inspect, create and edit Mathcad Prime `.mcdx` worksheets from Python, the
command line or an AI agent. Includes semantic YAML authoring, an MCP server
and a Claude plugin with a Mathcad authoring skill.

## What it does

- Inspect worksheet content and formatting; extract images and import supported content as YAML.
- Build worksheets from YAML or supported Python syntax.
- Validate package structure and check layout with SVG previews.
- With local Mathcad Prime/Express: render pages, resave worksheets, compare renders and export raster PDFs.

Core tools run on Python 3.12+ on Linux, macOS and Windows without Mathcad.
Prime integration requires Windows or same-host WSL. Layout previews are estimates;
real Prime renders establish presentation, not independent numerical verification.

## Quick start

Install [uv](https://docs.astral.sh/uv/), then from a cloned checkout:

```bash
uv sync --locked
uv run pymcdx build examples/basic.yaml example.mcdx --preview example.svg
uv run pymcdx inspect example.mcdx --outline --full
uv run pymcdx --help
```

To install the CLI separately, use `uv tool install .`; for a Python environment,
use `pip install .`. The command and Python import name are `pymcdx`.
Installation is currently from source; no PyPI release is available.

## Use with an agent

Load the bundled skill and MCP tools in Claude Code:

```bash
claude --plugin-dir /absolute/path/to/mcdx-toolbox
```

Install uv on the same host as Claude Code. For other MCP clients, configure
`pymcdx mcp` as a local stdio server. The package and CLI work independently of
Claude.

See the [Mathcad authoring skill](skills/mathcad-authoring/SKILL.md) for the
workflow and its references for [YAML authoring](skills/mathcad-authoring/references/authoring.md),
[host/MCP setup](skills/mathcad-authoring/references/workflow.md) and
[Prime visual review](skills/mathcad-authoring/references/prime-render-visual-check.md).

## Scope

Imports report unsupported content; arbitrary worksheets are not guaranteed
lossless round-trips. This is worksheet tooling, not a standalone Mathcad
calculation engine. Engineering calculations still require independent verification.

## Contributing

Run `uv run --locked python scripts/verify_release.py` for lint, types, tests and
isolated wheel/sdist checks. See [development and release guidance](skills/mathcad-authoring/references/releasing.md)
for plugin validation and opt-in Prime tests.

## Licence

Original code is [MIT licensed](LICENSE). See [NOTICE](NOTICE.md) for font-derived
data and dependency notices. Not affiliated with or endorsed by PTC, Mathcad or Anthropic.
