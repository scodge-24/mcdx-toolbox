"""Typed semantic worksheet authoring models.

The models in this module are the closed v1 boundary for the semantic YAML
authoring format.  They intentionally contain document intent only; XML part
names, relationship identifiers, and absolute worksheet coordinates belong to
the rendering and packaging layers.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from pymcdx.authoring_functions import (
    called_names,
    parse_function,
    reject_bad_radicals,
    reject_function_syntax,
)


class _StrictModel(BaseModel):
    """Shared strict configuration for all authoring boundary models."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
        strip_whitespace=False,
    ),
]
"""Stable ASCII identifiers used for document components and style names."""

EntryId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[^\s\x00-\x1f\x7f]+$",
        strip_whitespace=False,
    ),
]
"""Provider entry identifiers, which may contain safe path separators."""


FiniteNumber = Annotated[int | float, Field(allow_inf_nan=False)]
NonNegativeNumber = Annotated[int | float, Field(ge=0, allow_inf_nan=False)]
PositiveNumber = Annotated[int | float, Field(gt=0, allow_inf_nan=False)]
ScalarValue = bool | int | float | str

UnitSymbol = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[^\s\x00-\x1f\x7f]+$",
        strip_whitespace=False,
    ),
]


class DocumentProvenance(_StrictModel):
    """Optional source identity carried by an authored document."""

    source: str | None = None
    source_hash: str | None = None
    tool: str | None = None
    tool_version: str | None = None
    notes: str | None = None

    @field_validator("source", "source_hash", "tool", "tool_version", "notes")
    @classmethod
    def _non_empty_text(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("provenance text values must not be empty")
        return value


class DocumentMetadata(_StrictModel):
    """Human-facing document metadata."""

    title: str | None = None
    project: str | None = None
    revision: str | None = None
    author: str | None = None
    calc_number: str | None = None
    date: str | None = None
    checked_by: str | None = None
    approved_by: str | None = None
    provenance: DocumentProvenance | None = None

    @field_validator(
        "title", "project", "revision", "author", "calc_number", "date", "checked_by", "approved_by"
    )
    @classmethod
    def _non_empty_text(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("document metadata text values must not be empty")
        return value


class WorksheetMargins(_StrictModel):
    """Content-relative page margins in millimetres."""

    left: NonNegativeNumber
    top: NonNegativeNumber
    right: NonNegativeNumber
    bottom: NonNegativeNumber


class WorksheetSettings(_StrictModel):
    """Page and grid intent used by the WPF-pixel layout pass."""

    paper: Literal["A4"]
    orientation: Literal["portrait"]
    margins_mm: WorksheetMargins
    grid: Literal["fine"]


class StyleToken(_StrictModel):
    """A named semantic style token, independent of Mathcad XML details."""

    font_family: str
    font_size_pt: PositiveNumber
    bold: bool = False
    italic: bool = False
    color: str
    width: Literal["full", "half"] = "full"
    space_before_mm: NonNegativeNumber = 0
    space_after_mm: NonNegativeNumber = 0
    line_height: PositiveNumber = 1.0

    @field_validator("font_family", "color")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value:
            raise ValueError("style text values must not be empty")
        return value


class QuantityInput(_StrictModel):
    """A scalar magnitude paired with the unit required by a provider."""

    value: FiniteNumber
    unit: UnitSymbol


CalculationInput = ScalarValue | QuantityInput


StyleProfileName = Literal["engineering"]
IndentGridlines = Annotated[int, Field(strict=True, ge=0, le=4)]


def _engineering_styles() -> dict[str, StyleToken]:
    """Return the calibrated semantic tokens for the engineering profile."""

    return {
        "title": StyleToken(
            font_family="Arial",
            font_size_pt=14.0,
            bold=True,
            color="#FF000000",
            space_after_mm=2.5,
        ),
        "heading1": StyleToken(
            font_family="Arial",
            font_size_pt=12.0,
            bold=True,
            color="#FF000000",
            space_before_mm=5.0,
            space_after_mm=2.5,
        ),
        "heading2": StyleToken(
            font_family="Arial",
            font_size_pt=11.0,
            bold=True,
            color="#FF000000",
            space_before_mm=2.5,
            space_after_mm=2.5,
        ),
        "heading3": StyleToken(
            font_family="Arial",
            font_size_pt=10.0,
            bold=True,
            italic=True,
            color="#FF000000",
            space_before_mm=2.5,
            space_after_mm=1.25,
        ),
        "body": StyleToken(
            font_family="Arial",
            font_size_pt=10.0,
            color="#FF000000",
            space_after_mm=2.5,
        ),
        "note": StyleToken(
            font_family="Arial",
            font_size_pt=9.0,
            italic=True,
            color="#FF444444",
            space_after_mm=2.5,
        ),
        "math": StyleToken(
            font_family="Mathcad UniMath Prime",
            font_size_pt=11.0,
            color="#FF000000",
            space_after_mm=2.5,
        ),
        "result": StyleToken(
            font_family="Arial",
            font_size_pt=10.0,
            bold=True,
            color="#FF000000",
            space_after_mm=2.5,
        ),
        "figure": StyleToken(
            font_family="Arial",
            font_size_pt=10.0,
            color="#FF000000",
            space_before_mm=2.5,
            space_after_mm=5.0,
        ),
        "half_body": StyleToken(
            font_family="Arial",
            font_size_pt=10.0,
            color="#FF000000",
            width="half",
            space_after_mm=2.5,
        ),
    }


def profile_styles(profile: StyleProfileName) -> dict[str, StyleToken]:
    """Resolve one built-in profile to a fresh, immutable-token registry."""

    if profile == "engineering":
        return _engineering_styles()
    raise ValueError(f"unknown authoring style profile {profile!r}")


class HeadingBlock(_StrictModel):
    """A styled heading block."""

    kind: Literal["heading"]
    id: Identifier | None = None
    level: Literal[1, 2, 3, 4, 5, 6]
    text: str
    style: Identifier | None = None
    indent: IndentGridlines = 0

    @field_validator("text")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        if not value:
            raise ValueError("heading text must not be empty")
        return value


class TextRun(_StrictModel):
    """Plain text run within a text block."""

    kind: Literal["text"]
    text: str
    style: Identifier | None = None


class InlineMathRun(_StrictModel):
    """Inline Mathcad expression run within a text block."""

    kind: Literal["inline_math"]
    expression: str
    evaluate: bool = False
    unit: UnitSymbol | None = None
    style: Identifier | None = None

    @field_validator("expression")
    @classmethod
    def _non_empty_expression(cls, value: str) -> str:
        if not value:
            raise ValueError("inline math expression must not be empty")
        reject_function_syntax(value)
        reject_bad_radicals(value)
        return value


TextRunNode = Annotated[TextRun | InlineMathRun, Field(discriminator="kind")]


class TextBlock(_StrictModel):
    """A paragraph made of ordered plain-text and inline-math runs."""

    kind: Literal["text"]
    id: Identifier | None = None
    style: Identifier | None = None
    indent: IndentGridlines = 0
    runs: list[TextRunNode] = Field(min_length=1)


class MathBlock(_StrictModel):
    """A display Mathcad expression block."""

    kind: Literal["math"]
    id: Identifier | None = None
    expression: str
    style: Identifier | None = None
    indent: IndentGridlines = 0

    @field_validator("expression")
    @classmethod
    def _non_empty_expression(cls, value: str) -> str:
        if not value:
            raise ValueError("math expression must not be empty")
        reject_function_syntax(value)
        reject_bad_radicals(value)
        return value


class CalculationBlock(_StrictModel):
    """A typed request for a provider-backed calculation."""

    kind: Literal["calculation"]
    id: Identifier
    provider: Identifier
    entry_id: EntryId
    inputs: dict[Identifier, CalculationInput] = Field(default_factory=dict)
    output: Identifier | None = None
    style: Identifier | None = None
    indent: IndentGridlines = 0

    @field_validator("inputs")
    @classmethod
    def _finite_inputs(cls, value: dict[str, CalculationInput]) -> dict[str, CalculationInput]:
        for input_name, input_value in value.items():
            magnitude = input_value.value if isinstance(input_value, QuantityInput) else input_value
            if isinstance(magnitude, float) and not math.isfinite(magnitude):
                raise ValueError(f"calculation input {input_name!r} must be finite")
        return value


class PageBreakBlock(_StrictModel):
    """An explicit page break in the semantic document."""

    kind: Literal["page_break"]
    id: Identifier | None = None


class FunctionBlock(_StrictModel):
    """One user-defined Mathcad function written as a Python ``def``."""

    kind: Literal["function"]
    id: Identifier | None = None
    source: str
    style: Identifier | None = None
    indent: IndentGridlines = 0

    @field_validator("source")
    @classmethod
    def _supported_source(cls, value: str) -> str:
        parse_function(value)
        return value


class ContentsBlock(_StrictModel):
    """A generated table of contents over the numbered headings."""

    kind: Literal["contents"]
    id: Identifier | None = None
    title: str = "Contents"
    depth: Annotated[int, Field(strict=True, ge=1, le=6)] = 2
    style: Identifier | None = None
    indent: IndentGridlines = 0


class ImageBlock(_StrictModel):
    """A PNG diagram or screenshot, sized to the page, with an optional caption."""

    kind: Literal["image"]
    id: Identifier | None = None
    path: str
    caption: str | None = None
    width_mm: PositiveNumber | None = None
    style: Identifier | None = None
    indent: IndentGridlines = 0

    @field_validator("path")
    @classmethod
    def _png_path(cls, value: str) -> str:
        if not value.lower().endswith(".png"):
            raise ValueError("image path must name a .png file (convert other formats first)")
        return value

    @field_validator("caption")
    @classmethod
    def _non_empty_caption(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("image caption must not be empty")
        return value


class CheckBlock(_StrictModel):
    """A design check: a utilisation against a limit with a Mathcad-computed verdict."""

    kind: Literal["check"]
    id: Identifier | None = None
    label: str
    utilisation: str
    limit: FiniteNumber = 1.0
    style: Identifier | None = None
    indent: IndentGridlines = 0

    @field_validator("label")
    @classmethod
    def _non_empty_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("check label must not be empty")
        return value

    @field_validator("utilisation")
    @classmethod
    def _non_empty_utilisation(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("check utilisation must not be empty")
        reject_function_syntax(value)
        return value

    @field_validator("limit")
    @classmethod
    def _positive_limit(cls, value: int | float) -> int | float:
        if value <= 0:
            raise ValueError(f"check limit must be positive, got {value}")
        return value


class SummaryBlock(_StrictModel):
    """A table of every check above it, closed by the maximum utilisation."""

    kind: Literal["summary"]
    id: Identifier | None = None
    style: Identifier | None = None
    indent: IndentGridlines = 0


Block = Annotated[
    HeadingBlock
    | TextBlock
    | MathBlock
    | FunctionBlock
    | CalculationBlock
    | ContentsBlock
    | ImageBlock
    | PageBreakBlock
    | CheckBlock
    | SummaryBlock,
    Field(discriminator="kind"),
]


class AuthoringDocument(_StrictModel):
    """Versioned semantic worksheet document consumed by the build pipeline."""

    schema_version: Literal[1]
    document: DocumentMetadata
    worksheet: WorksheetSettings
    styles: dict[Identifier, StyleToken] = Field(default_factory=dict)
    blocks: list[Block]
    profile: StyleProfileName = "engineering"

    @model_validator(mode="after")
    def _validate_references(self) -> Self:
        """Reject duplicate component IDs and unresolved style references."""

        component_ids: dict[str, int] = {}
        known_styles = profile_styles(self.profile) | self.styles
        for index, block in enumerate(self.blocks):
            block_id = block.id
            if block_id is not None:
                previous = component_ids.get(block_id)
                if previous is not None:
                    raise ValueError(
                        f"duplicate component id {block_id!r} at blocks[{index}] "
                        f"(already used at blocks[{previous}])"
                    )
                component_ids[block_id] = index

            if not isinstance(block, PageBreakBlock):
                self._require_style(block.style, f"blocks[{index}].style", known_styles)

            if isinstance(block, TextBlock):
                for run_index, run in enumerate(block.runs):
                    self._require_style(
                        run.style,
                        f"blocks[{index}].runs[{run_index}].style",
                        known_styles,
                    )

        self._validate_function_order()
        self._validate_summaries()
        return self

    def _validate_summaries(self) -> None:
        """A summary must follow at least one check block."""

        checks = 0
        for index, block in enumerate(self.blocks):
            if isinstance(block, CheckBlock):
                checks += 1
            elif isinstance(block, SummaryBlock) and checks == 0:
                raise ValueError(f"blocks[{index}]: a summary needs at least one check above it")

    def _validate_function_order(self) -> None:
        """Every call to a function block must come after its definition."""

        defined_at: dict[str, int] = {}
        for index, block in enumerate(self.blocks):
            if isinstance(block, FunctionBlock):
                name = parse_function(block.source).name
                if name in defined_at:
                    raise ValueError(
                        f"blocks[{index}]: function {name!r} is already defined at "
                        f"blocks[{defined_at[name]}]"
                    )
                defined_at[name] = index
        seen: set[str] = set()
        for index, block in enumerate(self.blocks):
            if isinstance(block, FunctionBlock):
                definition = parse_function(block.source)
                calls = definition.calls - {definition.name}
                seen.add(definition.name)
            elif isinstance(block, MathBlock):
                calls = called_names(block.expression)
            elif isinstance(block, CheckBlock):
                calls = called_names(block.utilisation)
            elif isinstance(block, TextBlock):
                runs = [r for r in block.runs if isinstance(r, InlineMathRun)]
                calls = frozenset().union(*(called_names(r.expression) for r in runs))
            else:
                continue
            early = sorted(calls & defined_at.keys() - seen)
            if early:
                name = early[0]
                raise ValueError(
                    f"blocks[{index}]: calls {name}() above its function block at "
                    f"blocks[{defined_at[name]}]; move the definition earlier"
                )

    def _require_style(
        self,
        style: str | None,
        path: str,
        known_styles: dict[str, StyleToken],
    ) -> None:
        if style is not None and style not in known_styles:
            raise ValueError(f"{path}: unresolved style reference {style!r}")


def resolved_styles(document: AuthoringDocument) -> dict[str, StyleToken]:
    """Merge the selected built-in profile with document-local overrides."""

    return profile_styles(document.profile) | document.styles


__all__ = [
    "AuthoringDocument",
    "Block",
    "CalculationBlock",
    "CalculationInput",
    "CheckBlock",
    "ContentsBlock",
    "DocumentMetadata",
    "DocumentProvenance",
    "EntryId",
    "FiniteNumber",
    "FunctionBlock",
    "HeadingBlock",
    "Identifier",
    "IndentGridlines",
    "InlineMathRun",
    "MathBlock",
    "NonNegativeNumber",
    "PageBreakBlock",
    "PositiveNumber",
    "QuantityInput",
    "ScalarValue",
    "StyleProfileName",
    "StyleToken",
    "SummaryBlock",
    "TextBlock",
    "TextRun",
    "TextRunNode",
    "UnitSymbol",
    "WorksheetMargins",
    "WorksheetSettings",
    "profile_styles",
    "resolved_styles",
]
