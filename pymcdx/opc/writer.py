"""OpcPackageWriter — assembles OPC packages as ZIP archives.

Handles content types, relationships, and part storage. The
``[Content_Types].xml`` and ``_rels/*.rels`` files are written
automatically on close.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import IO

from pymcdx.opc.content_types import ContentTypeRegistry
from pymcdx.opc.pack_uri import PackURI
from pymcdx.opc.relationships import Relationship

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644 << 16


class OpcPackageWriter:
    """Write an OPC package to a ZIP file.

    Use as a context manager::

        with OpcPackageWriter("output.mcdx") as pkg:
            pkg.add_default_content_type("xml", "application/xml")
            pkg.add_part("/mathcad/worksheet.xml", xml_bytes)
            pkg.add_relationship("/", "R1", rel_type, "/mathcad/worksheet.xml")
    """

    def __init__(self, target: str | Path | IO[bytes]) -> None:
        self._zipf = zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED)
        self._content_types = ContentTypeRegistry()
        self._rels: dict[str, list[Relationship]] = {}  # source → [rels]

    def __enter__(self) -> OpcPackageWriter:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def add_default_content_type(self, extension: str, content_type: str) -> None:
        """Register a default content type for a file extension."""
        self._content_types.add_default(extension, content_type)

    def add_override_content_type(self, part_name: str, content_type: str) -> None:
        """Register an override content type for a specific part."""
        self._content_types.add_override(part_name, content_type)

    def add_part(self, part_name: str, content: bytes) -> None:
        """Write a part to the package."""
        uri = PackURI(part_name)
        self._write_member(uri.membername, content)

    def add_relationship(
        self,
        source: str,
        rel_id: str,
        rel_type: str,
        target: str,
    ) -> None:
        """Add a relationship from *source* to *target*.

        Use ``source="/"`` for package-level relationships.
        """
        rel = Relationship(rel_id=rel_id, rel_type=rel_type, target=target)
        self._rels.setdefault(source, []).append(rel)

    def close(self) -> None:
        """Write content types and relationships, then close the ZIP."""
        # Write [Content_Types].xml
        self._write_member("[Content_Types].xml", self._content_types.to_xml())

        # Write relationship files
        for source in sorted(self._rels):
            rels = self._rels[source]
            xml_bytes = Relationship.serialize(rels)
            if source == "/":
                rels_path = "_rels/.rels"
            else:
                uri = PackURI(source)
                rels_path = PackURI(uri.rels_uri).membername
            self._write_member(rels_path, xml_bytes)

        self._zipf.close()

    def _write_member(self, member_name: str, content: bytes) -> None:
        """Write one member with reproducible metadata.

        ``ZipFile.writestr(name, ...)`` stamps the current local time into every
        member.  Mathcad does not depend on those timestamps, so canonicalising
        them makes otherwise-identical worksheet builds byte-for-byte stable.
        """
        info = zipfile.ZipInfo(member_name, date_time=_ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = _ZIP_FILE_MODE
        self._zipf.writestr(info, content)
