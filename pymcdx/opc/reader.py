"""OpcPackageReader — reads OPC packages from ZIP archives.

Provides access to parts, content types, and relationships
for any OPC-format file (.mcdx, .docx, .xlsx, etc.).
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from pymcdx.opc.content_types import ContentTypeRegistry
from pymcdx.opc.pack_uri import PackURI
from pymcdx.opc.relationships import Relationship


class OpcPackageReader:
    """Read an OPC package from a ZIP file.

    Use as a context manager::

        with OpcPackageReader("input.mcdx") as reader:
            for name, ct in reader.list_parts():
                print(name, ct)
    """

    def __init__(self, source: str | Path | IO[bytes]) -> None:
        self._zipf = zipfile.ZipFile(source, "r")
        self._content_types = self._load_content_types()

    def __enter__(self) -> OpcPackageReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def list_parts(self) -> Iterator[tuple[str, str]]:
        """Yield ``(part_name, content_type)`` for each content part.

        Excludes ``[Content_Types].xml`` and ``_rels/*.rels`` files.
        """
        for member in self._zipf.namelist():
            if member == "[Content_Types].xml":
                continue
            if member.endswith(".rels") and "/_rels/" in member:
                continue
            if member == "_rels/.rels":
                continue
            part_name = "/" + member
            try:
                ct = self._content_types.get_content_type(part_name)
            except KeyError:
                ct = "application/octet-stream"
            yield part_name, ct

    def get_content_type(self, part_name: str) -> str:
        """Look up the content type for a part."""
        return self._content_types.get_content_type(part_name)

    def get_relationships(self, source: str) -> list[Relationship]:
        """Read relationships for a source part.

        Use ``source="/"`` for package-level relationships.
        """
        if source == "/":
            rels_member = "_rels/.rels"
        else:
            uri = PackURI(source)
            rels_member = PackURI(uri.rels_uri).membername

        try:
            rels_xml = self._zipf.read(rels_member)
        except KeyError:
            return []
        return Relationship.parse(rels_xml)

    def read_part(self, part_name: str) -> bytes:
        """Read the raw bytes of a part."""
        uri = PackURI(part_name)
        return self._zipf.read(uri.membername)

    def close(self) -> None:
        """Close the underlying ZIP file."""
        self._zipf.close()

    def _load_content_types(self) -> ContentTypeRegistry:
        """Parse ``[Content_Types].xml`` from the package."""
        ct_xml = self._zipf.read("[Content_Types].xml")
        return ContentTypeRegistry.from_xml(ct_xml)
