"""Two-pass WPF-pixel layout for semantic worksheet authoring.

The layout pass consumes the closed Track A models directly.  It estimates
typography conservatively, snaps positions to the active Prime grid, and fails
before rendering if a block cannot fit the calibrated page model.  Prime
reflow remains the acceptance oracle for the ``estimated`` boxes.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Final, cast

from lxml import etree

from pymcdx.ast_converter import (
    convert_python_expression_to_math50,
    convert_python_to_math50,
)
from pymcdx.authoring_checks import lower_blocks
from pymcdx.authoring_functions import render_function_block
from pymcdx.authoring_images import ImageError, image_display_size, load_png
from pymcdx.authoring_models import (
    AuthoringDocument,
    Block,
    CalculationBlock,
    ContentsBlock,
    FunctionBlock,
    HeadingBlock,
    ImageBlock,
    InlineMathRun,
    MathBlock,
    PageBreakBlock,
    StyleToken,
    TextBlock,
    TextRun,
    WorksheetSettings,
    resolved_styles,
)
from pymcdx.layout import math_anchor_offset

PX_PER_MM: Final[float] = 96.0 / 25.4
PX_PER_PT: Final[float] = 96.0 / 72.0
A4_WIDTH_PX: Final[float] = 210.0 * PX_PER_MM
A4_HEIGHT_PX: Final[float] = 297.0 * PX_PER_MM
DEFAULT_MARGIN_PX: Final[tuple[float, float, float, float]] = (
    5.0 * PX_PER_MM,
    40.0 * PX_PER_MM,
    5.0 * PX_PER_MM,
    12.5 * PX_PER_MM,
)
GRID_PX: Final[dict[str, float]] = {
    "fine": 2.5 * PX_PER_MM,
    "standard": 5.0 * PX_PER_MM,
}
PAGE_EDGE_CLEARANCE_PX: Final[float] = 5.0 * PX_PER_MM


class LayoutError(ValueError):
    """Raised when semantic layout cannot produce safe worksheet geometry."""


@dataclass(frozen=True, slots=True)
class PageModel:
    """A supported worksheet page model, in WPF device-independent pixels."""

    paper_code: str
    orientation: str
    margin_type: str
    margin_left: float
    margin_top: float
    margin_right: float
    margin_bottom: float
    grid_size: str
    grid_px: float
    paper_width_px: float
    paper_height_px: float
    content_width_px: float
    content_height_px: float
    edge_clearance_px: float
    page_stride_px: float

    @property
    def margins(self) -> tuple[float, float, float, float]:
        return (self.margin_left, self.margin_top, self.margin_right, self.margin_bottom)


@dataclass(frozen=True, slots=True)
class MeasuredBlock:
    """Intrinsic dimensions for a semantic block."""

    block_id: str
    kind: str
    width: float
    height: float
    estimated: bool
    style_id: str | None = None

    @property
    def intrinsic_width(self) -> float:
        return self.width

    @property
    def intrinsic_height(self) -> float:
        return self.height


@dataclass(frozen=True, slots=True)
class Placement:
    """A placed block in content-relative continuous coordinates."""

    block_id: str
    kind: str
    page: int
    top: float
    left: float
    width: float
    height: float
    estimated: bool
    style_id: str | None = None
    page_break: bool = False
    anchor_offset: float = 0.0

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def region_top(self) -> float:
        return self.top + self.anchor_offset


@dataclass(frozen=True, slots=True)
class LayoutResult:
    """Output of :class:`TwoPassLayout`."""

    page: PageModel
    placements: tuple[Placement, ...]
    measured: tuple[MeasuredBlock, ...]
    page_count: int
    estimated_text_geometry: bool

    @property
    def boxes(self) -> tuple[Placement, ...]:
        return self.placements

    @property
    def estimated_geometry(self) -> bool:
        """Whether any emitted block geometry uses an estimate."""

        return any(placement.estimated for placement in self.placements)

    @property
    def regions(self) -> tuple[Placement, ...]:
        return self.placements

    @property
    def findings(self) -> tuple[object, ...]:
        return ()


def _finite(value: int | float, label: str) -> float:
    if isinstance(value, bool):
        raise LayoutError(f"{label} must be a finite number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise LayoutError(f"{label} must be finite, got {value!r}")
    return result


def _snap(value: float, grid_px: float) -> float:
    return round(value / grid_px) * grid_px


def _snap_up(value: float, grid_px: float) -> float:
    return math.ceil((value - 1e-9) / grid_px) * grid_px


def page_model_from(
    source: AuthoringDocument | WorksheetSettings | None = None,
    *,
    grid_size: str | None = None,
) -> PageModel:
    """Resolve the calibrated A4 portrait/custom-margin page model."""

    settings = source.worksheet if isinstance(source, AuthoringDocument) else source
    if settings is None:
        # Useful for renderer unit tests while keeping production documents
        # strictly typed by AuthoringDocument.
        paper = "A4"
        orientation = "portrait"
        grid = "fine"
        left, top, right, bottom = DEFAULT_MARGIN_PX
    else:
        paper = settings.paper
        orientation = settings.orientation
        grid = settings.grid
        margins = settings.margins_mm
        left = _finite(margins.left, "worksheet.margins_mm.left") * PX_PER_MM
        top = _finite(margins.top, "worksheet.margins_mm.top") * PX_PER_MM
        right = _finite(margins.right, "worksheet.margins_mm.right") * PX_PER_MM
        bottom = _finite(margins.bottom, "worksheet.margins_mm.bottom") * PX_PER_MM
    if paper != "A4":
        raise LayoutError(f"unsupported paper {paper!r}; v1 supports A4 only")
    if orientation != "portrait":
        raise LayoutError(f"unsupported orientation {orientation!r}; v1 supports portrait only")
    active_grid = (grid_size or grid).lower()
    if active_grid not in GRID_PX:
        raise LayoutError(f"unsupported grid {active_grid!r}; expected fine or standard")
    if min(left, top, right, bottom) < 0:
        raise LayoutError("worksheet margins cannot be negative")
    content_width = A4_WIDTH_PX - left - right
    content_height = A4_HEIGHT_PX - top - bottom
    if content_width <= 0 or content_height <= 0:
        raise LayoutError("worksheet margins leave no positive content rectangle")
    grid_px = GRID_PX[active_grid]
    return PageModel(
        paper_code="A4",
        orientation="Portrait",
        margin_type="Custom",
        margin_left=left,
        margin_top=top,
        margin_right=right,
        margin_bottom=bottom,
        grid_size=active_grid.title(),
        grid_px=grid_px,
        paper_width_px=A4_WIDTH_PX,
        paper_height_px=A4_HEIGHT_PX,
        content_width_px=content_width,
        content_height_px=content_height,
        # Prime can paint region content across a page boundary when its stored
        # box touches the first or last content coordinate.  Reserve a calibrated
        # 5 mm safety band at both edges for every region kind.
        edge_clearance_px=PAGE_EDGE_CLEARANCE_PX,
        # A page stride is a containment boundary, not a visual position: it
        # must never round below the content rectangle and let adjacent pages
        # share the same continuous-coordinate band.
        page_stride_px=_snap_up(content_height, grid_px),
    )


@dataclass(frozen=True, slots=True)
class _Typography:
    font_size_px: float
    bold: bool
    italic: bool
    color: str
    space_before_px: float
    space_after_px: float
    line_height_px: float


def _default_font_size(level: int | None = None) -> float:
    if level == 1:
        return 16.0 * PX_PER_PT
    if level == 2:
        return 13.0 * PX_PER_PT
    if level in (3, 4):
        return 12.0 * PX_PER_PT
    return 10.0 * PX_PER_PT


def _typography(style: StyleToken | None, *, level: int | None = None) -> _Typography:
    size = (
        _default_font_size(level)
        if style is None
        else _finite(style.font_size_pt, "font_size_pt") * PX_PER_PT
    )
    if size <= 0:
        raise LayoutError("style font_size_pt must be positive")
    before = 0.0 if style is None else _finite(style.space_before_mm, "space_before_mm") * PX_PER_MM
    after = 0.0 if style is None else _finite(style.space_after_mm, "space_after_mm") * PX_PER_MM
    # Prime persists 15.333 px for 10 pt body text and 18.400 px for
    # 12 pt headings: 1.15 times the WPF font size.
    multiplier = 1.15 if style is None else max(1.15, _finite(style.line_height, "line_height"))
    if multiplier <= 0:
        raise LayoutError("style line_height must be positive")
    return _Typography(
        font_size_px=size,
        bold=False if style is None else style.bold,
        italic=False if style is None else style.italic,
        color="#FF000000" if style is None else style.color,
        space_before_px=before,
        space_after_px=after,
        line_height_px=size * multiplier,
    )


def _style_for(
    block: Block, styles: Mapping[str, StyleToken]
) -> tuple[str | None, StyleToken | None]:
    if isinstance(block, PageBreakBlock):
        return None, None
    if isinstance(block, HeadingBlock):
        style_id = block.style or f"heading{min(block.level, 3)}"
    elif isinstance(block, TextBlock):
        style_id = block.style or "body"
    elif isinstance(block, (MathBlock, FunctionBlock)):
        style_id = block.style or "math"
    elif isinstance(block, CalculationBlock):
        style_id = block.style or "result"
    elif isinstance(block, ContentsBlock):
        style_id = block.style or "body"
    elif isinstance(block, ImageBlock):
        style_id = block.style or "figure"
    else:
        raise LayoutError(f"unsupported block type {type(block).__name__}")
    if style_id not in styles:
        if block.style is None:
            return None, None
        raise LayoutError(f"unknown style reference {style_id!r}")
    try:
        return style_id, styles[style_id]
    except KeyError:
        raise LayoutError(f"unknown style reference {style_id!r}") from None


def heading_labels(blocks: Sequence[Block]) -> dict[int, str]:
    """Hierarchical numbers for every non-title heading, keyed by block index."""

    counters = [0] * 6
    labels: dict[int, str] = {}
    for index, block in enumerate(blocks):
        if not isinstance(block, HeadingBlock) or block.style == "title":
            continue
        counters[block.level - 1] += 1
        counters[block.level :] = [0] * (6 - block.level)
        parts = counters[: block.level]
        labels[index] = f"{parts[0]}." if block.level == 1 else ".".join(map(str, parts))
    return labels


def figure_labels(blocks: Sequence[Block]) -> dict[int, str]:
    """'Figure N' for every captioned image, numbered in document order."""

    labels: dict[int, str] = {}
    for index, block in enumerate(blocks):
        if isinstance(block, ImageBlock) and block.caption is not None:
            labels[index] = f"Figure {len(labels) + 1}"
    return labels


def figure_caption(block: ImageBlock, label: str) -> TextBlock:
    """The caption line under a figure, as the text block that renders it."""

    return TextBlock(kind="text", runs=[TextRun(kind="text", text=f"{label}: {block.caption}")])


FIGURE_CAPTION_GAP_PX = 6.0
"""Clear space between a picture's bottom edge and its caption line."""


@dataclass(frozen=True, slots=True)
class FigureGeometry:
    """An image block's PNG bytes, display size, and caption line."""

    data: bytes
    width: float
    height: float
    caption: TextBlock | None
    caption_height: float
    """Caption line plus the gap above it."""


def figure_geometry(
    block: ImageBlock,
    label: str | None,
    style: StyleToken | None,
    *,
    span_px: float,
    usable_height_px: float,
) -> FigureGeometry:
    """Size an image block to its span and one page, leaving room for its caption."""

    caption = figure_caption(block, label) if block.caption is not None and label else None
    caption_height = (
        FIGURE_CAPTION_GAP_PX + _estimate_text_height(caption, _typography(style), span_px)
        if caption
        else 0.0
    )
    try:
        data, info = load_png(Path(block.path))
    except ImageError as error:
        raise LayoutError(f"image block {block.id or block.path!r}: {error}") from error
    width, height = image_display_size(
        info,
        width_mm=block.width_mm,
        span_px=span_px,
        max_height_px=usable_height_px - caption_height,
    )
    return FigureGeometry(data, width, height, caption, caption_height)


def numbered_heading(block: HeadingBlock, label: str | None) -> HeadingBlock:
    """The heading as drawn: its number, a space, then its text."""

    return block if label is None else block.model_copy(update={"text": f"{label} {block.text}"})


@dataclass(frozen=True, slots=True)
class ContentsEntry:
    block_index: int
    label: str
    text: str


def contents_entries(
    blocks: Sequence[Block], depth: int, labels: Mapping[int, str]
) -> tuple[ContentsEntry, ...]:
    """Numbered headings at or above ``depth``, in document order."""

    return tuple(
        ContentsEntry(index, labels[index], block.text)
        for index, block in enumerate(blocks)
        if isinstance(block, HeadingBlock) and index in labels and block.level <= depth
    )


CONTENTS_PAGE_COLUMN_PX = 40.0
# Prime renders star-sized FlowDocument table columns at zero width, so every
# column is fixed; the slack keeps the table inside the region's text inset.
_CONTENTS_SLACK_PX = 16.0


def contents_columns(width: float, depth: int, font_size_px: float) -> tuple[float, float, float]:
    """Fixed (number, title, page) column widths for a contents table."""

    number = font_size_px * (1.0 + 1.1 * depth)
    title = width - number - CONTENTS_PAGE_COLUMN_PX - _CONTENTS_SLACK_PX
    return number, title, CONTENTS_PAGE_COLUMN_PX


def _block_id(block: Block, index: int) -> str:
    return block.id or f"block-{index:04d}"


def _block_indent(block: Block) -> int:
    if isinstance(block, PageBreakBlock):
        return 0
    return block.indent


def _row_visual_tops(
    row_cursor: float,
    row_before: float,
    anchor_offsets: list[float],
    grid_px: float,
) -> list[float]:
    return [
        _snap_up(row_cursor + row_before + offset, grid_px) - offset for offset in anchor_offsets
    ]


def estimate_inline_math_size(
    expression: str,
    unit: str | None,
    font_size_px: float,
    *,
    evaluate: bool = False,
) -> tuple[float, float]:
    """Reserve Prime-calibrated geometry for one inline Math50 expression."""

    if unit is None and not evaluate:
        try:
            inline_root = convert_python_expression_to_math50(expression)
        except (SyntaxError, NotImplementedError, ValueError):
            inline_root = None
        if inline_root is not None and etree.QName(inline_root).localname == "id":
            width_em = max(0.8, _identifier_width_em(inline_root) + 0.25)
            return font_size_px * width_em, font_size_px * 1.15
        return estimate_display_math_size(expression, font_size_px)

    if _is_definition(expression):
        # `x := expr = value unit`: the definition's own geometry plus an
        # estimated `= value unit` tail (evaluated widths remain estimates).
        width, height = estimate_display_math_size(expression, font_size_px)
        tail_em = 4.0 + 0.37 * len(unit or "")
        unit_height_em = 2.6 if unit is not None and "/" in unit else 1.35
        return width + font_size_px * tail_em, max(height, font_size_px * unit_height_em)
    if unit is None:
        return estimate_display_math_size(expression, font_size_px)

    width_factor = 2.9 + 0.37 * (len(expression) + len(unit))
    height_factor = 2.6 if "/" in unit else 1.35
    return font_size_px * width_factor, font_size_px * height_factor


def _is_definition(expression: str) -> bool:
    try:
        nodes = convert_python_to_math50(expression)
    except (SyntaxError, NotImplementedError, ValueError):
        return False
    return len(nodes) == 1 and nodes[0] is not None and _local_name(nodes[0]) == "define"


def _local_name(element: etree._Element) -> str:
    return etree.QName(element).localname


def _element_text(element: etree._Element) -> str:
    return "".join(cast(Iterable[str], element.itertext()))


def _identifier_width_em(element: etree._Element) -> float:
    """Estimate one Math50 identifier using Prime's base/subscript proportions."""

    text = _element_text(element).strip()
    subscript = "".join(
        _element_text(descendant)
        for descendant in element.iterdescendants()
        if _local_name(descendant) == "Subscript"
    )
    base_count = max(0, len(text) - len(subscript))
    base_factor = 0.52 if element.get("labels") == "UNIT" else 0.55
    return max(base_factor, base_count * base_factor + len(subscript) * 0.30)


def _math_width_em(element: etree._Element) -> float:
    """Recursively measure rendered Math50 width in font-size units."""

    name = _local_name(element)
    children = list(element)
    if name == "id":
        return _identifier_width_em(element)
    if name in {"real", "str"}:
        return max(0.55, len(_element_text(element).strip()) * 0.55)
    if name in {"define", "localDefine"}:
        return sum(_math_width_em(child) for child in children) + 1.48
    if name == "eval":
        expression = next(
            (child for child in children if _local_name(child) != "unitOverride"),
            None,
        )
        # Evaluated values are unavailable during semantic layout. Keep an
        # explicit result allowance and retain the report's estimated flag.
        return (0.55 if expression is None else _math_width_em(expression)) + 2.40
    if name == "apply" and children:
        operator = _local_name(children[0])
        operands = children[1:]
        widths = [_math_width_em(operand) for operand in operands]
        if operator == "div" and widths:
            return max(widths) + 0.30
        if operator == "mult":
            return sum(widths) + max(0, len(widths) - 1) * 0.90
        if operator == "scale":
            return sum(widths) + max(0, len(widths) - 1) * 0.49
        if operator in {"plus", "minus"}:
            return sum(widths) + max(1, len(widths) - 1) * 0.80
        if operator == "pow" and len(widths) >= 2:
            return widths[0] + widths[1] * 0.65 + 0.10
        if operator == "sqrt" and widths:
            return widths[0] + _RADICAL_SIGN_WIDTH_EM
        return sum(widths) + 0.70
    if name in {"sequence", "parens"}:
        return sum(_math_width_em(child) for child in children) + 0.40
    return sum((_math_width_em(child) for child in children), start=0.0) or 0.55


def _contains_operator(element: etree._Element, operator: str) -> bool:
    return any(_local_name(candidate) == operator for candidate in element.iter())


# Vertical extent calibrated against Prime-saved actualHeight of 24 display
# shapes (docs/format/worksheet-layout-and-authoring.md §10). Extents are em
# above/below one plain line; a region adds _MATH_PAD_EM of padding.
_MATH_PAD_EM = 0.59
_FRACTION_GAP_EM = 0.32
_SUBSCRIPT_DESCENT_EM = 0.165
_SUPERSCRIPT_SCALE = 0.735
_SUPERSCRIPT_OFFSET_EM = 0.555
_SUPERSCRIPT_OVER_EM = 0.05
_RADICAL_OVER_EM = 0.19
_RADICAL_MIN_ABOVE_EM = 0.40
_RADICAL_SIGN_WIDTH_EM = 1.0


def _math_extent_em(element: etree._Element) -> tuple[float, float]:
    """Rendered (above, below) extent of a Math50 element beyond one plain line."""

    name = _local_name(element)
    children = list(element)
    if name == "id":
        subscripted = any(_local_name(d) == "Subscript" for d in element.iterdescendants())
        return 0.0, _SUBSCRIPT_DESCENT_EM if subscripted else 0.0
    if name == "apply" and len(children) == 3 and _local_name(children[0]) == "div":
        numerator, denominator = (_math_height_em(child) for child in children[1:])
        half_gap = _FRACTION_GAP_EM / 2
        return numerator + half_gap - 0.5, denominator + half_gap - 0.5
    if name == "apply" and len(children) == 3 and _local_name(children[0]) == "pow":
        # The exponent's box sits on the base's top (a tall exponent over a
        # tall base rises by its whole extra height), or on the plain line.
        base_above, base_below = _math_extent_em(children[1])
        exponent = _math_height_em(children[2])
        on_base = base_above + _SUPERSCRIPT_OVER_EM + _SUPERSCRIPT_SCALE * (exponent - 1.0)
        raised = _SUPERSCRIPT_SCALE * exponent - _SUPERSCRIPT_OFFSET_EM
        return max(on_base, raised), base_below
    if name == "apply" and children and _local_name(children[0]) in {"sqrt", "nthRoot"}:
        # The radicand is the last operand (nthRoot stores its index first);
        # the sign's bar clears the radicand, and never sits below a plain root.
        above, below = _math_extent_em(children[-1])
        return max(above + _RADICAL_OVER_EM, _RADICAL_MIN_ABOVE_EM), below
    extents = [_math_extent_em(child) for child in children]
    return max((a for a, _ in extents), default=0.0), max((b for _, b in extents), default=0.0)


def _math_height_em(element: etree._Element) -> float:
    above, below = _math_extent_em(element)
    return 1.0 + above + below


# Program geometry calibrated against Prime-saved actualHeight/actualWidth of 14
# program shapes at the 11 pt math font (docs/format/worksheet-layout-and-
# authoring.md §10): heights fit within 0.84 px, widths within 7.4 px. Values
# are Prime pixels at that font and scale with the font size.
_CALIBRATION_FONT_PX = 11.0 * PX_PER_PT
_PROGRAM_STATEMENT_PX = 16.27
_PROGRAM_CONTROL_PX = 23.20
_PROGRAM_BASE_PX = 15.39 + 1.0  # + safety so no calibrated shape is under-reserved
_PROGRAM_WIDTH_PAD_PX = 46.7 + 7.4  # + the largest calibrated under-estimate
_PROGRAM_DEPTH_EM = 1.1
_PROGRAM_KEYWORD_EM = {"if": 1.8, "elseif": 3.78, "else": 2.34, "for": 2.34, "while": 3.24}


def _row_extra_em(element: etree._Element) -> float:
    return _math_height_em(element) - 1.0


def _program_rows(
    statements: Iterable[etree._Element], depth: int, rows: list[tuple[str, float, int, float]]
) -> None:
    """Flatten a program into visible rows: (kind, content em, depth, extra height em)."""

    for statement in statements:
        kind = _local_name(statement)
        if kind == "if":
            for clause in statement:
                clause_kind = _local_name(clause)
                if clause_kind == "test":
                    rows.append(("if", _math_width_em(clause[0]), depth, _row_extra_em(clause[0])))
                elif clause_kind == "then":
                    _program_rows(clause[0], depth + 1, rows)
                elif clause_kind == "elseif":
                    test, then = clause[0], clause[1]
                    rows.append(("elseif", _math_width_em(test[0]), depth, _row_extra_em(test[0])))
                    _program_rows(then[0], depth + 1, rows)
                elif clause_kind == "else":
                    rows.append(("else", 0.0, depth, 0.0))
                    _program_rows(clause[0], depth + 1, rows)
        elif kind in {"for", "while"}:
            parts = list(statement)[:-1]
            header = sum(_math_width_em(part) for part in parts) + (0.8 if kind == "for" else 0.0)
            extra = max((_row_extra_em(part) for part in parts), default=0.0)
            rows.append((kind, header, depth, extra))
            _program_rows(statement[-1], depth + 1, rows)
        else:
            rows.append(
                (
                    "statement",
                    _math_width_em(statement),
                    depth,
                    _row_extra_em(statement),
                )
            )


def estimate_program_math_size(
    root: etree._Element,
    font_size_px: float,
) -> tuple[float, float]:
    """Reserve a program expression from its visible rows (Prime-calibrated)."""

    head_em = 0.0
    body = root
    if _local_name(root) in {"define", "localDefine"} and len(root) == 2:
        head_em = _math_width_em(root[0])
        body = root[1]
    rows: list[tuple[str, float, int, float]] = []
    _program_rows(body if _local_name(body) == "program" else (body,), 0, rows)
    scale = font_size_px / _CALIBRATION_FONT_PX
    statements = sum(1 for kind, *_ in rows if kind == "statement")
    controls = len(rows) - statements
    extra_em = sum(extra for *_, extra in rows)
    height = (
        statements * _PROGRAM_STATEMENT_PX + controls * _PROGRAM_CONTROL_PX + _PROGRAM_BASE_PX
    ) * scale + extra_em * font_size_px
    widest_em = max(
        width + _PROGRAM_KEYWORD_EM.get(kind, 0.0) + _PROGRAM_DEPTH_EM * depth
        for kind, width, depth, _ in rows
    )
    width = (head_em + widest_em) * font_size_px + _PROGRAM_WIDTH_PAD_PX * scale
    return width, height


def estimate_display_math_size(expression: str, font_size_px: float) -> tuple[float, float]:
    """Measure a display expression from its rendered Math50 operator tree."""

    try:
        nodes = convert_python_to_math50(expression)
    except (SyntaxError, NotImplementedError, ValueError):
        # Rendering owns the public unsupported-expression error contract. A
        # conservative fallback lets it raise RenderError at the established
        # boundary without restoring length-based sizing for valid math.
        return max(2.5, len(expression) * 0.55) * font_size_px, 1.59 * font_size_px
    if len(nodes) != 1 or nodes[0] is None:
        return max(2.5, len(expression) * 0.55) * font_size_px, 1.59 * font_size_px
    return estimate_math_element_size(nodes[0], font_size_px)


def estimate_math_element_size(root: etree._Element, font_size_px: float) -> tuple[float, float]:
    """Measure one Math50 element: programs by visible rows, others by operator tree."""

    if _contains_operator(root, "program") or _contains_operator(root, "if"):
        return estimate_program_math_size(root, font_size_px)
    width = max(2.5, _math_width_em(root)) * font_size_px
    return width, font_size_px * (_math_height_em(root) + _MATH_PAD_EM)


PICTURE_ANCHOR_OFFSET_PX = -12.0
"""Stored top minus planned top of a picture: Prime paints a picture 12 px lower
than a text region with the same stored top (render probe, 2026-09-23)."""

MATH_ROW_INDENT_PX = 20
"""First-line indent of calculation rows (template-sheet-styles ``TextIndent="20"``)."""


def math_only_paragraphs(runs: Sequence[TextRun | InlineMathRun]) -> tuple[bool, ...]:
    """Flag each newline-delimited paragraph that holds only inline math."""

    flags: list[bool] = []
    has_math = has_text = False
    for run in runs:
        if isinstance(run, InlineMathRun):
            has_math = True
            continue
        pieces = run.text.split("\n")
        for piece_index, piece in enumerate(pieces):
            has_text = has_text or bool(piece.strip())
            if piece_index < len(pieces) - 1:
                flags.append(has_math and not has_text)
                has_math = has_text = False
    flags.append(has_math and not has_text)
    return tuple(flags)


_TALL_ABOVE_EM = 0.5
"""A maths row rising this far above a plain line (a fraction) gets a blank line above it."""


def _rises_above_line(run: InlineMathRun) -> bool:
    if run.unit is not None and "/" in run.unit:
        return True
    try:
        nodes = convert_python_to_math50(run.expression)
    except (SyntaxError, NotImplementedError, ValueError):
        return False
    if len(nodes) != 1 or nodes[0] is None:
        return False
    root = nodes[0]
    if _contains_operator(root, "program") or _contains_operator(root, "if"):
        return True
    return _math_extent_em(root)[0] >= _TALL_ABOVE_EM


def spaced_runs(
    runs: Sequence[TextRun | InlineMathRun],
) -> tuple[TextRun | InlineMathRun, ...]:
    """Insert blank paragraphs that give calculation rows room to breathe.

    A blank line separates a run of maths rows from the prose after it, and
    precedes any maths row that rises above a plain line. Author-written blank
    paragraphs are kept, never doubled.
    """

    paragraphs: list[list[TextRun | InlineMathRun]] = [[]]
    for run in runs:
        if isinstance(run, InlineMathRun):
            paragraphs[-1].append(run)
            continue
        pieces = run.text.split("\n")
        for piece_index, piece in enumerate(pieces):
            if piece:
                paragraphs[-1].append(run.model_copy(update={"text": piece}))
            if piece_index < len(pieces) - 1:
                paragraphs.append([])

    def is_blank(paragraph: list[TextRun | InlineMathRun]) -> bool:
        return all(isinstance(r, TextRun) and not r.text.strip() for r in paragraph)

    math_rows = math_only_paragraphs(runs)
    spaced: list[TextRun | InlineMathRun] = []
    for index, paragraph in enumerate(paragraphs):
        if index:
            previous = paragraphs[index - 1]
            needs_gap = (
                not is_blank(previous)
                and not is_blank(paragraph)
                and (
                    (math_rows[index - 1] and not math_rows[index])
                    or (
                        math_rows[index]
                        and any(
                            isinstance(r, InlineMathRun) and _rises_above_line(r) for r in paragraph
                        )
                    )
                )
            )
            spaced.append(TextRun(kind="text", text="\n\n" if needs_gap else "\n"))
        spaced.extend(paragraph)
    return tuple(spaced)


# A text line holding inline maths is at least Prime's plain inline-maths line
# and grows by the maths' extent beyond one plain line, in em of the 11 pt math
# font; the region padding does not apply inside a line. Calibrated against
# Prime-saved text regions (docs/format/worksheet-layout-and-authoring.md §10).
_INLINE_MATH_FONT_PX = 11.0 * PX_PER_PT
_INLINE_MATH_LINE_PX = 15.34


def _inline_rise_em(run: InlineMathRun) -> float | None:
    """Extent beyond one plain line of an inline expression and its display unit.

    ``None`` for a program (a Python conditional lowers to one), whose rows the
    extent model does not measure.
    """

    elements: list[etree._Element] = []
    with contextlib.suppress(SyntaxError, NotImplementedError, ValueError):
        elements.extend(
            node for node in convert_python_to_math50(run.expression) if node is not None
        )
    if run.unit is not None:
        with contextlib.suppress(SyntaxError, NotImplementedError, ValueError):
            elements.append(convert_python_expression_to_math50(run.unit))
    if any(_contains_operator(element, "program") for element in elements):
        return None
    extents = [_math_extent_em(element) for element in elements]
    return max((a for a, _ in extents), default=0.0) + max((b for _, b in extents), default=0.0)


# Prime wraps prose by word inside the FlowDocument; fitted against resaved
# paragraphs growing one word at a time (docs/format/worksheet-layout-and-
# authoring.md §10). Widths: Windows Arial, pymcdx/data/fonts.
_WRAP_PADDING_PX = 12.0  # upper end of the fitted 9.5-12 px: wrap early rather than overlap
_FALLBACK_ADVANCE_EM = 0.556
# Prime's inline math is up to ~20 % wider than the width estimate (152
# resaved inline regions: median 1.04, 95th percentile 1.21 of the text-size
# estimate). Measured at the math font plus this margin, a borderline token
# wraps early (a little spare space) rather than late (an overlap).
_INLINE_WRAP_MARGIN = 1.1


@cache
def _advance_widths() -> dict[str, dict[str, float]]:
    source = resources.files("pymcdx").joinpath("data", "fonts", "layout-advance-widths.json")
    table = json.loads(source.read_text(encoding="utf-8"))
    return cast(dict[str, dict[str, float]], table["widths"])


def _text_width_px(text: str, typography: _Typography) -> float:
    face = {
        (False, False): "regular",
        (True, False): "bold",
        (False, True): "italic",
        (True, True): "bold_italic",
    }[(typography.bold, typography.italic)]
    widths = _advance_widths()[face]
    return typography.font_size_px * sum(widths.get(ch, _FALLBACK_ADVANCE_EM) for ch in text)


def _estimate_text_height(
    block: HeadingBlock | TextBlock,
    typography: _Typography,
    width: float,
    styles: Mapping[str, StyleToken] | None = None,
) -> float:
    """Wrapped height; a run's own style sets its font and its line's height."""

    available = max(1.0, width - _WRAP_PADDING_PX)
    runs: Sequence[TextRun | InlineMathRun] = (
        [TextRun(kind="text", text=block.text)]
        if isinstance(block, HeadingBlock)
        else spaced_runs(block.runs)
    )
    math_rows = math_only_paragraphs(runs)
    paragraph = 0
    total_height = 0.0
    line_start = MATH_ROW_INDENT_PX if math_rows[0] else 0.0
    line_width = line_start
    line_height = 0.0

    def finish_line() -> None:
        nonlocal total_height, line_width, line_start, line_height
        total_height += line_height or typography.line_height_px
        line_width = line_start = 0.0
        line_height = 0.0

    def add_token(token_width: float, *, breakable: bool = True) -> None:
        nonlocal line_width
        if breakable and line_width > line_start and line_width + token_width > available:
            finish_line()
        while line_width + token_width > available and token_width > 0:
            # A token wider than the line breaks across lines.
            token_width -= available - line_width
            finish_line()
        line_width += token_width

    def add_text(text: str, face: _Typography) -> None:
        nonlocal line_width, line_height
        for piece in re.split(r"( +)", text):
            if not piece:
                continue
            if piece.startswith(" "):
                # Spaces are break opportunities and never start a line.
                if line_width > line_start:
                    line_width += _text_width_px(piece, face)
                continue
            add_token(_text_width_px(piece, face))
            line_height = max(line_height, face.line_height_px)

    for run in runs:
        if isinstance(run, TextRun):
            run_style = (styles or {}).get(run.style) if run.style else None
            face = typography if run_style is None else _typography(run_style)
            pieces = run.text.split("\n")
            for piece_index, piece in enumerate(pieces):
                add_text(piece, face)
                if piece_index < len(pieces) - 1:
                    finish_line()
                    paragraph += 1
                    if math_rows[paragraph]:
                        line_width = line_start = MATH_ROW_INDENT_PX
            continue

        # Inline maths renders at the math font whatever the text size, so its
        # wrap width is measured there.
        reserved, _ = estimate_inline_math_size(
            run.expression, run.unit, _INLINE_MATH_FONT_PX, evaluate=run.evaluate
        )
        add_token(min(reserved * _INLINE_WRAP_MARGIN, available))
        rise = _inline_rise_em(run)
        if rise is None:
            # A program keeps its calibrated estimate at the text size.
            _, row_height = estimate_inline_math_size(
                run.expression, run.unit, typography.font_size_px, evaluate=run.evaluate
            )
        else:
            row_height = _INLINE_MATH_LINE_PX + _INLINE_MATH_FONT_PX * rise
        line_height = max(line_height, typography.line_height_px, row_height)

    finish_line()
    return max(typography.line_height_px, total_height)


class TwoPassLayout:
    """Measure intrinsic block sizes, then place them on a snapped page grid."""

    def __init__(
        self,
        document: AuthoringDocument | None = None,
        *,
        page: WorksheetSettings | None = None,
        styles: Mapping[str, StyleToken] | None = None,
        grid_size: str | None = None,
    ) -> None:
        self.document = document
        self.page = page_model_from(page or document, grid_size=grid_size)
        self.styles = (
            resolved_styles(document) if styles is None and document is not None else (styles or {})
        )

    def snap(self, value: int | float) -> float:
        """Snap one content-relative coordinate to the active grid."""

        return _snap(_finite(value, "coordinate"), self.page.grid_px)

    def measure_block(
        self,
        block: Block,
        *,
        index: int = 0,
        label: str | None = None,
        entries: Sequence[ContentsEntry] = (),
    ) -> MeasuredBlock:
        """Estimate one block's intrinsic dimensions in WPF pixels.

        ``label`` is a heading's number and ``entries`` a contents block's
        headings; :meth:`measure` supplies both from the whole document.
        """

        block_id = _block_id(block, index)
        style_id, style = _style_for(block, self.styles)
        indent_px = _block_indent(block) * self.page.grid_px
        full_width = self.page.content_width_px - indent_px
        half_width = (self.page.content_width_px - 2.0 * self.page.grid_px) / 2.0 - indent_px
        if isinstance(block, PageBreakBlock):
            return MeasuredBlock(block_id, "page-break", 0.0, 0.0, False, style_id)
        if full_width <= 0 or half_width <= 0:
            raise LayoutError(f"block {block_id!r} indent leaves no positive horizontal span")
        if isinstance(block, HeadingBlock):
            typography = _typography(style, level=block.level)
            width = half_width if style is not None and style.width == "half" else full_width
            height = _estimate_text_height(numbered_heading(block, label), typography, width)
            return MeasuredBlock(
                block_id,
                "heading",
                width,
                height,
                True,
                style_id,
            )
        if isinstance(block, TextBlock):
            typography = _typography(style)
            width = half_width if style is not None and style.width == "half" else full_width
            height = _estimate_text_height(block, typography, width, self.styles)
            return MeasuredBlock(
                block_id,
                "text",
                width,
                height,
                True,
                style_id,
            )
        if isinstance(block, MathBlock):
            typography = _typography(style)
            width, height = estimate_display_math_size(
                block.expression,
                typography.font_size_px,
            )
            if width > full_width + 1e-6:
                raise LayoutError(
                    f"block {block_id!r} intrinsic width {width:.3f} exceeds indented "
                    f"span {full_width:.3f}"
                )
            if style is not None and style.width == "half":
                width = half_width
            return MeasuredBlock(block_id, "math", width, height, True, style_id)
        if isinstance(block, FunctionBlock):
            typography = _typography(style)
            width, height = estimate_math_element_size(
                render_function_block(block.source), typography.font_size_px
            )
            if width > full_width + 1e-6:
                raise LayoutError(
                    f"block {block_id!r} intrinsic width {width:.3f} exceeds indented "
                    f"span {full_width:.3f}"
                )
            return MeasuredBlock(block_id, "function", width, height, True, style_id)
        if isinstance(block, CalculationBlock):
            typography = _typography(style)
            width = min(
                full_width,
                max(
                    typography.font_size_px * 5.0,
                    len(block.entry_id) * typography.font_size_px * 0.62,
                ),
            )
            # Provider results may include a stacked display unit; layout runs
            # before the provider response, so reserve the conservative case.
            height = typography.font_size_px * 3.1
            if style is not None and style.width == "half":
                width = half_width
            return MeasuredBlock(block_id, "calculation", width, height, True, style_id)
        if isinstance(block, ContentsBlock):
            typography = _typography(style)
            caption = _typography(self.styles.get("heading1"), level=1)
            _, title_width, _ = contents_columns(full_width, block.depth, typography.font_size_px)
            max_chars = max(1, int(title_width / (typography.font_size_px * 0.46)))
            lines = sum(max(1, math.ceil(len(entry.text) / max_chars)) for entry in entries)
            height = caption.line_height_px + max(1, lines) * typography.line_height_px
            return MeasuredBlock(block_id, "contents", full_width, height, True, style_id)
        if isinstance(block, ImageBlock):
            figure = figure_geometry(
                block,
                label,
                style,
                span_px=full_width,
                usable_height_px=self.page.content_height_px - 2.0 * self.page.edge_clearance_px,
            )
            height = figure.height + figure.caption_height
            return MeasuredBlock(block_id, "image", full_width, height, False, style_id)
        raise LayoutError(f"unsupported block type {type(block).__name__}")

    def measure(self, document: AuthoringDocument | None = None) -> tuple[MeasuredBlock, ...]:
        """First pass: return deterministic dimensions for every block."""

        source = document if document is not None else self.document
        if source is None:
            raise LayoutError("measure requires an AuthoringDocument")
        source = lower_blocks(source)
        labels = heading_labels(source.blocks)
        figures = figure_labels(source.blocks)
        return tuple(
            self.measure_block(
                block,
                index=index,
                label=figures.get(index, labels.get(index)),
                entries=contents_entries(source.blocks, block.depth, labels)
                if isinstance(block, ContentsBlock)
                else (),
            )
            for index, block in enumerate(source.blocks)
        )

    def _following_height(
        self,
        source: AuthoringDocument,
        measured: tuple[MeasuredBlock, ...],
        index: int,
    ) -> float:
        """Height of the blocks a heading must share its page with.

        Consecutive headings chain, so a heading run stays with the first body
        block after it. Spacing is the collapsed gap before each block.
        """

        total = 0.0
        while index < len(source.blocks):
            block = source.blocks[index]
            if isinstance(block, PageBreakBlock):
                break
            _style_id, style = _style_for(block, self.styles)
            typography = _typography(
                style, level=block.level if isinstance(block, HeadingBlock) else None
            )
            height = measured[index].height
            if (
                style is not None
                and style.width == "half"
                and index + 1 < len(source.blocks)
                and not isinstance(source.blocks[index + 1], PageBreakBlock)
            ):
                # A half-width pair shares one row as tall as its taller column.
                _, partner = _style_for(source.blocks[index + 1], self.styles)
                if partner is not None and partner.width == "half":
                    height = max(height, measured[index + 1].height)
            total += typography.space_before_px + height
            if not isinstance(block, HeadingBlock):
                break
            index += 1
        return total

    def layout_document(self, document: AuthoringDocument | None = None) -> LayoutResult:
        """Measure and place a document, failing before XML is emitted."""

        source = document if document is not None else self.document
        if source is None:
            raise LayoutError("layout requires an AuthoringDocument")
        source = lower_blocks(source)
        measured = self.measure(source)
        placements: list[Placement] = []
        by_page: dict[int, list[Placement]] = {}
        cursor = 0.0
        # The previous row's after-space; adjacent spacing collapses to the
        # larger of it and the next row's before-space, as CSS margins do.
        pending_after = 0.0
        page_index = 0

        def is_half(intrinsic: MeasuredBlock) -> bool:
            style = self.styles.get(intrinsic.style_id) if intrinsic.style_id else None
            return style is not None and style.width == "half"

        index = 0
        while index < len(source.blocks):
            block = source.blocks[index]
            intrinsic = measured[index]
            if isinstance(block, PageBreakBlock):
                placements.append(
                    Placement(
                        intrinsic.block_id,
                        "page-break",
                        page_index,
                        (page_index + 1) * self.page.page_stride_px,
                        0.0,
                        self.page.paper_width_px,
                        2.0,
                        False,
                        None,
                        True,
                    )
                )
                page_index += 1
                cursor = page_index * self.page.page_stride_px
                pending_after = 0.0
                index += 1
                continue

            row: list[tuple[Block, MeasuredBlock]] = [(block, intrinsic)]
            if (
                is_half(intrinsic)
                and index + 1 < len(source.blocks)
                and not isinstance(source.blocks[index + 1], PageBreakBlock)
                and is_half(measured[index + 1])
            ):
                row.append((source.blocks[index + 1], measured[index + 1]))

            typography: list[_Typography] = []
            anchor_offsets: list[float] = []
            for row_block, _ in row:
                _style_id, style = _style_for(row_block, self.styles)
                item_typography = _typography(
                    style,
                    level=row_block.level if isinstance(row_block, HeadingBlock) else None,
                )
                typography.append(item_typography)
                if isinstance(row_block, (MathBlock, FunctionBlock, CalculationBlock)):
                    anchor_offsets.append(math_anchor_offset(item_typography.font_size_px))
                elif isinstance(row_block, ImageBlock):
                    anchor_offsets.append(PICTURE_ANCHOR_OFFSET_PX)
                else:
                    anchor_offsets.append(0.0)

            row_before = max(item.space_before_px for item in typography)
            tallest = max(range(len(row)), key=lambda item: row[item][1].height)
            row_after = typography[tallest].space_after_px
            row_height = max(item[1].height for item in row)

            page_origin = page_index * self.page.page_stride_px
            # Page-start spacing collapses into the safe band: a zero-before
            # body region and a 5 mm-before heading both start at 5 mm, while a
            # deliberately larger before-space remains larger.  Inside a page,
            # before-spacing collapses with the previous row's after-spacing.
            row_start = max(
                cursor + max(pending_after, row_before),
                page_origin + self.page.edge_clearance_px,
            )
            item_tops = _row_visual_tops(
                row_start,
                0.0,
                anchor_offsets,
                self.page.grid_px,
            )
            row_bottom = max(
                item_top + item.height for item_top, (_, item) in zip(item_tops, row, strict=True)
            )
            # Keep a heading with what follows it: if the heading and its
            # following block cannot share this page, start the page at the heading.
            keep_bottom = row_bottom
            if isinstance(block, HeadingBlock) and row_start > (
                page_origin + self.page.edge_clearance_px + 1e-6
            ):
                keep_bottom += self._following_height(source, measured, index + 1)
            if (
                keep_bottom - page_origin
                > self.page.content_height_px - self.page.edge_clearance_px + 1e-6
            ):
                page_index += 1
                page_origin = page_index * self.page.page_stride_px
                cursor = page_origin
                row_start = max(
                    cursor + row_before,
                    page_origin + self.page.edge_clearance_px,
                )
                item_tops = _row_visual_tops(
                    row_start,
                    0.0,
                    anchor_offsets,
                    self.page.grid_px,
                )
                row_bottom = max(
                    item_top + item.height
                    for item_top, (_, item) in zip(item_tops, row, strict=True)
                )
            usable_height = self.page.content_height_px - 2.0 * self.page.edge_clearance_px
            if row_height > usable_height + 1e-6:
                raise LayoutError(
                    f"row height {row_height:.3f} exceeds edge-safe content height "
                    f"{usable_height:.3f}"
                )

            row_placements: list[Placement] = []
            half_width = (self.page.content_width_px - 2.0 * self.page.grid_px) / 2.0
            for slot, (row_block, item) in enumerate(row):
                item_top = item_tops[slot]
                local_top = item_top - page_origin
                if item.width > self.page.content_width_px + 1e-6:
                    raise LayoutError(
                        f"block {item.block_id!r} width {item.width:.3f} exceeds "
                        f"content width {self.page.content_width_px:.3f}"
                    )
                column_left = (
                    slot * (half_width + 2.0 * self.page.grid_px) if len(row) == 2 else 0.0
                )
                left = column_left + _block_indent(row_block) * self.page.grid_px
                right_bound = (
                    column_left + half_width if is_half(item) else self.page.content_width_px
                )
                if left + item.width > right_bound + 1e-6:
                    raise LayoutError(
                        f"block {item.block_id!r} extends past its indented horizontal span"
                    )
                placed = Placement(
                    item.block_id,
                    item.kind,
                    page_index,
                    item_top,
                    left,
                    item.width,
                    item.height,
                    item.estimated,
                    item.style_id,
                    anchor_offset=anchor_offsets[slot],
                )
                if (
                    local_top < self.page.edge_clearance_px - 1e-6
                    or local_top + placed.height
                    > self.page.content_height_px - self.page.edge_clearance_px + 1e-6
                ):
                    raise LayoutError(f"block {item.block_id!r} overflows page at {local_top:.3f}")
                for previous in by_page.setdefault(page_index, []):
                    previous_top = previous.top - page_index * self.page.page_stride_px
                    if (
                        placed.left < previous.right - 1e-6
                        and previous.left < placed.right - 1e-6
                        and local_top < previous_top + previous.height - 1e-6
                        and previous_top < local_top + placed.height - 1e-6
                    ):
                        raise LayoutError(
                            f"block {item.block_id!r} overlaps block {previous.block_id!r}"
                        )
                row_placements.append(placed)

            placements.extend(row_placements)
            by_page.setdefault(page_index, []).extend(row_placements)
            cursor = max(placement.bottom for placement in row_placements)
            pending_after = row_after
            index += len(row)

        page_count = max(
            page_index + 1, max((placement.page for placement in placements), default=0) + 1
        )
        estimated_text = any(
            placement.estimated and placement.kind in {"heading", "text"}
            for placement in placements
        )
        return LayoutResult(self.page, tuple(placements), measured, page_count, estimated_text)

    def place(self, document: AuthoringDocument | None = None) -> LayoutResult:
        """Alias for :meth:`layout_document`."""

        return self.layout_document(document)

    def layout(self, document: AuthoringDocument | None = None) -> LayoutResult:
        """Concise alias for the two-pass layout."""

        return self.layout_document(document)


__all__ = [
    "A4_HEIGHT_PX",
    "A4_WIDTH_PX",
    "DEFAULT_MARGIN_PX",
    "GRID_PX",
    "MATH_ROW_INDENT_PX",
    "PICTURE_ANCHOR_OFFSET_PX",
    "ContentsEntry",
    "FigureGeometry",
    "LayoutError",
    "LayoutResult",
    "MeasuredBlock",
    "PageModel",
    "Placement",
    "TwoPassLayout",
    "contents_columns",
    "contents_entries",
    "estimate_display_math_size",
    "estimate_inline_math_size",
    "estimate_math_element_size",
    "estimate_program_math_size",
    "figure_caption",
    "figure_geometry",
    "figure_labels",
    "heading_labels",
    "math_only_paragraphs",
    "numbered_heading",
    "page_model_from",
    "spaced_runs",
]
