import secrets
from collections.abc import Sequence
from dataclasses import dataclass

from lxml import etree

# Namespaces
WS_NS = "http://schemas.mathsoft.com/worksheet50"
ML_NS = "http://schemas.mathsoft.com/math50"
XML_NS = "http://www.w3.org/XML/1998/namespace"
WPF_NS = "http://schemas.microsoft.com/winfx/2006/xaml/presentation"

# Orange highlight used for WRITEPRN/APPENDPRN regions
_PRN_HIGHLIGHT = "#FFFBBF96"
NSMAP = {
    None: WS_NS,
    "ws": WS_NS,
    "ml": ML_NS,
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "ve": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "u": "http://schemas.mathsoft.com/units10",
    "p": "http://schemas.mathsoft.com/provenance10",
}

# A4 page dimensions in points (1 pt = 1/72 inch)
_A4_HEIGHT = 841.89  # 297 mm
_PAGE_MARGIN_TOP = 28.35  # ~10 mm top margin on each page


@dataclass(frozen=True, slots=True)
class InlineMathSpec:
    """A renderer-owned inline Math50 region inside a rich text block."""

    expression: etree._Element
    caret_position: int
    width: float
    height: float
    result_format: etree._Element | None = None


@dataclass(frozen=True, slots=True)
class PresentationPageModel:
    """Page settings that must agree with semantic region coordinates."""

    paper_code: str
    grid_size: str
    orientation: str
    margin_type: str
    margins: tuple[float, float, float, float]


class LayoutManager:
    """
    Manages vertical layout of regions on the page.
    """

    def __init__(
        self,
        start_top: float = 28.35,
        margin_left: float = 18.9,
        spacing: float = 22.24,
        page_height: float = _A4_HEIGHT,
    ):
        # Default start_top approx 1cm (28.35 pts ≈ 10mm)
        # Default spacing so that height(25) + spacing(22.24) ≈ 47.24 pts (≈ 16.67mm)
        self.current_top = start_top
        self.margin_left = margin_left
        self.spacing = spacing
        self.page_height = page_height

    def get_next_position(self, height: float = 25.0) -> float:
        """Returns the top position for the next region and advances the cursor."""
        pos = self.current_top
        self.current_top += height + self.spacing
        return pos

    def remaining_on_page(self) -> float:
        """How many points of vertical space remain on the current page."""
        page_bottom = self._current_page_bottom()
        return max(0.0, page_bottom - self.current_top)

    def advance_to_next_page(self) -> None:
        """Move cursor to the start of the next page."""
        page_idx = self._current_page_index() + 1
        self.current_top = page_idx * self.page_height + _PAGE_MARGIN_TOP

    def _current_page_index(self) -> int:
        """Which page (0-based) the cursor is currently on."""
        return int(self.current_top / self.page_height)

    def _current_page_bottom(self) -> float:
        """Bottom usable position of the current page (with bottom margin)."""
        return (self._current_page_index() + 1) * self.page_height - _PAGE_MARGIN_TOP


class WorksheetBuilder:
    """
    Orchestrates the creation of worksheet.xml.
    """

    def __init__(self):
        self.root = etree.Element(f"{{{WS_NS}}}worksheet", nsmap=NSMAP)
        self.regions_container = etree.SubElement(self.root, f"{{{WS_NS}}}regions")
        self.layout = LayoutManager()
        self.region_count = 0
        self.result_refs = 0

        # Mapping of resultRef ID to result data (initially empty)
        self.results: dict[int, str] = {}

        # Mapping of region-id to flowdoc content (for text regions)
        self.flowdocs: dict[str, str] = {}  # region_id -> text_content
        self.flowdoc_rels: dict[str, str] = {}  # region_id -> rel_id
        self.flowdoc_bold: dict[str, bool] = {}  # region_id -> bold flag
        # Semantic authoring stores canonical XAML here. Legacy callers keep
        # using flowdocs/flowdoc_bold and are rendered exactly as before.
        self.flowdoc_xaml: dict[str, str] = {}
        # Picture regions: region id -> (relationship id, image bytes).
        self.images: dict[str, tuple[str, bytes]] = {}
        # Legacy builders use the static presentation template. Semantic
        # authoring supplies the exact page model used by its layout pass.
        self.presentation_page_model: PresentationPageModel | None = None
        # Page-header regions (header.xml); None keeps the static template.
        self.header_root: etree._Element | None = None
        self.header_region_ids: set[str] = set()
        # Package title (docProps/core.xml dc:title); None writes no title.
        self.title: str | None = None

    def snapshot(self) -> dict:
        """Capture builder state for rollback on error."""
        return {
            "region_count": self.region_count,
            "result_refs": self.result_refs,
            "num_regions": len(self.regions_container),
            "current_top": self.layout.current_top,
            "results_keys": set(self.results.keys()),
            "flowdoc_keys": set(self.flowdocs.keys()),
            "flowdoc_xaml_keys": set(self.flowdoc_xaml.keys()),
        }

    def rollback(self, snap: dict) -> None:
        """Restore builder state from a snapshot, removing any regions added since."""
        # Remove XML region elements added after snapshot
        while len(self.regions_container) > snap["num_regions"]:
            self.regions_container.remove(self.regions_container[-1])
        self.region_count = snap["region_count"]
        self.result_refs = snap["result_refs"]
        self.layout.current_top = snap["current_top"]
        # Clean up dicts
        for key in set(self.results.keys()) - snap["results_keys"]:
            del self.results[key]
        for key in set(self.flowdocs.keys()) - snap["flowdoc_keys"]:
            del self.flowdocs[key]
            self.flowdoc_rels.pop(key, None)
            self.flowdoc_bold.pop(key, None)
        for key in set(self.flowdoc_xaml.keys()) - snap["flowdoc_xaml_keys"]:
            del self.flowdoc_xaml[key]

    def add_math_region(
        self,
        math_element: etree._Element,
        *,
        top: float | None = None,
        left: float | None = None,
        width: float | None = None,
        height: float = 25.0,
    ):
        """
        Wraps a math50 element in a worksheet region.

        When top/left are provided, the region is placed at that position without
        advancing the layout cursor — useful for side-by-side input layouts.
        The height parameter controls both the actualHeight attribute and how far
        the layout cursor advances (for auto-positioned regions).
        """
        region_id = str(self.region_count)
        self.region_count += 1

        if top is None:
            top = self.layout.get_next_position(height=height)
        if left is None:
            left = self.layout.margin_left
        attrib = {
            "region-id": region_id,
            "top": str(top),
            "left": str(left),
            "actualHeight": str(height),
        }
        # Math regions omit width (no text wrapping); only set actualWidth
        if width is not None:
            attrib["width"] = str(width)
            attrib["actualWidth"] = str(width)

        region = etree.SubElement(
            self.regions_container,
            f"{{{WS_NS}}}region",
            attrib=attrib,
        )

        # Math container
        result_ref = self.result_refs
        self.result_refs += 1
        self.results[result_ref] = ""

        math_container = etree.SubElement(
            region, f"{{{WS_NS}}}math", attrib={"resultRef": str(result_ref)}
        )

        math_container.append(math_element)

        return region

    def add_text_region(
        self,
        text_content: str,
        *,
        top: float | None = None,
        left: float | None = None,
        width: float | None = None,
        height: float = 25.0,
        bold: bool = False,
    ):
        """
        Adds a text region.

        When top/left are provided, the region is placed at that position without
        advancing the layout cursor — useful for annotations beside math regions.
        The height parameter controls cursor advance (Mathcad recalculates on open).
        """
        region_id = str(self.region_count)
        self.region_count += 1

        if top is None:
            top = self.layout.get_next_position(height=height)
        if left is None:
            left = self.layout.margin_left
        if width is None:
            width = 552.0

        region = etree.SubElement(
            self.regions_container,
            f"{{{WS_NS}}}region",
            attrib={
                "region-id": region_id,
                "top": str(top),
                "left": str(left),
                "width": str(width),
                "actualWidth": str(width),
                "actualHeight": str(height),
            },
        )

        # Generate a unique relationship ID for the FlowDocument
        rel_id = f"R{secrets.token_hex(8)}"
        self.flowdocs[region_id] = text_content
        self.flowdoc_rels[region_id] = rel_id
        self.flowdoc_bold[region_id] = bold

        etree.SubElement(region, f"{{{WS_NS}}}text", attrib={"item-idref": rel_id})

        return region

    def add_header_page_field(
        self,
        *,
        top: float,
        left: float,
        width: float,
        height: float,
        font_size: float,
    ) -> etree._Element:
        """Add the page header's live "page of total" field (Prime's fieldText)."""

        if self.header_root is None:
            self.header_root = etree.Element(f"{{{WS_NS}}}header", nsmap=NSMAP)
            etree.SubElement(self.header_root, f"{{{WS_NS}}}regions")
        region_id = str(self.region_count)
        self.region_count += 1
        region = etree.SubElement(
            self.header_root[0],
            f"{{{WS_NS}}}region",
            attrib={
                "region-id": region_id,
                "top": str(top),
                "left": str(left),
                "actualWidth": str(width),
                "actualHeight": str(height),
            },
        )
        field_text = etree.SubElement(region, f"{{{WS_NS}}}fieldText")
        document = etree.SubElement(
            etree.SubElement(field_text, f"{{{WS_NS}}}text"),
            f"{{{WPF_NS}}}FlowDocument",
            attrib={
                "FontFamily": "Arial",
                "FontStyle": "Normal",
                "FontWeight": "Bold",
                "FontSize": str(font_size),
                "Foreground": "#FF000000",
                "Background": "#00FFFFFF",
                "TextAlignment": "Left",
                "Typography.Variants": "Normal",
            },
            nsmap={None: WPF_NS},  # type: ignore[arg-type]  # lxml accepts None for default
        )
        etree.SubElement(document, f"{{{WPF_NS}}}Paragraph").text = "1 of 1"
        etree.SubElement(
            field_text, f"{{{WS_NS}}}pageNumber", attrib={"template": "@PageNo_of_@PagesTotal"}
        )
        return region

    def add_rich_text_region(
        self,
        xaml: str,
        *,
        top: float,
        left: float,
        width: float,
        height: float,
        inline_math: Sequence[InlineMathSpec] = (),
        relationship_id: str | None = None,
        stub_attributes: dict[str, str] | None = None,
        header: bool = False,
    ) -> etree._Element:
        """Add a rich FlowDocument-backed text region.

        ``header`` places the region in the page header (header.xml), which
        Prime repeats on every page, instead of the worksheet body.

        ``inline_math`` is represented in worksheet XML as caret-only nested
        regions. Their Math50 elements stay separate from the external
        FlowDocument, matching Prime's text/math join. ``relationship_id`` is
        optional to preserve the legacy random-ID behaviour; semantic builds
        pass a deterministic value.
        """

        if width <= 0 or height <= 0:
            raise ValueError(f"rich text region dimensions must be positive, got {width}×{height}")
        region_id = str(self.region_count)
        self.region_count += 1
        rel_id = relationship_id or f"R{secrets.token_hex(8)}"
        self.flowdocs[region_id] = ""
        self.flowdoc_rels[region_id] = rel_id
        self.flowdoc_bold[region_id] = False
        self.flowdoc_xaml[region_id] = xaml

        container = self.regions_container
        if header:
            if self.header_root is None:
                self.header_root = etree.Element(f"{{{WS_NS}}}header", nsmap=NSMAP)
                etree.SubElement(self.header_root, f"{{{WS_NS}}}regions")
            container = self.header_root[0]
            self.header_region_ids.add(region_id)
        region = etree.SubElement(
            container,
            f"{{{WS_NS}}}region",
            attrib={
                "region-id": region_id,
                "top": str(top),
                "left": str(left),
                "width": str(width),
                "actualWidth": str(width),
                "actualHeight": str(height),
            },
        )
        text = etree.SubElement(
            region,
            f"{{{WS_NS}}}text",
            attrib={"item-idref": rel_id},
        )
        if stub_attributes is not None:
            # The surrounding worksheet already owns a different default
            # namespace. Prime feeds this stub to its WPF XAML reader as inner
            # XML, so WPF must be redeclared as the default here: a generated
            # ``ns0`` prefix leaves attached names such as
            # ``Typography.Variants`` resolving against worksheet50.
            etree.SubElement(
                text,
                f"{{{WPF_NS}}}FlowDocument",
                attrib=stub_attributes,
                nsmap={None: WPF_NS},  # type: ignore[arg-type]  # lxml accepts None for default
            )
        if inline_math:
            nested = etree.SubElement(text, f"{{{WS_NS}}}regions")
            for spec in inline_math:
                if spec.caret_position < 0:
                    raise ValueError(
                        f"inline math caret must be non-negative, got {spec.caret_position}"
                    )
                child = etree.SubElement(
                    nested,
                    f"{{{WS_NS}}}region",
                    attrib={
                        "region-id": str(self.region_count),
                        "actualWidth": str(spec.width),
                        "actualHeight": str(spec.height),
                        "text-caret-position": str(spec.caret_position),
                    },
                )
                self.region_count += 1
                result_ref = self.result_refs
                self.result_refs += 1
                self.results[result_ref] = ""
                math = etree.SubElement(
                    child,
                    f"{{{WS_NS}}}math",
                    attrib={"resultRef": str(result_ref)},
                )
                math.append(spec.expression)
                if spec.result_format is not None:
                    math.append(spec.result_format)
        return region

    def add_picture_region(
        self,
        image: bytes,
        *,
        top: float,
        left: float,
        width: float,
        height: float,
        relationship_id: str,
    ) -> etree._Element:
        """Add a picture region displaying ``image`` at ``width`` x ``height`` px."""

        if width <= 0 or height <= 0:
            raise ValueError(f"picture dimensions must be positive, got {width}x{height}")
        region_id = str(self.region_count)
        self.region_count += 1
        self.images[region_id] = (relationship_id, image)
        region = etree.SubElement(
            self.regions_container,
            f"{{{WS_NS}}}region",
            attrib={
                "region-id": region_id,
                "height": str(height),
                "width": str(width),
                "actualWidth": str(width),
                "actualHeight": str(height),
                "top": str(top),
                "left": str(left),
            },
        )
        etree.SubElement(
            etree.SubElement(region, f"{{{WS_NS}}}picture"),
            f"{{{WS_NS}}}png",
            attrib={
                "item-idref": relationship_id,
                "display-width": str(width),
                "display-height": str(height),
            },
        )
        return region

    def add_page_break_region(
        self,
        *,
        top: float,
        width: float,
        left: float = 0.0,
        height: float = 2.0,
    ) -> etree._Element:
        """Add an explicit page-break marker at a continuous document top."""

        if width <= 0 or height <= 0:
            raise ValueError(f"page-break dimensions must be positive, got {width}×{height}")
        region_id = str(self.region_count)
        self.region_count += 1
        region = etree.SubElement(
            self.regions_container,
            f"{{{WS_NS}}}region",
            attrib={
                "region-id": region_id,
                "top": str(top),
                "left": str(left),
                "actualWidth": str(width),
                "actualHeight": str(height),
                "height": str(height),
            },
        )
        etree.SubElement(region, f"{{{WS_NS}}}pageBreak")
        return region

    # -- WRITEPRN / APPENDPRN helpers ------------------------------------------

    def _ml(self, tag: str, text: str | None = None, **attribs) -> etree._Element:
        """Shorthand: create an element in the Math50 namespace."""
        elem = etree.SubElement(
            etree.Element("_dummy"),  # detached parent, we'll reparent
            f"{{{ML_NS}}}{tag}",
        )
        # Detach from dummy
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)
        elem = etree.Element(f"{{{ML_NS}}}{tag}")
        if text is not None:
            elem.text = text
        for k, v in attribs.items():
            if k == "xml_space":
                elem.set(f"{{{XML_NS}}}space", v)
            else:
                elem.set(k, v)
        return elem

    def _ml_id(
        self, name: str, *, labels: str | None = None, contextual: bool = False
    ) -> etree._Element:
        """Create an ml:id element."""
        attribs: dict = {}
        if labels:
            attribs["labels"] = labels
        if contextual:
            attribs["label-is-contextual"] = "true"
        attribs["xml_space"] = "preserve"
        return self._ml("id", text=name, **attribs)

    def _ml_str(self, value: str) -> etree._Element:
        """Create an ml:str element."""
        return self._ml("str", text=value, xml_space="preserve")

    def _add_prn_region(
        self,
        math_content: etree._Element,
        *,
        disable_calc: bool = True,
        result_format: bool = False,
        height: float = 25.0,
    ) -> etree._Element:
        """Add a math region with PRN-specific attributes (disable-calc, formatting)."""
        region_id = str(self.region_count)
        self.region_count += 1

        top = self.layout.get_next_position(height=height)
        left = self.layout.margin_left

        region = etree.SubElement(
            self.regions_container,
            f"{{{WS_NS}}}region",
            attrib={
                "region-id": region_id,
                "top": str(top),
                "left": str(left),
                "actualHeight": str(height),
            },
        )

        result_ref = self.result_refs
        self.result_refs += 1
        self.results[result_ref] = ""

        math_attrib = {"resultRef": str(result_ref)}
        if disable_calc:
            math_attrib["disable-calc"] = "true"

        math_container = etree.SubElement(region, f"{{{WS_NS}}}math", attrib=math_attrib)
        math_container.append(math_content)

        if result_format:
            rf = etree.SubElement(math_container, f"{{{WS_NS}}}resultFormat")
            etree.SubElement(
                rf,
                f"{{{WS_NS}}}matrix",
                attrib={
                    "size": "12,12",
                    "offset": "0,0",
                    "show-indices": "false",
                    "expand-nested-arrays": "false",
                },
            )

        etree.SubElement(
            math_container,
            f"{{{WS_NS}}}formattingOverride",
            attrib={
                "BackgroundColor": _PRN_HIGHLIGHT,
            },
        )

        return region

    def add_writeprn_region(
        self,
        filename: str,
        variable: str,
        *,
        disable_calc: bool = True,
    ) -> etree._Element:
        """Add a WRITEPRN(filename, variable) region.

        Produces:
            WRITEPRN("filename", variable) =
        with disable-calc and orange highlight.
        """
        # ml:eval > ml:apply > WRITEPRN(str, id) + unitOverride
        eval_elem = self._ml("eval")

        apply_elem = self._ml("apply")
        apply_elem.append(self._ml_id("WRITEPRN", labels="FUNCTION"))
        seq = self._ml("sequence")
        seq.append(self._ml_str(filename))
        seq.append(self._ml_id(variable, labels="VARIABLE", contextual=True))
        apply_elem.append(seq)
        eval_elem.append(apply_elem)

        unit_override = self._ml("unitOverride")
        unit_override.append(self._ml("placeholder"))
        eval_elem.append(unit_override)

        return self._add_prn_region(eval_elem, disable_calc=disable_calc)

    def add_appendprn_region(
        self,
        filename: str,
        variables: list[str],
        *,
        disable_calc: bool = True,
    ) -> etree._Element:
        """Add an APPENDPRN(filename, [var1, var2, ...]) region.

        Produces:
            APPENDPRN("filename", [var1; var2; ...])
        with disable-calc, resultFormat, and orange highlight.

        Args:
            filename: Output filename string.
            variables: List of variable names to include in the column matrix.
        """
        # ml:apply > APPENDPRN(str, matrix)
        apply_elem = self._ml("apply")
        apply_elem.append(self._ml_id("APPENDPRN", labels="FUNCTION"))

        seq = self._ml("sequence")
        seq.append(self._ml_str(filename))

        matrix = self._ml("matrix", rows=str(len(variables)), cols="1")
        for var in variables:
            matrix.append(self._ml_id(var))
        seq.append(matrix)

        apply_elem.append(seq)

        height = max(25.0, 20.0 * len(variables))
        return self._add_prn_region(
            apply_elem, disable_calc=disable_calc, result_format=True, height=height
        )

    def to_xml_string(self) -> str:
        return etree.tostring(
            self.root, pretty_print=True, xml_declaration=True, encoding="utf-8"
        ).decode("utf-8")
