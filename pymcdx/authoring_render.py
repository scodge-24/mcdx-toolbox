"""Lower semantic authoring models to worksheet and FlowDocument XML."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lxml import etree

from pymcdx.ast_converter import (
    convert_python_expression_to_math50,
    convert_python_to_math50,
    create_eval,
)
from pymcdx.authoring_checks import lower_blocks
from pymcdx.authoring_contracts import CalculationResult
from pymcdx.authoring_functions import render_function_block
from pymcdx.authoring_layout import (
    FIGURE_CAPTION_GAP_PX,
    MATH_ROW_INDENT_PX,
    ContentsEntry,
    LayoutResult,
    Placement,
    TwoPassLayout,
    contents_columns,
    contents_entries,
    estimate_inline_math_size,
    figure_geometry,
    figure_labels,
    heading_labels,
    math_only_paragraphs,
    numbered_heading,
    spaced_runs,
)
from pymcdx.authoring_models import (
    AuthoringDocument,
    Block,
    CalculationBlock,
    ContentsBlock,
    DocumentMetadata,
    FunctionBlock,
    HeadingBlock,
    ImageBlock,
    InlineMathRun,
    MathBlock,
    PageBreakBlock,
    StyleToken,
    TextBlock,
    TextRun,
    resolved_styles,
)
from pymcdx.builder import ML_NS, InlineMathSpec, PresentationPageModel, WorksheetBuilder
from pymcdx.flowdoc import FlowDocumentGenerator, FlowParagraph, FlowRun

PX_PER_PT = 96.0 / 72.0
WPF_NS = "http://schemas.microsoft.com/winfx/2006/xaml/presentation"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class RenderError(ValueError):
    """Raised when a semantic block cannot be lowered without loss."""


@dataclass(frozen=True, slots=True)
class AuthoringBuild:
    """Typed result of compiling a semantic document."""

    builder: WorksheetBuilder
    layout: LayoutResult

    @property
    def estimated_geometry(self) -> bool:
        """Whether any emitted region uses conservative geometry."""

        return self.layout.estimated_geometry

    @property
    def estimated_text_geometry(self) -> bool:
        """Whether the layout contains conservative text estimates."""

        return self.layout.estimated_text_geometry


@dataclass(frozen=True, slots=True)
class _RunStyle:
    token_id: str | None
    token: StyleToken | None


def _style_for(
    style_id: str | None,
    styles: Mapping[str, StyleToken],
) -> _RunStyle:
    if style_id is None:
        return _RunStyle(None, None)
    try:
        return _RunStyle(style_id, styles[style_id])
    except KeyError:
        raise RenderError(f"unknown style reference {style_id!r}") from None


def _default_style_id(block: Block) -> str | None:
    """Select a semantic profile token when a block omits an explicit style."""

    if isinstance(block, HeadingBlock):
        return f"heading{min(block.level, 3)}"
    if isinstance(block, TextBlock):
        return "body"
    if isinstance(block, (MathBlock, FunctionBlock)):
        return "math"
    if isinstance(block, CalculationBlock):
        return "result"
    if isinstance(block, ImageBlock):
        return "figure"
    return None


def _font_size_pt(style: StyleToken | None, level: int | None = None) -> float:
    if style is not None:
        return float(style.font_size_pt)
    if level == 1:
        return 16.0
    if level == 2:
        return 13.0
    if level in (3, 4):
        return 12.0
    return 10.0


def _flow_run(text: str, style: _RunStyle, *, level: int | None = None) -> FlowRun:
    token = style.token
    return FlowRun(
        text=text,
        style=style.token_id,
        font_family=None if token is None else token.font_family,
        font_size=_font_size_pt(token, level) * PX_PER_PT,
        font_weight=None if token is None or not token.bold else "Bold",
        font_style=None if token is None or not token.italic else "Italic",
        foreground=None if token is None else token.color,
    )


def _root_attributes(style: StyleToken | None, *, level: int | None = None) -> dict[str, str]:
    size = _font_size_pt(style, level) * PX_PER_PT
    return {
        "FontFamily": "Arial" if style is None else style.font_family,
        "FontStyle": "Normal" if style is None or not style.italic else "Italic",
        "FontWeight": "Normal" if style is None or not style.bold else "Bold",
        "FontSize": str(size),
        "Foreground": "#FF000000" if style is None else style.color,
        "Background": "#00FFFFFF",
        "TextAlignment": "Left",
        f"{{{XML_NS}}}lang": "en-gb",
        "Typography.Variants": "Normal",
    }


def _expression_element(expression: str) -> etree._Element:
    """Convert one Python-shaped expression to one Math50 expression node."""

    try:
        nodes = convert_python_to_math50(expression)
    except (SyntaxError, NotImplementedError, ValueError) as exc:
        raise RenderError(f"unsupported Mathcad expression {expression!r}: {exc}") from exc
    if len(nodes) != 1 or nodes[0] is None:
        raise RenderError(
            f"expression {expression!r} must lower to exactly one supported Math50 node"
        )
    return nodes[0]


def _inline_expression_element(
    expression: str,
    unit: str | None,
    *,
    evaluate: bool,
) -> etree._Element:
    """Lower notation-only names bare and retain eval/define semantics otherwise."""

    if unit is None and not evaluate:
        try:
            candidate = convert_python_expression_to_math50(expression)
        except (SyntaxError, NotImplementedError, ValueError):
            candidate = None
        if candidate is not None and etree.QName(candidate).localname == "id":
            if candidate.get("labels") == "VARIABLE":
                del candidate.attrib["labels"]
            if "label-is-contextual" in candidate.attrib:
                del candidate.attrib["label-is-contextual"]
            return candidate
    element = _expression_element(expression)
    if not evaluate and unit is None:
        return element
    if etree.QName(element).localname == "define":
        # Prime saves `x := expr = value unit` as define(id, eval(expr, unitOverride)).
        value = element[-1]
        if etree.QName(value).localname != "eval":
            element.remove(value)
            element.append(create_eval(value))
        return element
    if etree.QName(element).localname != "eval":
        raise RenderError("an evaluated inline expression must lower to Math50 eval")
    return element


def _inline_size(run: InlineMathRun, style: StyleToken | None) -> tuple[float, float]:
    size = _font_size_pt(style) * PX_PER_PT
    return estimate_inline_math_size(
        run.expression,
        run.unit,
        size,
        evaluate=run.evaluate,
    )


def _unit_expression(unit: str) -> etree._Element:
    """Lower a display unit and reject identifiers not known as Mathcad units."""

    unit_eval = _expression_element(unit)
    if etree.QName(unit_eval).localname != "eval":
        raise RenderError(f"display unit {unit!r} must be an evaluated unit expression")
    expression = next(
        (child for child in unit_eval if etree.QName(child).localname != "unitOverride"),
        None,
    )
    if expression is None:
        raise RenderError(f"display unit {unit!r} produced no unit expression")
    identifiers = list(expression.iter(f"{{{ML_NS}}}id"))
    if not identifiers or any(identifier.get("labels") != "UNIT" for identifier in identifiers):
        raise RenderError(f"display unit {unit!r} contains an unknown unit identifier")
    return expression


def _with_display_unit(expression: etree._Element, unit: str | None) -> etree._Element:
    if unit is None:
        return expression
    evaluated = expression
    if etree.QName(expression).localname == "define":
        evaluated = expression[-1]
    if etree.QName(evaluated).localname != "eval":
        raise RenderError("an inline display unit requires an evaluated expression")
    override = evaluated.find(f"{{{ML_NS}}}unitOverride")
    if override is None:
        raise RenderError("evaluated inline expression has no unit override")
    for child in tuple(override):
        override.remove(child)
    override.append(_unit_expression(unit))
    return expression


def _text_xaml(
    block: HeadingBlock | TextBlock,
    styles: Mapping[str, StyleToken],
) -> tuple[str, tuple[InlineMathSpec, ...]]:
    """Build prose XAML and the parallel caret-only inline Math50 specs."""

    block_style = _style_for(block.style or _default_style_id(block), styles)
    generator = FlowDocumentGenerator()
    paragraphs: list[FlowParagraph] = []
    inline_specs: list[InlineMathSpec] = []
    # WPF TextPointer offsets count text-container symbols, not only visible
    # characters. The first Paragraph start contributes one symbol; every Run
    # contributes its text plus start/end boundary symbols.
    caret = 1
    if isinstance(block, HeadingBlock):
        paragraphs.append(
            FlowParagraph(
                (_flow_run(block.text, block_style, level=block.level),),
            )
        )
    else:
        runs = spaced_runs(block.runs)
        math_rows = math_only_paragraphs(runs)

        def close(flow_runs: list[FlowRun]) -> FlowParagraph:
            indent = MATH_ROW_INDENT_PX if math_rows[len(paragraphs)] else None
            return FlowParagraph(tuple(flow_runs), text_indent=indent)

        current: list[FlowRun] = []
        for run in runs:
            run_style = _style_for(run.style or block.style or _default_style_id(block), styles)
            if isinstance(run, TextRun):
                pieces = run.text.split("\n")
                for piece_index, piece in enumerate(pieces):
                    if piece:
                        current.append(_flow_run(piece, run_style))
                        caret += len(piece) + 2
                    if piece_index < len(pieces) - 1:
                        paragraphs.append(close(current))
                        current = []
                        # Close the current Paragraph and enter the next one.
                        caret += 2
            elif isinstance(run, InlineMathRun):
                width, height = _inline_size(run, run_style.token)
                inline_specs.append(
                    InlineMathSpec(
                        expression=_with_display_unit(
                            _inline_expression_element(
                                run.expression,
                                run.unit,
                                evaluate=run.evaluate,
                            ),
                            run.unit,
                        ),
                        caret_position=caret,
                        width=width,
                        height=height,
                    )
                )
                # The reserved object slot is a one-character Run: one text
                # symbol plus its two element-boundary symbols.
                current.append(_flow_run(" ", run_style))
                caret += 3
        paragraphs.append(close(current))
    style = block_style.token
    xaml = generator.generate_rich_xaml(
        tuple(paragraphs),
        font_family="Arial" if style is None else style.font_family,
        font_size=_font_size_pt(style, block.level if isinstance(block, HeadingBlock) else None)
        * PX_PER_PT,
        font_weight="Normal" if style is None or not style.bold else "Bold",
        font_style="Normal" if style is None or not style.italic else "Italic",
        foreground="#FF000000" if style is None else style.color,
    )
    return xaml, tuple(inline_specs)


def _contents_xaml(
    block: ContentsBlock,
    entries: Sequence[ContentsEntry],
    pages: Mapping[int, int],
    width: float,
    styles: Mapping[str, StyleToken],
) -> str:
    """A caption and a fixed-width number | heading | page FlowDocument table."""

    body = _style_for(block.style or "body", styles)
    token = body.token
    font_px = _font_size_pt(token) * PX_PER_PT
    caption = FlowParagraph((_flow_run(block.title, _style_for("heading1", styles), level=1),))
    section = etree.fromstring(
        FlowDocumentGenerator()
        .generate_rich_xaml(
            (caption,),
            font_family="Arial" if token is None else token.font_family,
            font_size=font_px,
            foreground="#FF000000" if token is None else token.color,
        )
        .encode("utf-8")
    )
    table = etree.SubElement(section, f"{{{WPF_NS}}}Table", CellSpacing="0", Margin="0")
    columns = etree.SubElement(table, f"{{{WPF_NS}}}Table.Columns")
    for column_width in contents_columns(width, block.depth, font_px):
        etree.SubElement(columns, f"{{{WPF_NS}}}TableColumn", Width=format(column_width, ".15g"))
    group = etree.SubElement(table, f"{{{WPF_NS}}}TableRowGroup")
    for entry in entries:
        row = etree.SubElement(group, f"{{{WPF_NS}}}TableRow")
        cells = ((entry.label, None), (entry.text, None), (str(pages[entry.block_index]), "Right"))
        for text, alignment in cells:
            paragraph = etree.SubElement(
                etree.SubElement(row, f"{{{WPF_NS}}}TableCell"), f"{{{WPF_NS}}}Paragraph"
            )
            if alignment is not None:
                paragraph.set("TextAlignment", alignment)
            etree.SubElement(paragraph, f"{{{WPF_NS}}}Run").text = text
    return etree.tostring(section, encoding="utf-8").decode("utf-8")


@dataclass(frozen=True, slots=True)
class TitleBlockCell:
    """One text cell of the page-header title block, in header pixels."""

    key: str
    text: str
    left: float
    top: float
    width: float
    height: float
    font_size: float
    bold: bool = False
    alignment: str | None = None


# template-sheet-styles header geometry (A4 portrait, 5 mm side margins).
_TEMPLATE_CONTENT_WIDTH_PX = 755.9055118110236
_LABEL_PX = 13.333333333333334
_ROW_PX = 14.666666666666667
_RIGHT_LABELS = (
    ("calc_number", "Calc No.:", 37.795275590551178),
    ("date", "Date:", 56.69291338582677),
    ("author", "By:", 75.590551181102356),
    ("checked_by", "Checked:", 94.488188976377941),
    ("approved_by", "Approved:", 113.38582677165354),
)
_LEFT_ROWS = (
    ("project", "Project:", 85.039370078740149),
    ("title", "Title:", 103.93700787401573),
)
PAGE_FIELD_BOX = (633.07086614173227, 18.897637795275589, 38.823333333333338, 15.333333333333334)
"""Template left, top, width, height of the live Sheet "page of total" field."""


def title_block_regions(
    metadata: DocumentMetadata, content_width: float
) -> tuple[TitleBlockCell, ...]:
    """The template-sheet-styles title block, filled from document metadata.

    The right-hand column follows the content's right edge and the caption its
    centre, so page widths other than the template's keep the layout.
    """

    shift = content_width - _TEMPLATE_CONTENT_WIDTH_PX
    cells = [
        TitleBlockCell(
            "sheet-label",
            "Sheet:",
            557.48031496062993 + shift,
            18.897637795275589,
            65.555555555556026,
            15.333333333333334,
            _LABEL_PX,
            alignment="Right",
        ),
        TitleBlockCell(
            "caption",
            "Calculations",
            (content_width - 130.07407407407402) / 2,
            56.69291338582677,
            130.07407407407402,
            21.463333333333335,
            18.666666666666668,
            bold=True,
            alignment="Center",
        ),
    ]
    for key, label, top in _RIGHT_LABELS:
        cells.append(
            TitleBlockCell(
                f"{key}-label",
                label,
                548.03149606299212 + shift,
                top,
                75.3148148148153,
                15.333333333333334,
                _LABEL_PX,
                alignment="Right",
            )
        )
        value = getattr(metadata, key)
        if value:
            size = 12.0 if key == "calc_number" else _LABEL_PX
            cells.append(
                TitleBlockCell(
                    key,
                    value,
                    623.62204724409446 + shift,
                    top,
                    125.555555555556,
                    15.333333333333334,
                    size,
                    bold=True,
                )
            )
    value_width = 451.16666666666691 + shift
    for key, label, top in _LEFT_ROWS:
        cells.append(
            TitleBlockCell(
                f"{key}-label",
                label,
                18.897637795275589,
                top,
                67.851851851852075,
                16.866666666666667,
                _ROW_PX,
            )
        )
        value = getattr(metadata, key)
        if value:
            # Bold Arial averages ~0.49 em per character (the text estimator's figure).
            per_line = max(1, int(value_width / (0.49 * _ROW_PX)))
            lines = math.ceil(len(value) / per_line)
            cells.append(
                TitleBlockCell(
                    key,
                    value,
                    85.039370078740149,
                    top,
                    value_width,
                    16.866666666666667 * lines,
                    _ROW_PX,
                    bold=True,
                )
            )
    return tuple(cells)


def _title_block_xaml(cell: TitleBlockCell) -> str:
    run = FlowRun(
        text=cell.text,
        font_family="Arial",
        font_size=cell.font_size,
        font_weight="Bold" if cell.bold else None,
    )
    return FlowDocumentGenerator().generate_rich_xaml(
        (FlowParagraph((run,), text_alignment=cell.alignment),),
        font_family="Arial",
        font_size=cell.font_size,
    )


def _relationship_id(block_id: str) -> str:
    digest = hashlib.sha256(f"flowdoc:{block_id}".encode()).hexdigest()[:16]
    return f"R{digest}"


def calculation_expression(block: CalculationBlock, result: CalculationResult) -> str:
    """Lower one selected provider output to a Mathcad-compatible expression."""

    if block.output is not None:
        try:
            output = result.outputs[block.output]
        except KeyError:
            raise RenderError(
                f"calculation result {result.entry_id!r} has no output {block.output!r}"
            ) from None
    else:
        if len(result.outputs) != 1:
            count = len(result.outputs)
            raise RenderError(
                f"calculation result {result.entry_id!r} must expose exactly one output "
                f"when block output is omitted; found {count}"
            )
        output = next(iter(result.outputs.values()))
    value = output.value
    if isinstance(value, bool):
        literal = "1" if value else "0"
    elif isinstance(value, str):
        literal = repr(value)
    else:
        literal = repr(value)
    if output.unit is None:
        return literal
    if not output.unit.strip():
        raise RenderError(f"calculation result {result.entry_id!r} has an empty output unit")
    return f"({literal}) * ({output.unit.strip()})"


def _calculation_result(
    block: CalculationBlock,
    calculations: Mapping[str, CalculationResult] | CalculationResult | None,
) -> CalculationResult:
    if calculations is None:
        raise RenderError(
            f"calculation block {block.id!r} ({block.entry_id!r}) requires a resolved "
            "provider result"
        )
    if isinstance(calculations, CalculationResult):
        result = calculations
    else:
        result: CalculationResult | None = None
        for key in (block.id, block.entry_id):
            candidate = calculations.get(key)
            if candidate is not None:
                result = candidate
                break
        if result is None:
            raise RenderError(
                f"no calculation result supplied for block {block.id!r} ({block.entry_id!r})"
            )
    if result.entry_id != block.entry_id:
        raise RenderError(
            f"calculation result entry {result.entry_id!r} does not match block {block.entry_id!r}"
        )
    return result


def _add_image(
    target: WorksheetBuilder,
    block: ImageBlock,
    block_id: str,
    placement: Placement,
    label: str | None,
    styles: Mapping[str, StyleToken],
    result: LayoutResult,
) -> None:
    """A picture centred in its span, with its caption centred beneath it."""

    style = _style_for(block.style or "figure", styles)
    figure = figure_geometry(
        block,
        label,
        style.token,
        span_px=placement.width,
        usable_height_px=result.page.content_height_px - 2.0 * result.page.edge_clearance_px,
    )
    grid = result.page.grid_px
    offset = max(0.0, round((placement.width - figure.width) / 2.0 / grid) * grid)
    target.add_picture_region(
        figure.data,
        top=placement.region_top,
        left=placement.left + offset,
        width=figure.width,
        height=figure.height,
        relationship_id=_relationship_id(f"image:{block_id}"),
    )
    if figure.caption is None:
        return
    token = style.token
    text = "".join(run.text for run in figure.caption.runs if isinstance(run, TextRun))
    xaml = FlowDocumentGenerator().generate_rich_xaml(
        (FlowParagraph((_flow_run(text, style),), text_alignment="Center"),),
        font_family="Arial" if token is None else token.font_family,
        font_size=_font_size_pt(token) * PX_PER_PT,
        text_alignment="Center",
    )
    target.add_rich_text_region(
        xaml,
        # Prime snaps a region's top to the grid on save; store it snapped.
        top=round((placement.top + figure.height + FIGURE_CAPTION_GAP_PX) / grid) * grid,
        left=placement.left,
        width=placement.width,
        height=figure.caption_height - FIGURE_CAPTION_GAP_PX,
        relationship_id=_relationship_id(block_id),
        stub_attributes=_root_attributes(token),
    )


def render_document(
    document: AuthoringDocument,
    calculations: Mapping[str, CalculationResult] | CalculationResult | None = None,
    *,
    layout: LayoutResult | None = None,
    builder: WorksheetBuilder | None = None,
    calculation_results: Mapping[str, CalculationResult] | CalculationResult | None = None,
) -> WorksheetBuilder:
    """Render a validated semantic document into a :class:`WorksheetBuilder`.

    ``calculation_results`` is an optional provider seam.  Without a resolved
    result a calculation block fails loudly instead of becoming an unevaluable
    placeholder.
    """

    resolved_calculations = calculation_results if calculation_results is not None else calculations
    document = lower_blocks(document)
    result = layout or TwoPassLayout(document).layout_document()
    target = builder or WorksheetBuilder()
    target.title = document.document.title
    styles = resolved_styles(document)
    placements_by_id = {placement.block_id: placement for placement in result.placements}
    labels = heading_labels(document.blocks)
    figures = figure_labels(document.blocks)
    pages = {
        index: placements_by_id[block.id or f"block-{index:04d}"].page + 1
        for index, block in enumerate(document.blocks)
        if index in labels
    }
    for index, block in enumerate(document.blocks):
        block_id = block.id or f"block-{index:04d}"
        placement = placements_by_id[block_id]
        if isinstance(block, PageBreakBlock):
            target.add_page_break_region(
                top=placement.region_top,
                left=placement.left,
                width=placement.width,
                height=placement.height,
            )
            continue
        if isinstance(block, ImageBlock):
            _add_image(target, block, block_id, placement, figures.get(index), styles, result)
            continue
        if isinstance(block, ContentsBlock):
            target.add_rich_text_region(
                _contents_xaml(
                    block,
                    contents_entries(document.blocks, block.depth, labels),
                    pages,
                    placement.width,
                    styles,
                ),
                top=placement.region_top,
                left=placement.left,
                width=placement.width,
                height=placement.height,
                relationship_id=_relationship_id(block_id),
            )
            continue
        if isinstance(block, (HeadingBlock, TextBlock)):
            drawn = (
                numbered_heading(block, labels.get(index))
                if isinstance(block, HeadingBlock)
                else block
            )
            xaml, inline_specs = _text_xaml(drawn, styles)
            style = _style_for(block.style or _default_style_id(block), styles).token
            target.add_rich_text_region(
                xaml,
                top=placement.region_top,
                left=placement.left,
                width=placement.width,
                height=placement.height,
                inline_math=inline_specs,
                relationship_id=_relationship_id(block_id),
                stub_attributes=_root_attributes(
                    style,
                    level=block.level if isinstance(block, HeadingBlock) else None,
                ),
            )
            continue
        if isinstance(block, MathBlock):
            target.add_math_region(
                _expression_element(block.expression),
                top=placement.region_top,
                left=placement.left,
                width=placement.width,
                height=placement.height,
            )
            continue
        if isinstance(block, FunctionBlock):
            target.add_math_region(
                render_function_block(block.source),
                top=placement.region_top,
                left=placement.left,
                width=placement.width,
                height=placement.height,
            )
            continue
        if isinstance(block, CalculationBlock):
            calculation = _calculation_result(block, resolved_calculations)
            target.add_math_region(
                _expression_element(calculation_expression(block, calculation)),
                top=placement.region_top,
                left=placement.left,
                width=placement.width,
                height=placement.height,
            )
            continue
        raise RenderError(f"unsupported block type {type(block).__name__}")
    # Header regions follow the body so body region ids stay stable.
    for cell in title_block_regions(document.document, result.page.content_width_px):
        if cell.top + cell.height > result.page.margin_top:
            raise RenderError(
                f"title block {cell.key!r} ends at {cell.top + cell.height:.1f} px, below the "
                f"{result.page.margin_top:.1f} px top margin; shorten it or enlarge the margin"
            )
        target.add_rich_text_region(
            _title_block_xaml(cell),
            top=cell.top,
            left=cell.left,
            width=cell.width,
            height=cell.height,
            relationship_id=_relationship_id(f"header-{cell.key}"),
            stub_attributes=_root_attributes(None),
            header=True,
        )
    left, top, width, height = PAGE_FIELD_BOX
    target.add_header_page_field(
        top=top,
        left=left + result.page.content_width_px - _TEMPLATE_CONTENT_WIDTH_PX,
        width=width,
        height=height,
        font_size=_LABEL_PX,
    )
    return target


def compile_document(
    document: AuthoringDocument,
    calculations: Mapping[str, CalculationResult] | CalculationResult | None = None,
    *,
    layout: LayoutResult | None = None,
    builder: WorksheetBuilder | None = None,
    calculation_results: Mapping[str, CalculationResult] | CalculationResult | None = None,
) -> AuthoringBuild:
    """Compile a semantic document and retain the typed layout result."""

    resolved_layout = layout or TwoPassLayout(document).layout_document()
    target = render_document(
        document,
        calculations,
        layout=resolved_layout,
        builder=builder,
        calculation_results=calculation_results,
    )
    target.presentation_page_model = PresentationPageModel(
        paper_code=resolved_layout.page.paper_code,
        grid_size=resolved_layout.page.grid_size,
        orientation=resolved_layout.page.orientation,
        margin_type=resolved_layout.page.margin_type,
        margins=resolved_layout.page.margins,
    )
    return AuthoringBuild(builder=target, layout=resolved_layout)


__all__ = [
    "AuthoringBuild",
    "RenderError",
    "calculation_expression",
    "compile_document",
    "render_document",
]
