"""Stable, JSON-friendly reports for semantic worksheet builds.

The report is deliberately a value object rather than a log transcript.  It
contains the identities and verdicts needed to reproduce or audit a build,
while path fields are available to callers but excluded from the persisted
payload so reports do not accidentally leak machine-local paths or become
non-deterministic across workspaces.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pymcdx.authoring_models import ScalarValue


class ValueCheckEntry(BaseModel):
    """The verdict for one expected name; values are SI."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str
    sheet_name: str
    status: Literal["match", "mismatch", "missing"]
    expected: float
    actual: float | str | None = None
    rel_error: float | None = None


class ValueCheckReport(BaseModel):
    """Per-name verdicts; ``passed`` only when every name matches."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    status: Literal["passed", "failed"]
    entries: list[ValueCheckEntry]
    partial_gap: str | None = None


class _StrictReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ValidationReport(_StrictReport):
    """XSD/package validation verdict."""

    status: Literal["passed", "failed"]
    valid: bool
    errors: list[str] = Field(default_factory=list)


class LayoutFindingReport(_StrictReport):
    """One Mathcad-free geometry diagnostic."""

    region: str
    check: str
    message: str


class GeometryReport(_StrictReport):
    """Geometry measured by the authoring layout pass and package lint."""

    paper: str
    orientation: str
    geometry_profile: str
    content_width_px: float
    content_height_px: float
    page_count: int
    region_count: int
    margins_confirmed: bool
    estimated: bool
    estimated_text_geometry: bool


class LayoutReport(_StrictReport):
    """Strict layout verdict plus the measured geometry."""

    status: Literal["passed", "failed"]
    valid: bool
    geometry: GeometryReport
    findings: list[LayoutFindingReport] = Field(default_factory=list)


class CalculationOutputReport(_StrictReport):
    """One scalar output captured from a calculation provider."""

    value: ScalarValue
    unit: str | None = None


class CalculationReport(_StrictReport):
    """Resolution/provenance summary for one semantic calculation block."""

    block_id: str
    entry_id: str
    status: Literal["resolved", "skipped", "failed"]
    manifest_hash: str | None = None
    engine_version: str | None = None
    source: str | None = None
    outputs: dict[str, CalculationOutputReport] = Field(default_factory=dict)
    output_count: int
    trace_count: int


class ProvenanceReport(_StrictReport):
    """Build identities used to compare or reproduce reports."""

    schema_version: int
    compiler: str
    source_hash: str
    declared_source_hash: str | None = None
    style_hash: str
    manifest_hashes: list[str] = Field(default_factory=list)
    provider_sources: list[str] = Field(default_factory=list)


class BuildReport(_StrictReport):
    """Versioned result returned by the service, CLI, and MCP surface."""

    schema_version: Literal[1]
    status: Literal["succeeded"]
    source_hash: str
    output_hash: str
    source_path: str | None = Field(default=None, exclude=True)
    output_path: str | None = Field(default=None, exclude=True)
    validation: ValidationReport
    layout: LayoutReport
    calculations: list[CalculationReport] = Field(default_factory=list)
    provenance: ProvenanceReport
    warnings: list[str] = Field(default_factory=list)
    estimated_geometry: bool
    estimated_text_geometry: bool
    caret_index_estimated: bool
    value_check: ValueCheckReport | None = None

    @property
    def validated(self) -> bool:
        """Compatibility convenience for callers that need the boolean verdict."""

        return self.validation.valid

    @property
    def layout_valid(self) -> bool:
        """Whether strict package geometry passed."""

        return self.layout.valid

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic persisted representation."""

        value = self.model_dump(mode="json", exclude_none=True)
        if not isinstance(value, dict):  # pragma: no cover - guarded by Pydantic
            raise TypeError("build report did not serialize to an object")
        return value

    def to_json(self) -> str:
        """Serialize without timestamps, paths, or incidental key ordering."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "BuildReport",
    "CalculationOutputReport",
    "CalculationReport",
    "GeometryReport",
    "LayoutFindingReport",
    "LayoutReport",
    "ProvenanceReport",
    "ValidationReport",
]
