"""PackURI — a str subclass representing an OPC part name.

Provides computed properties for ZIP member names, relationship file paths,
and file extensions, eliminating string-manipulation bugs.

Follows the Open Packaging Conventions (ECMA-376 Part 2) naming rules.
"""

import posixpath


class PackURI(str):
    """An OPC pack URI (part name) with computed path properties.

    Always starts with ``/`` and uses forward slashes. The root package
    itself is represented by ``/`` for relationship lookups, but ``/`` is
    not a valid part name.
    """

    def __new__(cls, uri: str) -> "PackURI":
        if not uri.startswith("/"):
            raise ValueError(f"PackURI must start with '/': {uri!r}")
        if len(uri) > 1 and uri.endswith("/"):
            raise ValueError(f"PackURI must not end with '/': {uri!r}")
        return str.__new__(cls, uri)

    @property
    def membername(self) -> str:
        """ZIP archive member name (strips leading ``/``)."""
        return self[1:]

    @property
    def rels_uri(self) -> str:
        """Path to this part's relationships file.

        ``/mathcad/worksheet.xml`` → ``/mathcad/_rels/worksheet.xml.rels``
        """
        base = posixpath.split(self)[0]
        filename = posixpath.basename(self)
        return f"{base}/_rels/{filename}.rels"

    @property
    def ext(self) -> str:
        """File extension without the dot."""
        return posixpath.splitext(self)[1][1:]

    @property
    def base_uri(self) -> str:
        """Parent directory of this part."""
        return posixpath.split(self)[0]
