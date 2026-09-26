"""Open Packaging Conventions (OPC) layer for MCDX file I/O.

Implements the ECMA-376 Part 2 packaging model: content types, relationships,
and ZIP-based package read/write.
"""

from pymcdx.opc.content_types import ContentTypeRegistry
from pymcdx.opc.pack_uri import PackURI
from pymcdx.opc.reader import OpcPackageReader
from pymcdx.opc.relationships import Relationship
from pymcdx.opc.writer import OpcPackageWriter

__all__ = [
    "ContentTypeRegistry",
    "OpcPackageReader",
    "OpcPackageWriter",
    "PackURI",
    "Relationship",
]
