"""Exercise the directory-visible command from a relocated plugin checkout."""

import json
import os
import shutil
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.skipif(
    bool(os.environ.get("PYMCDX_TEST_COMMAND")),
    reason="Plugin checkout launch is separate from installed wheel/sdist verification",
)
def test_plugin_manifest_starts_from_unrelated_directory(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    plugin = tmp_path / "plugin with spaces"
    shutil.copytree(
        root,
        plugin,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "build",
            "dist",
            "*.egg-info",
        ),
    )
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    config = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["mcdx-toolbox"]
    command = shutil.which(server["command"])
    assert command is not None
    args = [arg.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)) for arg in server["args"]]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UV_") and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    protocol_errors: list[Exception] = []

    async def record_protocol_errors(message: object) -> None:
        if isinstance(message, Exception):
            protocol_errors.append(message)

    async def check() -> None:
        with anyio.fail_after(120):
            params = StdioServerParameters(command=command, args=args, cwd=str(cwd), env=env)
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write, message_handler=record_protocol_errors) as session,
            ):
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"inspect_worksheet", "validate_worksheet", "build_worksheet"} <= names
                assert "eval_worksheet" not in names
                assert not protocol_errors

    anyio.run(check)
