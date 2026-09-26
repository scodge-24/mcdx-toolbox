"""ContentTypeRegistry — manages [Content_Types].xml for OPC packages.

Tracks default content types (by file extension) and override content types
(by specific part name). Serializes to and parses from the XML format
defined in ECMA-376 Part 2.
"""

from lxml import etree

NS = "http://schemas.openxmlformats.org/package/2006/content-types"


class ContentTypeRegistry:
    """Registry of MIME content types for OPC package parts."""

    def __init__(self) -> None:
        self._defaults: dict[str, str] = {}  # extension (lowercase) → content type
        self._overrides: dict[str, str] = {}  # part name → content type

    def add_default(self, extension: str, content_type: str) -> None:
        """Register a default content type for a file extension.

        Extension matching is case-insensitive (stored lowercase internally).
        """
        self._defaults[extension.lower()] = content_type

    def add_override(self, part_name: str, content_type: str) -> None:
        """Register an override content type for a specific part name."""
        self._overrides[part_name] = content_type

    def get_content_type(self, part_name: str) -> str:
        """Look up the content type for a part name.

        Overrides take precedence over defaults. Extension matching is
        case-insensitive. Raises ``KeyError`` if no match found.
        """
        if part_name in self._overrides:
            return self._overrides[part_name]
        ext = part_name.rsplit(".", 1)[-1].lower()
        if ext in self._defaults:
            return self._defaults[ext]
        raise KeyError(f"No content type registered for {part_name!r}")

    def to_xml(self) -> bytes:
        """Serialize to ``[Content_Types].xml`` bytes."""
        # lxml-stubs types nsmap as Mapping[str, str], but lxml itself accepts
        # a `None` key for the default (unprefixed) namespace.
        root = etree.Element(
            f"{{{NS}}}Types",
            nsmap={None: NS},  # pyright: ignore[reportArgumentType]
        )
        for ext in sorted(self._defaults):
            etree.SubElement(
                root,
                f"{{{NS}}}Default",
                Extension=ext,
                ContentType=self._defaults[ext],
            )
        for part_name in sorted(self._overrides):
            etree.SubElement(
                root,
                f"{{{NS}}}Override",
                PartName=part_name,
                ContentType=self._overrides[part_name],
            )
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    @classmethod
    def from_xml(cls, xml_bytes: bytes) -> "ContentTypeRegistry":
        """Parse a ``[Content_Types].xml`` file."""
        reg = cls()
        root = etree.fromstring(xml_bytes)
        for elem in root.findall(f"{{{NS}}}Default"):
            ext = elem.get("Extension", "")
            reg._defaults[ext.lower()] = elem.get("ContentType", "")
        for elem in root.findall(f"{{{NS}}}Override"):
            part_name = elem.get("PartName", "")
            reg._overrides[part_name] = elem.get("ContentType", "")
        return reg
