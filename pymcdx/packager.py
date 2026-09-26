from pathlib import Path

from lxml import etree

from pymcdx.builder import WorksheetBuilder
from pymcdx.opc import OpcPackageWriter

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "templates"

# Mathcad content types
_MATHCAD_MAIN = "application/vnd.openxmlformats-officedocument.mathprocessingml.mathcad.main+xml"
_RELS_CT = "application/vnd.openxmlformats-package.relationships+xml"

# Mathcad relationship types
_REL_OFFICE_DOC = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_REL_CORE_PROPS = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
)
_REL_FLOW_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/flowDocument"
_REL_RESULT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/result"
_REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_PRESENTATION_NS = "http://schemas.ptc.com/mathcad/settings/presentation10"
_DC_NS = "http://purl.org/dc/elements/1.1/"

# Content type overrides for specific parts.
# Long MIME-type strings are split across adjacent literals (concatenated,
# byte-identical to a single-line value) to stay under E501.
_OVERRIDES = {
    "/docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
    "/docProps/app.xml": "application/mathcad.extended-properties+xml",
    "/mathcad/settings/presentation.xml": (
        "application/vnd.openxmlformats-officedocument.mathprocessingml.mathcad."
        "settings.presentation+xml"
    ),
    "/mathcad/settings/calculation.xml": (
        "application/vnd.openxmlformats-officedocument.mathprocessingml.mathcad."
        "settings.calculation+xml"
    ),
    "/mathcad/result.xml": (
        "application/vnd.openxmlformats-officedocument.mathprocessingml.mathcad.result+xml"
    ),
    "/mathcad/integration.xml": (
        "application/vnd.openxmlformats-officedocument.mathprocessingml.mathcad.integration+xml"
    ),
}


class McdxPackager:
    """Assembles the .mcdx ZIP archive using the OPC packaging layer."""

    def __init__(self, output_path: Path, builder: WorksheetBuilder):
        self.output_path = output_path
        self.builder = builder

    def create_package(self, *, validate: bool = True):
        """Creates the .mcdx file.

        Args:
            validate: If True, validate generated XML against XSD schemas
                before writing to ZIP. Set to False to skip validation.
        """
        if validate:
            self._validate_xml()

        with OpcPackageWriter(self.output_path) as pkg:
            self._register_content_types(pkg)
            self._write_parts(pkg)
            self._write_relationships(pkg)

    def _validate_xml(self):
        """Validate worksheet XML against XSD schemas before packaging."""
        from pymcdx.validation import validate_worksheet_xml

        doc = etree.fromstring(self.builder.to_xml_string().encode("utf-8"))
        validate_worksheet_xml(doc)

    def _register_content_types(self, pkg: OpcPackageWriter):
        # Default types by extension
        pkg.add_default_content_type("xml", _MATHCAD_MAIN)
        pkg.add_default_content_type("rels", _RELS_CT)
        pkg.add_default_content_type("XamlPackage", "application/zip")
        pkg.add_default_content_type("png", "image/png")
        pkg.add_default_content_type(
            "ole", "application/vnd.openxmlformats-officedocument.oleObject"
        )

        # Override types for specific parts
        for part_name, content_type in _OVERRIDES.items():
            pkg.add_override_content_type(part_name, content_type)

    def _write_parts(self, pkg: OpcPackageWriter):
        # Generated content
        pkg.add_part("/mathcad/worksheet.xml", self.builder.to_xml_string().encode("utf-8"))
        pkg.add_part("/mathcad/result.xml", self._generate_result_xml().encode("utf-8"))

        # Static templates
        for template_name, part_name in [
            ("app.xml", "/docProps/app.xml"),
            ("core.xml", "/docProps/core.xml"),
            ("header.xml", "/mathcad/header.xml"),
            ("footer.xml", "/mathcad/footer.xml"),
            ("calculation.xml", "/mathcad/settings/calculation.xml"),
            ("presentation.xml", "/mathcad/settings/presentation.xml"),
            ("integration.xml", "/mathcad/integration.xml"),
        ]:
            if template_name == "presentation.xml":
                content = self._presentation_xml()
            elif template_name == "core.xml":
                content = self._core_xml()
            elif template_name == "header.xml" and self.builder.header_root is not None:
                content = etree.tostring(
                    self.builder.header_root, encoding="utf-8", xml_declaration=True
                )
            else:
                content = self._read_template(template_name)
            pkg.add_part(part_name, content)

        # FlowDocument packages (text regions)
        self._write_flowdocs(pkg)
        for region_id, (rel_id, image) in self.builder.images.items():
            part_name = f"/mathcad/media/Image{region_id}.png"
            pkg.add_part(part_name, image)
            pkg.add_relationship("/mathcad/worksheet.xml", rel_id, _REL_IMAGE, part_name)

    def _core_xml(self) -> bytes:
        """Core properties, with ``dc:title`` when the builder has a title."""

        source = self._read_template("core.xml")
        title = self.builder.title
        if not title:
            return source
        root = etree.fromstring(source)
        element = etree.SubElement(root, f"{{{_DC_NS}}}title", nsmap={"dc": _DC_NS})
        element.text = title
        return etree.tostring(root, encoding="utf-8", xml_declaration=True)

    def _presentation_xml(self) -> bytes:
        """Render semantic page settings into the package presentation part."""

        source = self._read_template("presentation.xml")
        page = self.builder.presentation_page_model
        if page is None:
            return source
        root = etree.fromstring(source)
        page_model = root.find(f"{{{_PRESENTATION_NS}}}pageModel")
        if page_model is None:
            raise ValueError("presentation template has no pageModel")
        page_model.set("paper-code", page.paper_code)
        page_model.set("grid-size", page.grid_size)
        page_model.set("orientation", page.orientation)
        page_model.set("margin-type", page.margin_type)
        page_model.set("page-margin", ",".join(format(value, ".15g") for value in page.margins))
        return etree.tostring(root, encoding="utf-8", xml_declaration=True)

    def _write_relationships(self, pkg: OpcPackageWriter):
        # Package-level relationships
        pkg.add_relationship("/", "Rworksheet", _REL_OFFICE_DOC, "/mathcad/worksheet.xml")
        pkg.add_relationship("/", "Rheader", _REL_OFFICE_DOC, "/mathcad/header.xml")
        pkg.add_relationship("/", "Rfooter", _REL_OFFICE_DOC, "/mathcad/footer.xml")
        pkg.add_relationship("/", "Rcore", _REL_CORE_PROPS, "/docProps/core.xml")

        # Worksheet-level: result.xml
        pkg.add_relationship(
            "/mathcad/worksheet.xml", "Rresult", _REL_RESULT, "/mathcad/result.xml"
        )

    def _write_flowdocs(self, pkg: OpcPackageWriter):
        from pymcdx.flowdoc import FlowDocumentGenerator, FlowDocumentPackager

        doc_gen = FlowDocumentGenerator()
        doc_packager = FlowDocumentPackager()

        for region_id, text_content in self.builder.flowdocs.items():
            rel_id = self.builder.flowdoc_rels[region_id]

            # Semantic authoring may provide already-rendered rich XAML.  The
            # legacy Python path still stores plain text and a bold flag.
            xaml_str = self.builder.flowdoc_xaml.get(region_id)
            if xaml_str is None:
                bold = self.builder.flowdoc_bold.get(region_id, False)
                xaml_str = doc_gen.generate_xaml(text_content, bold=bold)
            pkg_bytes = doc_packager.create_package(xaml_str.encode("utf-8"))

            # Write the XamlPackage part
            part_name = f"/mathcad/xaml/FlowDocument{region_id}.XamlPackage"
            pkg.add_part(part_name, pkg_bytes)

            # Worksheet (or page header) → FlowDocument relationship
            source = (
                "/mathcad/header.xml"
                if region_id in self.builder.header_region_ids
                else "/mathcad/worksheet.xml"
            )
            pkg.add_relationship(source, rel_id, _REL_FLOW_DOC, part_name)

    def _read_template(self, template_name: str) -> bytes:
        template_path = TEMPLATE_DIR / template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Template {template_name} not found at {template_path}")
        return template_path.read_bytes()

    def _generate_result_xml(self) -> str:
        """Generates result.xml mapping resultRefs to empty data."""
        result_ns = "http://schemas.mathsoft.com/result10"
        root = etree.Element(
            f"{{{result_ns}}}resultsList",
            # lxml-stubs types nsmap as Mapping[str, str], but lxml itself
            # accepts a `None` key for the default (unprefixed) namespace.
            nsmap={  # pyright: ignore[reportArgumentType]
                None: result_ns,
                "ml": "http://schemas.mathsoft.com/math50",
                "u": "http://schemas.mathsoft.com/units10",
            },
        )

        for ref_id in self.builder.results:
            etree.SubElement(root, "resultData", attrib={"result-id": str(ref_id)})

        return etree.tostring(
            root, pretty_print=True, xml_declaration=True, encoding="utf-8"
        ).decode("utf-8")
