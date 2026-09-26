"""Synthetic public workflows through CLI processes and the real stdio protocol."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

EXAMPLE = Path(
    os.environ.get(
        "PYMCDX_TEST_EXAMPLE", str(Path(__file__).resolve().parents[1] / "examples" / "basic.yaml")
    )
)


def _command() -> list[str]:
    executable = os.environ.get("PYMCDX_TEST_COMMAND")
    return [executable] if executable else [sys.executable, "-m", "pymcdx.worksheet_cli"]


def _cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [*_command(), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict"},
        check=False,
    )


def test_cli_build_inspect_import_and_private_command_refusal(tmp_path: Path) -> None:
    folder = tmp_path / "worksheet with spaces"
    folder.mkdir()
    sheet = folder / "basic.mcdx"
    preview = folder / "layout.svg"
    result = _cli("build", str(EXAMPLE), str(sheet), "--preview", str(preview), cwd=folder)
    assert result.returncode == 0, result.stderr
    assert sheet.is_file() and "<svg" in preview.read_text(encoding="utf-8")
    inspected = _cli("inspect", str(sheet), "--outline", "--full", cwd=folder)
    assert inspected.returncode == 0, inspected.stderr
    assert "width" in inspected.stdout and "height" in inspected.stdout
    for command in ("audit", "layout-check"):
        checked = _cli(command, str(sheet), cwd=folder)
        assert checked.returncode == 0, checked.stderr
    imported = folder / "editable.yaml"
    result = _cli("import-yaml", str(sheet), "-o", str(imported), cwd=folder)
    assert result.returncode == 0, result.stderr
    assert "width" in imported.read_text(encoding="utf-8")
    before = sheet.read_bytes()
    for command in ("eval", "to-python", "check-values", "englib-scaffold"):
        refused = _cli(command, str(sheet), cwd=folder)
        assert refused.returncode == 2
        assert "invalid choice" in refused.stderr
    assert sheet.read_bytes() == before


def _structured(result: CallToolResult) -> dict[str, object]:
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


async def _stdio_workflow(folder: Path) -> None:
    env = {**os.environ, "PATH": ""}  # no Prime/Windows launcher in this transport test
    parameters = StdioServerParameters(
        command=_command()[0],
        args=[*_command()[1:], "mcp"],
        cwd=str(folder),
        env=env,
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert names == {
            "inspect_worksheet",
            "validate_worksheet",
            "generate_worksheet",
            "build_worksheet",
            "import_worksheet",
            "layout_check",
            "layout_preview",
            "render_worksheet",
            "compare_renders",
        }
        sheet = folder / "mcp.mcdx"
        result = _structured(
            await session.call_tool(
                "build_worksheet",
                {
                    "spec_path": str(EXAMPLE),
                    "output_path": str(sheet),
                },
            )
        )
        assert result["output_path"] == str(sheet)
        linked = folder / "linked-preview.svg"
        os.link(sheet, linked)
        original = sheet.read_bytes()
        refused = await session.call_tool(
            "layout_preview", {"path": str(sheet), "output_path": str(linked)}
        )
        assert refused.isError
        assert "must differ" in str(refused.content)
        assert sheet.read_bytes() == original
        valid = _structured(await session.call_tool("validate_worksheet", {"path": str(sheet)}))
        assert valid["valid"] is True
        outline = _structured(await session.call_tool("inspect_worksheet", {"path": str(sheet)}))
        assert "width" in str(outline["regions"])
        layout = _structured(await session.call_tool("layout_check", {"path": str(sheet)}))
        assert layout["findings"] == []
        preview = folder / "mcp.svg"
        _structured(
            await session.call_tool(
                "layout_preview",
                {
                    "path": str(sheet),
                    "output_path": str(preview),
                },
            )
        )
        assert "<svg" in preview.read_text(encoding="utf-8")
        imported = folder / "mcp.yaml"
        _structured(
            await session.call_tool(
                "import_worksheet",
                {
                    "path": str(sheet),
                    "output_path": str(imported),
                },
            )
        )
        assert imported.is_file()
        source = folder / "synthetic.py"
        source.write_text("width = 3\nheight = 4\narea = width * height\n", encoding="utf-8")
        generated = _structured(
            await session.call_tool(
                "generate_worksheet",
                {
                    "python_path": str(source),
                    "output_path": str(folder / "generated.mcdx"),
                },
            )
        )
        assert generated["validated"] is True
        bad = folder / "bad.mcdx"
        bad.write_bytes(b"not a package")
        invalid = _structured(await session.call_tool("validate_worksheet", {"path": str(bad)}))
        assert invalid["valid"] is False
        assert invalid["errors"]
        unavailable = await session.call_tool(
            "render_worksheet",
            {
                "path": str(sheet),
                "output_dir": str(folder / "render"),
            },
        )
        assert unavailable.isError
        assert "Windows" in str(unavailable.content)
        from PIL import Image

        for directory, color in (("old", "white"), ("new", "black")):
            (folder / directory).mkdir()
            Image.new("RGB", (20, 20), color).save(folder / directory / "page-01.png")
        compared = _structured(
            await session.call_tool(
                "compare_renders",
                {
                    "old_dir": str(folder / "old"),
                    "new_dir": str(folder / "new"),
                    "output_dir": str(folder / "diff"),
                },
            )
        )
        assert compared["changed"] is True
        assert (folder / "diff" / "page-01.png").is_file()


def test_stdio_public_workflow(tmp_path: Path) -> None:
    anyio.run(_stdio_workflow, tmp_path)
