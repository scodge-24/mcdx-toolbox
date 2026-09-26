# Security and runtime behaviour

This page states what mcdx-toolbox does on your machine, so users and plugin
directory reviewers can check it against the code. Automated scanners have
flagged some of the patterns below. Each section explains what the flagged code
actually does.

## Summary

| Question | Answer |
|---|---|
| Does it use the network at runtime? | No. The package has no networking code. |
| Does it read environment variables at runtime? | No. Only the maintainer's test script and the tests do. |
| Does it run other programs? | Only for rendering: PowerShell and `cmd` on your own Windows machine, to drive your installed Mathcad Prime. |
| What files does it write? | The files you or your agent ask for, plus temporary files it creates and removes. |
| Does it download anything? | Only at install time: `uv` fetches the dependencies pinned in `uv.lock`. |

## Installation and launch

The Claude plugin starts its MCP server with:

```text
uv run --project ${CLAUDE_PLUGIN_ROOT} --locked --no-dev python ${CLAUDE_PLUGIN_ROOT}/scripts/launch_mcp.py
```

- `uv.lock` pins every dependency to an exact version with checksums.
  `uv run` installs exactly what the lockfile lists, and `--locked` makes it
  refuse to run if the lockfile is out of date rather than re-resolving, so no
  dependency floats to a newer release at install time.
- On first launch `uv` downloads those pinned packages from PyPI, and may
  download a Python 3.12 interpreter if none is installed. Later launches reuse
  them.
- `--no-dev` leaves out development tools (test, lint and type-check packages).
- [`scripts/launch_mcp.py`](scripts/launch_mcp.py) is a readable eleven-line
  entrypoint that starts the package's own MCP server over stdio.

The dependencies can't be copied into the plugin instead: lxml, Pillow and
pypdfium2 ship compiled binaries for each platform.

## Network

The package contains no networking code: no `socket`, `urllib`, `http.client`,
`requests` or `httpx`. The MCP server talks to its client over standard
input/output only.

Strings such as `http://schemas.mathsoft.com/math50` and
`http://schemas.microsoft.com/winfx/2006/xaml/presentation` in
`pymcdx/ast_converter.py`, `pymcdx/authoring_import.py` and elsewhere are **XML
namespace names**. They are labels that Mathcad Prime writes into every
worksheet to identify its XML vocabulary. They are never fetched, and changing
them would produce files Prime can't read.

## Environment variables

The package reads no environment variables at runtime.
[`scripts/verify_release.py`](scripts/verify_release.py) is a maintainer script
that the plugin never runs. It copies the environment only to remove
`PYTHONPATH` and test-selection variables before running the test suite against
freshly built packages. The tests read `PYMCDX_TEST_*` variables to choose
which installed command to test and whether to run the optional Prime tests.

## Code execution

The toolbox never executes code from worksheets or YAML files.

- `pymcdx/ast_converter.py` uses Python's `ast.parse` to read an expression's
  syntax tree and convert it to Mathcad XML. The tree is never compiled or run.
- `pymcdx/authoring_import.py` uses `ast.literal_eval` only to read a numeric
  literal from worksheet XML. That function accepts literals and nothing
  executable.
- `generate` converts a supported subset of Python syntax to a worksheet in the
  same way, without running the Python.

## Processes and files

Rendering (`pymcdx render`, or the `render_worksheet` MCP tool) is the only
feature that starts other programs, and only on Windows or WSL on the same
machine:

- `powershell.exe` runs the bundled
  [`pymcdx/prime/render.ps1`](pymcdx/prime/render.ps1), which opens a staged
  copy of the worksheet in your installed Mathcad Prime through its automation
  interface and exports the pages.
- Under WSL, `wslpath` and `cmd.exe` convert paths and find the Windows
  temporary folder.

The staged copy lives in a new `pymcdx-render-*` temporary folder. The renderer
never hides, closes or kills a Prime session it didn't open. On a timeout it
leaves the staging folder in place for recovery and reports where it is.

The CLI and MCP tools read and write only the paths the caller supplies, plus
short-lived temporary files next to the outputs. Paths refer to the machine
running the server.

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/scodge-24/mcdx-toolbox/security/advisories/new),
not as a public issue. For ordinary bugs and feature requests, use the
[issue forms](https://github.com/scodge-24/mcdx-toolbox/issues/new/choose).
