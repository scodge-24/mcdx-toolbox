"""Provider-neutral calculation contracts for semantic worksheet authoring."""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pymcdx.authoring_models import (
    CalculationInput,
    EntryId,
    Identifier,
    QuantityInput,
    ScalarValue,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def _finite_inputs(
    values: dict[str, CalculationInput],
) -> dict[str, CalculationInput]:
    for name, value in values.items():
        magnitude = value.value if isinstance(value, QuantityInput) else value
        if isinstance(magnitude, float) and not math.isfinite(magnitude):
            raise ValueError(f"calculation input {name!r} must be finite")
    return values


def _finite_scalars(values: dict[str, ScalarValue]) -> dict[str, ScalarValue]:
    for name, value in values.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"scalar value {name!r} must be finite")
    return values


class CalculationManifest(_StrictModel):
    """Minimal manifest identity needed by the provider seam."""

    entry_id: EntryId
    inputs: list[Identifier] = Field(default_factory=list)
    outputs: list[Identifier] = Field(default_factory=list)
    manifest_hash: str | None = None
    engine_version: str | None = None
    source: str | None = None


class CalculationRequest(_StrictModel):
    """One provider calculation request with scalar or unit-bearing inputs."""

    entry_id: EntryId
    inputs: dict[Identifier, CalculationInput] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def _finite_inputs(cls, value: dict[str, CalculationInput]) -> dict[str, CalculationInput]:
        return _finite_inputs(value)


class CalculationOutput(_StrictModel):
    """One named calculation output and its optional display unit."""

    value: ScalarValue
    unit: str | None = None

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: ScalarValue) -> ScalarValue:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("calculation output value must be finite")
        return value


class CalculationTraceStep(_StrictModel):
    """A provider trace entry suitable for a build report."""

    label: str | None = None
    message: str | None = None
    values: dict[Identifier, ScalarValue] = Field(default_factory=dict)

    @field_validator("label", "message")
    @classmethod
    def _non_empty_text(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("trace text values must not be empty")
        return value

    @field_validator("values")
    @classmethod
    def _finite_values(cls, value: dict[str, ScalarValue]) -> dict[str, ScalarValue]:
        return _finite_scalars(value)


class CalculationResult(_StrictModel):
    """Typed provider result consumed by rendering and reporting."""

    entry_id: EntryId
    outputs: dict[Identifier, CalculationOutput] = Field(default_factory=dict)
    trace: list[CalculationTraceStep] = Field(default_factory=list)
    manifest_hash: str | None = None
    engine_version: str | None = None
    source: str | None = None


@runtime_checkable
class CalculationProvider(Protocol):
    """Calculation source seam used by the semantic authoring service."""

    def manifest(self, entry_id: str) -> CalculationManifest:
        """Return the pinned contract manifest for one calculation entry."""
        ...

    def run(self, request: CalculationRequest) -> CalculationResult:
        """Evaluate one typed calculation request."""
        ...


__all__ = [
    "CalculationInput",
    "CalculationManifest",
    "CalculationOutput",
    "CalculationProvider",
    "CalculationRequest",
    "CalculationResult",
    "CalculationTraceStep",
    "EntryId",
    "QuantityInput",
    "ScalarValue",
]
