"""XSD validation for Mathcad Math50 and Worksheet50 XML."""

from pathlib import Path

from lxml import etree

XSD_DIR = Path(__file__).parent / "xsd"

# Lazy-loaded schema cache
_math50_schema: etree.XMLSchema | None = None
_worksheet50_schema: etree.XMLSchema | None = None


class McdxValidationError(Exception):
    """Raised when generated XML fails XSD validation."""

    def __init__(self, message: str, errors: list[str]):
        self.errors = errors
        super().__init__(message)


def _get_math50_schema() -> etree.XMLSchema:
    """Load and cache the Math50 XSD schema."""
    global _math50_schema
    if _math50_schema is None:
        _math50_schema = etree.XMLSchema(etree.parse(str(XSD_DIR / "math50.xsd")))
    return _math50_schema


def _get_worksheet50_schema() -> etree.XMLSchema:
    """Load and cache the Worksheet50 XSD schema."""
    global _worksheet50_schema
    if _worksheet50_schema is None:
        _worksheet50_schema = etree.XMLSchema(etree.parse(str(XSD_DIR / "worksheet50.xsd")))
    return _worksheet50_schema


def validate_worksheet_xml(doc: etree._Element) -> None:
    """Validate a worksheet XML document against both XSD schemas.

    Validates the full document against worksheet50.xsd, then validates
    each math region element against math50.xsd.

    Raises McdxValidationError on failure.
    """
    ws_schema = _get_worksheet50_schema()
    math_schema = _get_math50_schema()

    # Validate worksheet structure
    if not ws_schema.validate(doc):
        # lxml-stubs declares _ErrorLog as an opaque stub with no __iter__,
        # but lxml's error_log is genuinely iterable at runtime.
        errors = [str(e) for e in ws_schema.error_log]  # pyright: ignore[reportGeneralTypeIssues]
        raise McdxValidationError(f"Worksheet50 validation failed ({len(errors)} errors)", errors)

    # Validate each math region against math50
    ns = {"ml": "http://schemas.mathsoft.com/math50"}
    for tag in ("define", "apply", "program", "eval"):
        for elem in doc.findall(f".//ml:{tag}", ns):
            if not math_schema.validate(elem):
                errors = [
                    str(e)
                    for e in math_schema.error_log  # pyright: ignore[reportGeneralTypeIssues]
                ]
                raise McdxValidationError(f"Math50 validation failed for <ml:{tag}>", errors)


def validate_mcdx_file(mcdx_path: Path) -> None:
    """Validate an .mcdx file by extracting worksheet.xml and running XSD checks."""
    import zipfile

    with zipfile.ZipFile(mcdx_path) as zf:
        ws_xml = zf.read("mathcad/worksheet.xml")

    doc = etree.fromstring(ws_xml)
    validate_worksheet_xml(doc)
