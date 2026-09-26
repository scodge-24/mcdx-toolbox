"""Relationship — OPC relationship model and XML serialization.

Represents the ``<Relationship>`` elements found in ``_rels/*.rels`` files
as defined in ECMA-376 Part 2.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

NS = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class Relationship:
    """A single OPC relationship."""

    rel_id: str
    rel_type: str
    target: str

    @staticmethod
    def serialize(rels: list[Relationship]) -> bytes:
        """Serialize a list of relationships to XML bytes."""
        # lxml-stubs types nsmap as Mapping[str, str], but lxml itself accepts
        # a `None` key for the default (unprefixed) namespace.
        root = etree.Element(
            f"{{{NS}}}Relationships",
            nsmap={None: NS},  # pyright: ignore[reportArgumentType]
        )
        for rel in rels:
            etree.SubElement(
                root,
                f"{{{NS}}}Relationship",
                Id=rel.rel_id,
                Type=rel.rel_type,
                Target=rel.target,
            )
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    @staticmethod
    def parse(xml_bytes: bytes) -> list[Relationship]:
        """Parse relationships from XML bytes."""
        root = etree.fromstring(xml_bytes)
        rels: list[Relationship] = []
        for elem in root.findall(f"{{{NS}}}Relationship"):
            rels.append(
                Relationship(
                    rel_id=elem.get("Id", ""),
                    rel_type=elem.get("Type", ""),
                    target=elem.get("Target", ""),
                )
            )
        return rels
