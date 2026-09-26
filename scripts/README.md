# scripts

| Script | Who runs it | What it does |
|---|---|---|
| `launch_mcp.py` | The Claude plugin, through `.mcp.json` | Starts the package's MCP server over stdio. It does nothing else. |
| `verify_release.py` | Maintainers and CI | Runs formatting, lint, type and test checks, builds the wheel and sdist, installs each into a fresh environment and runs the tests against it. It copies the environment only to remove `PYTHONPATH` and test-selection variables before starting those checks. The plugin never runs it, and it never publishes anything. |

See [SECURITY.md](../SECURITY.md) for the plugin's full runtime behaviour.
