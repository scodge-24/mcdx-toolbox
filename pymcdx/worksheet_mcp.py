"""FastMCP worksheet tools for the :mod:`pymcdx` command surface."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import NoReturn

from lxml import etree
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, ValidationError

from pymcdx import layout, worksheet_cli
from pymcdx.authoring import AuthoringBuildError, build_authoring_document
from pymcdx.authoring_import import import_worksheet as import_sheet
from pymcdx.authoring_report import BuildReport
from pymcdx.prime_render import render_pages, worksheet_title, write_pdf
from pymcdx.render_compare import compare_renders as compare_pages
from pymcdx.validation import McdxValidationError, validate_mcdx_file


class _StrictResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class WorksheetRegion(_StrictResult):
    """One top-level region in the worksheet outline."""

    region_id: str
    kind: str
    summary: str | None = None


class WorksheetOutline(_StrictResult):
    """Structured output for :func:`inspect_worksheet`."""

    regions: list[WorksheetRegion]


class ValidationResult(_StrictResult):
    """Schema validation verdict and actionable validation diagnostics."""

    valid: bool
    errors: list[str]


class GenerateResult(_StrictResult):
    output_path: str
    validated: bool


class BuildWorksheetResult(_StrictResult):
    """Structured result from the semantic YAML build service."""

    output_path: str
    report: BuildReport
    report_path: str | None = None
    preview_path: str | None = None


class LayoutFinding(_StrictResult):
    region: str
    check: str
    message: str


class LayoutSummary(_StrictResult):
    paper: str
    orientation: str
    content_width_px: float
    content_height_px: float
    margins_confirmed: bool
    region_count: int
    findings: list[LayoutFinding]


class McpToolError(RuntimeError):
    """An actionable, one-line error suitable for an MCP tool response."""


mcp = FastMCP("pymcdx")


def _one_line(detail: str) -> str:
    return " ".join(detail.split())


def _existing_file(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.exists():
        raise McpToolError(f"Error: {path} not found.")
    if not path.is_file():
        raise McpToolError(f"Error: {path} is not a file.")
    return path


def _package_error(path: Path, exc: Exception) -> McpToolError:
    detail = _one_line(str(exc))
    if isinstance(exc, zipfile.BadZipFile):
        return McpToolError(f"Error: {path} is not a valid OPC package (corrupt or not a zip).")
    if isinstance(exc, KeyError):
        return McpToolError(f"Error: {path} is missing an expected part: {detail}")
    if isinstance(exc, etree.XMLSyntaxError):
        return McpToolError(f"Error: {path} contains malformed XML: {detail}")
    return McpToolError(f"Error: could not read {path}: {detail or type(exc).__name__}")


def _unsupported_error(path: Path, detail: str) -> McpToolError:
    return McpToolError(f"Error: unsupported worksheet input {path}: {_one_line(detail)}")


def _raise_unsupported(path: Path, exc: Exception) -> NoReturn:
    raise _unsupported_error(path, str(exc)) from exc


@mcp.tool()
def inspect_worksheet(path: str) -> WorksheetOutline:
    """Return worksheet regions and rendered summaries for math regions."""
    worksheet_path = _existing_file(path)
    try:
        root = worksheet_cli._read_worksheet_xml(worksheet_path)
        regions = worksheet_cli._get_regions(root)
        outline = []
        for region in regions:
            region_id = region.get("region-id", "?")
            kind, content, _ = worksheet_cli._classify_region(region, worksheet_path)
            outline.append(
                WorksheetRegion(
                    region_id=region_id,
                    kind=kind,
                    summary=content,
                )
            )
        return WorksheetOutline(regions=outline)
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError, OSError) as exc:
        raise _package_error(worksheet_path, exc) from exc


@mcp.tool()
def validate_worksheet(path: str) -> ValidationResult:
    """Check package structure against project-authored schemas; not Prime acceptance."""
    worksheet_path = _existing_file(path)
    try:
        validate_mcdx_file(worksheet_path)
    except McdxValidationError as exc:
        errors = list(exc.errors) or [_one_line(str(exc))]
        return ValidationResult(valid=False, errors=errors)
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError, OSError) as exc:
        return ValidationResult(valid=False, errors=[str(_package_error(worksheet_path, exc))])
    return ValidationResult(valid=True, errors=[])


@mcp.tool()
def generate_worksheet(python_path: str, output_path: str) -> GenerateResult:
    """Generate a worksheet from Python and validate the resulting package."""
    source_path = Path(python_path)
    if not source_path.exists():
        raise McpToolError(f"Error: Input file {source_path} not found.")
    if not source_path.is_file():
        raise McpToolError(f"Error: Input file {source_path} is not a file.")
    destination = Path(output_path)
    try:
        worksheet_cli.generate_mcdx(source_path, destination)
    except (NotImplementedError, SyntaxError, TypeError, ValueError) as exc:
        raise _unsupported_error(source_path, str(exc)) from exc

    try:
        validate_mcdx_file(destination)
    except McdxValidationError:
        return GenerateResult(output_path=str(destination), validated=False)
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError, OSError) as exc:
        raise _package_error(destination, exc) from exc
    return GenerateResult(output_path=str(destination), validated=True)


@mcp.tool()
def build_worksheet(
    spec_path: str,
    output_path: str,
    report_path: str | None = None,
    preview_path: str | None = None,
) -> BuildWorksheetResult:
    """Build a semantic YAML worksheet with the CLI's transactional service."""
    try:
        report = build_authoring_document(
            Path(spec_path),
            Path(output_path),
            report_path=Path(report_path) if report_path is not None else None,
            preview_path=Path(preview_path) if preview_path is not None else None,
        )
    except AuthoringBuildError as exc:
        raise McpToolError(f"Error: {exc.code} at {exc.path}: {exc.detail}") from exc
    return BuildWorksheetResult(
        output_path=output_path,
        report=report,
        report_path=report_path,
        preview_path=preview_path,
    )


@mcp.tool()
def layout_check(path: str) -> LayoutSummary:
    """Return the existing Mathcad-free worksheet layout lint JSON."""
    worksheet_path = _existing_file(path)
    try:
        summary = layout.summarize(worksheet_path)
        return LayoutSummary.model_validate(summary)
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError, OSError) as exc:
        raise _package_error(worksheet_path, exc) from exc
    except (TypeError, ValueError, ValidationError) as exc:
        _raise_unsupported(worksheet_path, exc)


class ImportWorksheetResult(_StrictResult):
    output_path: str
    images: list[str]
    unsupported: list[str]
    warnings: list[str]


class FileResult(_StrictResult):
    output_path: str


class RenderWorksheetResult(_StrictResult):
    xps_path: str
    pages: list[str]
    pdf_path: str | None = None
    resaved_path: str | None = None


class CompareRendersResult(_StrictResult):
    changed: bool
    pages: list[str]


@mcp.tool()
def import_worksheet(path: str, output_path: str, force: bool = False) -> ImportWorksheetResult:
    """Import a worksheet as editable semantic YAML; report unsupported regions."""
    result = import_sheet(_existing_file(path), Path(output_path), force=force)
    return ImportWorksheetResult(
        output_path=str(result.output),
        images=[str(p) for p in result.images],
        unsupported=[p.describe() for p in result.unsupported],
        warnings=list(result.warnings),
    )


@mcp.tool()
def layout_preview(path: str, output_path: str) -> FileResult:
    """Write an estimated SVG layout preview. This is not a Prime render."""
    source = _existing_file(path)
    destination = Path(output_path)
    if destination.resolve() == source.resolve() or (
        destination.exists() and destination.samefile(source)
    ):
        raise McpToolError("preview output must differ from worksheet input")
    page = layout.load_page(source)
    svg = layout.render_svg(page, layout.load_regions(source, page=page))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(svg, encoding="utf-8")
    return FileResult(output_path=str(destination))


@mcp.tool()
def render_worksheet(
    path: str,
    output_dir: str,
    dpi: int = 110,
    timeout: float = 300,
    pdf_path: str | None = None,
    resave_path: str | None = None,
) -> RenderWorksheetResult:
    """Render in installed Windows Mathcad Prime/Express (native or same-host WSL).

    Optional PDF is raster assembly of the rendered pages. A render is visual
    evidence, not independent numerical verification. Never closes a user session.
    """
    source = _existing_file(path)
    if pdf_path is not None and Path(pdf_path).resolve() == source.resolve():
        raise McpToolError("PDF output must differ from worksheet input")
    result = render_pages(
        source,
        Path(output_dir),
        dpi=dpi,
        timeout=timeout,
        resave=Path(resave_path) if resave_path is not None else None,
    )
    if pdf_path is not None:
        write_pdf(result.pages, Path(pdf_path), title=worksheet_title(source), dpi=dpi)
    return RenderWorksheetResult(
        xps_path=str(result.xps),
        pages=[str(p) for p in result.pages],
        pdf_path=pdf_path,
        resaved_path=resave_path,
    )


@mcp.tool()
def compare_renders(
    old_dir: str,
    new_dir: str,
    output_dir: str,
    threshold: float = 0.0,
) -> CompareRendersResult:
    """Compare rendered pages and write pixel-difference images; no Prime required."""
    result = compare_pages(Path(old_dir), Path(new_dir), Path(output_dir), threshold=threshold)
    return CompareRendersResult(changed=result.changed, pages=[p.describe() for p in result.pages])


def main() -> None:
    """Run the server on the MCP stdio transport."""
    mcp.run(transport="stdio")
