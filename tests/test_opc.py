"""Unit tests for the pymcdx.opc package: PackURI, ContentTypeRegistry,
Relationship, and the OpcPackageWriter/Reader round trip.
"""

import pytest

from pymcdx.opc import (
    ContentTypeRegistry,
    OpcPackageReader,
    OpcPackageWriter,
    PackURI,
    Relationship,
)


def test_pack_uri_must_start_with_slash():
    with pytest.raises(ValueError, match="must start with"):
        PackURI("mathcad/worksheet.xml")


def test_pack_uri_must_not_end_with_slash():
    with pytest.raises(ValueError, match="must not end with"):
        PackURI("/mathcad/")


def test_pack_uri_membername():
    assert PackURI("/mathcad/worksheet.xml").membername == "mathcad/worksheet.xml"


def test_pack_uri_rels_uri():
    uri = PackURI("/mathcad/worksheet.xml")
    assert uri.rels_uri == "/mathcad/_rels/worksheet.xml.rels"


def test_pack_uri_ext():
    assert PackURI("/mathcad/worksheet.xml").ext == "xml"
    assert PackURI("/docProps/core.xml").ext == "xml"


def test_pack_uri_base_uri():
    assert PackURI("/mathcad/worksheet.xml").base_uri == "/mathcad"


def test_content_type_registry_default_and_override():
    reg = ContentTypeRegistry()
    reg.add_default("xml", "application/xml")
    reg.add_override("/special/part.xml", "application/special+xml")

    assert reg.get_content_type("/anything.xml") == "application/xml"
    assert reg.get_content_type("/special/part.xml") == "application/special+xml"

    with pytest.raises(KeyError):
        reg.get_content_type("/anything.unknown")


def test_content_type_registry_case_insensitive_extension():
    reg = ContentTypeRegistry()
    reg.add_default("xml", "application/xml")
    assert reg.get_content_type("/PART.XML") == "application/xml"


def test_content_type_registry_xml_round_trip():
    reg = ContentTypeRegistry()
    reg.add_default("xml", "application/xml")
    reg.add_override("/docProps/core.xml", "application/core+xml")

    xml_bytes = reg.to_xml()
    reg2 = ContentTypeRegistry.from_xml(xml_bytes)

    assert reg2.get_content_type("/foo.xml") == "application/xml"
    assert reg2.get_content_type("/docProps/core.xml") == "application/core+xml"


def test_relationship_serialize_and_parse_round_trip():
    rels = [
        Relationship(rel_id="R1", rel_type="http://example.com/rel", target="/part1.xml"),
        Relationship(rel_id="R2", rel_type="http://example.com/rel2", target="/part2.xml"),
    ]
    xml_bytes = Relationship.serialize(rels)
    parsed = Relationship.parse(xml_bytes)
    assert parsed == rels


def test_opc_writer_reader_round_trip(tmp_path):
    path = tmp_path / "test.mcdx"
    with OpcPackageWriter(path) as writer:
        writer.add_default_content_type("xml", "application/xml")
        writer.add_part("/mathcad/worksheet.xml", b"<root/>")
        writer.add_relationship(
            "/", "R1", "http://example.com/officeDocument", "/mathcad/worksheet.xml"
        )

    with OpcPackageReader(path) as reader:
        parts = dict(reader.list_parts())
        assert parts["/mathcad/worksheet.xml"] == "application/xml"
        assert reader.read_part("/mathcad/worksheet.xml") == b"<root/>"

        pkg_rels = reader.get_relationships("/")
        assert len(pkg_rels) == 1
        assert pkg_rels[0].target == "/mathcad/worksheet.xml"

        # No relationships registered for this part.
        assert reader.get_relationships("/mathcad/worksheet.xml") == []

        assert reader.get_content_type("/mathcad/worksheet.xml") == "application/xml"


def test_opc_reader_unknown_part_content_type_defaults_octet_stream(tmp_path):
    path = tmp_path / "test.mcdx"
    with OpcPackageWriter(path) as writer:
        writer.add_part("/unregistered.bin", b"data")

    with OpcPackageReader(path) as reader:
        parts = dict(reader.list_parts())
        assert parts["/unregistered.bin"] == "application/octet-stream"


def test_opc_writer_is_byte_deterministic(tmp_path):
    def write(path):
        with OpcPackageWriter(path) as writer:
            writer.add_default_content_type("xml", "application/xml")
            writer.add_part("/mathcad/worksheet.xml", b"<root/>")
            writer.add_relationship(
                "/",
                "R1",
                "http://example.com/officeDocument",
                "/mathcad/worksheet.xml",
            )

    first = tmp_path / "first.mcdx"
    second = tmp_path / "second.mcdx"
    write(first)
    write(second)

    assert first.read_bytes() == second.read_bytes()
