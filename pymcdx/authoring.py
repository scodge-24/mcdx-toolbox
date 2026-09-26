"""Transactional semantic YAML to Mathcad worksheet builds."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from pymcdx import layout as package_layout
from pymcdx.authoring_contracts import (
    CalculationProvider,
    CalculationRequest,
    CalculationResult,
)
from pymcdx.authoring_io import AuthoringDocumentError, load_document
from pymcdx.authoring_layout import LayoutError, LayoutResult, TwoPassLayout
from pymcdx.authoring_models import (
    AuthoringDocument,
    CalculationBlock,
    ImageBlock,
    resolved_styles,
)
from pymcdx.authoring_render import RenderError, compile_document
from pymcdx.authoring_report import (
    BuildReport,
    CalculationOutputReport,
    CalculationReport,
    GeometryReport,
    LayoutFindingReport,
    LayoutReport,
    ProvenanceReport,
    ValidationReport,
    ValueCheckReport,
)
from pymcdx.figures import manifest_for_output
from pymcdx.packager import McdxPackager
from pymcdx.validation import McdxValidationError, validate_mcdx_file

AUTHORING_COMPILER = "pymcdx-authoring-v1"
GEOMETRY_PROFILE = "a4-portrait-custom-fine-v1"
_SHA256_PREFIX: Final[str] = "sha256:"
_STRICT_LAYOUT_CHECKS: Final[frozenset[str]] = frozenset(
    {"horizontal-overflow", "out-of-bounds", "zero-size", "overlap"}
)


class AuthoringBuildError(RuntimeError):
    """Structured, fail-loud error from the semantic build boundary."""

    code: str
    path: str

    def __init__(self, message: str, *, code: str, path: str = "$") -> None:
        self.code = code
        self.path = path
        super().__init__(message)

    @property
    def detail(self) -> str:
        """Stable one-line human detail for CLI/MCP adapters."""

        return " ".join(str(self).split())

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.detail}


def _digest_bytes(value: bytes) -> str:
    return _SHA256_PREFIX + hashlib.sha256(value).hexdigest()


def _digest_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _digest_bytes(payload)


def _selected_result(result: CalculationResult, block: CalculationBlock) -> CalculationResult:
    """Select one provider output without silently dropping ambiguity.

    The v1 semantic block may select a named output.  If it does not, exactly
    one provider output is required; silently choosing one from a multi-output
    response would make the worksheet's meaning depend on dictionary order.
    """

    if not result.outputs:
        raise AuthoringBuildError(
            f"calculation block {block.id!r} returned no outputs",
            code="calculation_no_outputs",
            path=f"$.blocks[{block.id}]",
        )
    if block.output is None:
        if len(result.outputs) != 1:
            raise AuthoringBuildError(
                f"calculation block {block.id!r} returned multiple outputs; select one with output",
                code="calculation_output_ambiguous",
                path=f"$.blocks[{block.id}].output",
            )
        return result
    if block.output not in result.outputs:
        raise AuthoringBuildError(
            f"calculation output {block.output!r} was not returned for entry {block.entry_id!r}",
            code="calculation_output_unknown",
            path=f"$.blocks[{block.id}].output",
        )
    return result.model_copy(update={"outputs": {block.output: result.outputs[block.output]}})


def _resolve_calculations(
    document: AuthoringDocument,
    provider: CalculationProvider | None,
) -> tuple[dict[str, CalculationResult], list[CalculationReport], list[str], list[str]]:
    blocks = [block for block in document.blocks if isinstance(block, CalculationBlock)]
    if not blocks:
        return {}, [], [], []
    active_provider = provider
    if active_provider is None:
        raise AuthoringBuildError(
            "calculation blocks require a caller-supplied provider",
            code="provider_required",
            path="$.blocks",
        )

    calculations: dict[str, CalculationResult] = {}
    reports: list[CalculationReport] = []
    manifest_hashes: list[str] = []
    provider_sources: list[str] = []
    for block in blocks:
        block_id = block.id
        try:
            manifest = active_provider.manifest(block.entry_id)
        except Exception as exc:
            raise AuthoringBuildError(
                f"could not resolve calculation entry {block.entry_id!r}: {exc}",
                code="provider_manifest",
                path=f"$.blocks[{block_id}]",
            ) from exc
        unknown_inputs = sorted(set(block.inputs) - set(manifest.inputs))
        if unknown_inputs:
            raise AuthoringBuildError(
                f"calculation entry {block.entry_id!r} does not accept inputs {unknown_inputs!r}",
                code="calculation_input_binding",
                path=f"$.blocks[{block_id}].inputs",
            )
        try:
            result = active_provider.run(
                CalculationRequest(entry_id=block.entry_id, inputs=dict(block.inputs))
            )
        except Exception as exc:
            raise AuthoringBuildError(
                f"could not run calculation entry {block.entry_id!r}: {exc}",
                code="provider_run",
                path=f"$.blocks[{block_id}]",
            ) from exc
        if result.entry_id != block.entry_id:
            raise AuthoringBuildError(
                f"provider returned entry {result.entry_id!r} for requested {block.entry_id!r}",
                code="provider_result_mismatch",
                path=f"$.blocks[{block_id}]",
            )
        calculations[block_id] = _selected_result(result, block)
        manifest_hash = result.manifest_hash or manifest.manifest_hash
        source = result.source or manifest.source
        if manifest_hash is not None:
            manifest_hashes.append(manifest_hash)
        if source is not None:
            provider_sources.append(source)
        reports.append(
            CalculationReport(
                block_id=block_id,
                entry_id=block.entry_id,
                status="resolved",
                manifest_hash=manifest_hash,
                engine_version=result.engine_version or manifest.engine_version,
                source=source,
                outputs={
                    name: CalculationOutputReport(value=output.value, unit=output.unit)
                    for name, output in sorted(result.outputs.items())
                },
                output_count=len(result.outputs),
                trace_count=len(result.trace),
            )
        )
    return calculations, reports, manifest_hashes, provider_sources


def _layout_report(
    summary: Mapping[str, object],
    result: LayoutResult,
) -> LayoutReport:
    raw_findings = summary.get("findings")
    if not isinstance(raw_findings, list):
        raise AuthoringBuildError("layout summary findings are not a list", code="layout_summary")
    findings: list[LayoutFindingReport] = []
    for raw in raw_findings:
        if not isinstance(raw, Mapping):
            raise AuthoringBuildError(
                "layout summary finding is not an object", code="layout_summary"
            )
        region = raw.get("region")
        check = raw.get("check")
        message = raw.get("message")
        if (
            not isinstance(region, str)
            or not isinstance(check, str)
            or not isinstance(message, str)
        ):
            raise AuthoringBuildError(
                "layout summary finding has invalid fields", code="layout_summary"
            )
        findings.append(LayoutFindingReport(region=region, check=check, message=message))
    geometry = GeometryReport(
        paper=_required_str(summary, "paper"),
        orientation=_required_str(summary, "orientation"),
        geometry_profile=GEOMETRY_PROFILE,
        content_width_px=_required_float(summary, "content_width_px"),
        content_height_px=_required_float(summary, "content_height_px"),
        page_count=result.page_count,
        region_count=_required_int(summary, "region_count"),
        margins_confirmed=_required_bool(summary, "margins_confirmed"),
        estimated=result.estimated_geometry,
        estimated_text_geometry=result.estimated_text_geometry,
    )
    invalid = [finding for finding in findings if finding.check in _STRICT_LAYOUT_CHECKS]
    return LayoutReport(
        status="failed" if invalid else "passed",
        valid=not invalid,
        geometry=geometry,
        findings=findings,
    )


def _required_str(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise AuthoringBuildError(f"layout summary field {key!r} is invalid", code="layout_summary")
    return value


def _required_float(values: Mapping[str, object], key: str) -> float:
    value = values.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise AuthoringBuildError(f"layout summary field {key!r} is invalid", code="layout_summary")
    return float(value)


def _required_int(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuthoringBuildError(f"layout summary field {key!r} is invalid", code="layout_summary")
    return value


def _required_bool(values: Mapping[str, object], key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise AuthoringBuildError(f"layout summary field {key!r} is invalid", code="layout_summary")
    return value


def _stage_bytes(destination: Path, data: bytes) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    staged = Path(raw_path)
    try:
        with os.fdopen(handle, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _stage_text(destination: Path, text: str) -> Path:
    return _stage_bytes(destination, text.encode("utf-8"))


def _canonical_destination(path: Path) -> Path:
    """Resolve a user destination for identity checks without changing its spelling."""

    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthoringBuildError(
            f"could not resolve output destination {path}: {exc}",
            code="output_resolution",
        ) from exc


def _destinations_alias(
    left: Path,
    left_canonical: Path,
    right: Path,
    right_canonical: Path,
) -> bool:
    """Return whether two destination spellings can publish to one file."""

    if left_canonical == right_canonical:
        return True
    try:
        return left.samefile(right)
    except (FileNotFoundError, NotADirectoryError):
        # Canonical paths cover non-existent leaves (including dangling links).
        return False
    except OSError as exc:
        raise AuthoringBuildError(
            f"could not compare output destinations {left} and {right}: {exc}",
            code="output_resolution",
        ) from exc


def _reserve_temp_path(destination: Path, suffix: str) -> Path:
    handle, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=suffix, dir=destination.parent
    )
    os.close(handle)
    path = Path(raw_path)
    path.unlink(missing_ok=True)
    return path


def _publish_transaction(
    entries: list[tuple[Path, Path]],
) -> None:
    """Publish staged files as a rollback-capable replacement set."""

    backups: list[tuple[Path, Path | None]] = []
    published: list[Path] = []
    try:
        for destination, staged in entries:
            if destination.exists():
                backup = _reserve_temp_path(destination, ".bak")
                destination.replace(backup)
            else:
                backup = None
            backups.append((destination, backup))
            staged.replace(destination)
            published.append(destination)
    except BaseException:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        for destination, backup in reversed(backups):
            if backup is not None and backup.exists():
                backup.replace(destination)
        raise
    finally:
        for _destination, backup in backups:
            if backup is not None:
                backup.unlink(missing_ok=True)


def _require_manifest_figures(document: AuthoringDocument, directory: Path) -> None:
    """Name the regenerating command when a missing picture comes from a figures manifest."""

    for block in document.blocks:
        if not isinstance(block, ImageBlock) or Path(block.path).exists():
            continue
        manifest = manifest_for_output(Path(block.path), directory)
        if manifest is not None:
            raise AuthoringBuildError(
                f"image block {block.id or block.path!r}: {block.path} is missing; regenerate "
                f"it with `pymcdx figure --manifest {manifest}`",
                code="image_missing",
                path=f"$.blocks[{block.id or block.path}].path",
            )


def build_authoring_document(
    spec_path: Path | str,
    output_path: Path | str,
    report_path: Path | str | None = None,
    preview_path: Path | str | None = None,
    provider: CalculationProvider | None = None,
    *,
    document_loader: Callable[[Path], AuthoringDocument] = load_document,
    value_checker: Callable[[Path], ValueCheckReport] | None = None,
) -> BuildReport:
    """Build one semantic YAML document and atomically publish its products.

    Python integrations may supply a document loader and a value checker.
    No numerical evaluator is built in. A supplied check runs against the
    staged worksheet before publication and its typed report is preserved.
    A failed verdict still publishes; the caller decides the exit status.
    """

    spec_path = Path(spec_path)
    output_path = Path(output_path)
    report_path = Path(report_path) if report_path is not None else None
    preview_path = Path(preview_path) if preview_path is not None else None

    if not spec_path.exists():
        raise AuthoringBuildError(f"input file {spec_path} not found", code="input_missing")
    if not spec_path.is_file():
        raise AuthoringBuildError(f"input path {spec_path} is not a file", code="input_not_file")
    if output_path.exists() and output_path.is_dir():
        raise AuthoringBuildError(
            f"output path {output_path} is a directory", code="output_not_file"
        )
    for destination, label in (
        (report_path, "report"),
        (preview_path, "preview"),
    ):
        if destination is not None and destination.exists() and destination.is_dir():
            raise AuthoringBuildError(
                f"{label} path {destination} is a directory", code="output_not_file"
            )

    destinations = [("output", output_path)]
    if report_path is not None:
        destinations.append(("report", report_path))
    if preview_path is not None:
        destinations.append(("preview", preview_path))
    canonical_destinations = [
        (label, destination, _canonical_destination(destination))
        for label, destination in destinations
    ]
    for index, (label, destination, canonical) in enumerate(canonical_destinations):
        for other_label, other_destination, other_canonical in canonical_destinations[index + 1 :]:
            if _destinations_alias(destination, canonical, other_destination, other_canonical):
                raise AuthoringBuildError(
                    f"{label} path {destination} aliases {other_label} path {other_destination}",
                    code="output_collision",
                )

    try:
        source_bytes = spec_path.read_bytes()
    except OSError as exc:
        raise AuthoringBuildError(str(exc), code="input_read") from exc
    source_hash = _digest_bytes(source_bytes)
    try:
        document = document_loader(spec_path)
    except AuthoringDocumentError as exc:
        raise AuthoringBuildError(str(exc), code=exc.code, path=exc.path) from exc

    _require_manifest_figures(document, spec_path.parent)
    try:
        layout_result = TwoPassLayout(document).layout_document()
    except (LayoutError, ValueError) as exc:
        raise AuthoringBuildError(str(exc), code="layout_measurement") from exc

    calculations, calculation_reports, manifest_hashes, provider_sources = _resolve_calculations(
        document, provider
    )
    try:
        compiled = compile_document(document, calculations=calculations, layout=layout_result)
    except (RenderError, ValueError, TypeError) as exc:
        raise AuthoringBuildError(str(exc), code="render") from exc

    staged_output: Path | None = None
    staged_report: Path | None = None
    staged_preview: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        staged_output = _stage_bytes(output_path, b"")
        McdxPackager(staged_output, compiled.builder).create_package(validate=True)
        try:
            validate_mcdx_file(staged_output)
        except McdxValidationError as exc:
            raise AuthoringBuildError(
                str(exc), code="xsd_validation", path=str(staged_output)
            ) from exc

        try:
            package_summary = package_layout.summarize(staged_output)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise AuthoringBuildError(str(exc), code="layout_check") from exc
        layout_report = _layout_report(package_summary, compiled.layout)
        if not layout_report.valid:
            checks = ", ".join(sorted({finding.check for finding in layout_report.findings}))
            raise AuthoringBuildError(
                f"strict layout check failed: {checks}", code="layout_check", path="$.layout"
            )

        output_hash = _digest_bytes(staged_output.read_bytes())
        declared_hash = (
            document.document.provenance.source_hash if document.document.provenance else None
        )
        style_hash = _digest_json(
            {
                name: token.model_dump(mode="json", exclude_none=True)
                for name, token in sorted(resolved_styles(document).items())
            }
        )
        warnings: list[str] = []
        if layout_result.estimated_text_geometry:
            warnings.append(
                "text and math geometry use Prime-calibrated estimates; arbitrary wrapping "
                "and evaluated-result widths remain estimated"
            )
        warnings.append(
            "ASCII inline-math caret offsets are Prime-confirmed; non-BMP indexing remains "
            "estimated"
        )
        report = BuildReport(
            schema_version=1,
            status="succeeded",
            source_hash=source_hash,
            output_hash=output_hash,
            source_path=str(spec_path),
            output_path=str(output_path),
            validation=ValidationReport(status="passed", valid=True),
            layout=layout_report,
            calculations=calculation_reports,
            provenance=ProvenanceReport(
                schema_version=document.schema_version,
                compiler=AUTHORING_COMPILER,
                source_hash=source_hash,
                declared_source_hash=declared_hash,
                style_hash=style_hash,
                manifest_hashes=manifest_hashes,
                provider_sources=provider_sources,
            ),
            warnings=warnings,
            estimated_geometry=compiled.estimated_geometry,
            estimated_text_geometry=compiled.estimated_text_geometry,
            caret_index_estimated=True,
            value_check=(value_checker(staged_output) if value_checker is not None else None),
        )

        if preview_path is not None:
            try:
                preview_page = package_layout.load_page(staged_output)
                preview = package_layout.render_svg(
                    preview_page,
                    package_layout.load_regions(staged_output, page=preview_page),
                )
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise AuthoringBuildError(str(exc), code="preview") from exc
            staged_preview = _stage_text(preview_path, preview)
        if report_path is not None:
            staged_report = _stage_text(report_path, report.to_json() + "\n")

        entries: list[tuple[Path, Path]] = [(output_path, staged_output)]
        if staged_report is not None:
            if report_path is None:  # pragma: no cover - paired option invariant
                raise AuthoringBuildError("report staging lost its destination", code="report")
            entries.append((report_path, staged_report))
        if staged_preview is not None:
            if preview_path is None:  # pragma: no cover - paired option invariant
                raise AuthoringBuildError("preview staging lost its destination", code="preview")
            entries.append((preview_path, staged_preview))
        _publish_transaction(entries)
        staged_output = None
        staged_report = None
        staged_preview = None
        return report
    except AuthoringBuildError:
        raise
    except (OSError, McdxValidationError, ValueError, TypeError) as exc:
        raise AuthoringBuildError(str(exc), code="build") from exc
    finally:
        for staged in (staged_output, staged_report, staged_preview):
            if staged is not None:
                staged.unlink(missing_ok=True)


__all__ = ["AUTHORING_COMPILER", "AuthoringBuildError", "build_authoring_document"]
