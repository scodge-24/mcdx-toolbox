import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from lxml import etree

WPF_NS = "http://schemas.microsoft.com/winfx/2006/xaml/presentation"
XML_NS = "http://www.w3.org/XML/1998/namespace"
QN = etree.QName


@dataclass(frozen=True, slots=True)
class FlowRun:
    """One styled WPF Run in a semantic paragraph."""

    text: str
    style: str | None = None
    font_family: str | None = None
    font_size: float | None = None
    font_weight: str | None = None
    font_style: str | None = None
    foreground: str | None = None
    background: str | None = None
    text_decorations: str | None = None


@dataclass(frozen=True, slots=True)
class FlowParagraph:
    """A paragraph and its runs for :meth:`FlowDocumentGenerator.generate_rich_xaml`."""

    runs: tuple[FlowRun, ...]
    text_alignment: str | None = None
    text_indent: float | None = None
    number_substitution: str = "User"


class _AttributeTarget(Protocol):
    def __setitem__(self, key: str, value: str, /) -> None:
        """Set one XML attribute."""


def _put_if(attributes: _AttributeTarget, name: str, value: object | None) -> None:
    if value is not None:
        attributes[name] = str(value)


class FlowDocumentGenerator:
    def create_section(self, font_family="Arial", font_size=13.3333333333333):
        # lxml-stubs types nsmap as Mapping[str, str], but lxml itself accepts
        # a `None` key for the default (unprefixed) namespace.
        section = etree.Element(
            QN(WPF_NS, "Section"),
            nsmap={None: WPF_NS},  # pyright: ignore[reportArgumentType]
        )
        section.set(QN(XML_NS, "space"), "preserve")
        section.set(QN(XML_NS, "lang"), "en-gb")
        section.set("TextAlignment", "Left")
        section.set("FontFamily", font_family)
        section.set("FontSize", str(font_size))
        return section

    def create_paragraph(self, text, *, bold=False):
        para = etree.Element(QN(WPF_NS, "Paragraph"))
        attrib = {}
        if bold:
            attrib["FontWeight"] = "Bold"
        run = etree.SubElement(para, QN(WPF_NS, "Run"), attrib=attrib)
        run.text = text
        return para

    def generate_xaml(self, text, *, bold=False):
        """
        Generates the XAML content for a text region.
        Returns a string containing the XML.

        Note: WPF's TextRangeBase.Load requires the XamlPackage root
        element to be Section or Span — not FlowDocument.
        """
        section = self.create_section()
        for line in text.split("\n"):
            para = self.create_paragraph(line, bold=bold)
            section.append(para)
        return etree.tostring(section, encoding="utf-8").decode("utf-8")

    def generate_rich_xaml(
        self,
        paragraphs: Sequence[FlowParagraph],
        *,
        font_family: str = "Arial",
        font_size: float = 13.3333333333333,
        font_weight: str = "Normal",
        font_style: str = "Normal",
        foreground: str = "#FF000000",
        background: str = "#00FFFFFF",
        text_alignment: str = "Left",
        language: str = "en-gb",
        typography_variants: str = "Normal",
        number_substitution: str = "Text",
    ) -> str:
        """Lower semantic paragraphs/runs to a Prime-compatible Section XAML."""

        section = self.create_section(font_family=font_family, font_size=font_size)
        section.set(QN(XML_NS, "lang"), language)
        section.set("TextAlignment", text_alignment)
        section.set("LineHeight", "Auto")
        section.set("IsHyphenationEnabled", "False")
        section.set("FlowDirection", "LeftToRight")
        section.set("NumberSubstitution.CultureSource", number_substitution)
        section.set("NumberSubstitution.Substitution", "AsCulture")
        section.set("FontWeight", font_weight)
        section.set("FontStyle", font_style)
        section.set("FontStretch", "Normal")
        section.set("Foreground", foreground)
        section.set("Background", background)
        section.set("Typography.Variants", typography_variants)
        for paragraph in paragraphs:
            para = etree.SubElement(
                section,
                QN(WPF_NS, "Paragraph"),
                attrib={"NumberSubstitution.CultureSource": paragraph.number_substitution},
            )
            _put_if(para.attrib, "TextAlignment", paragraph.text_alignment)
            _put_if(para.attrib, "TextIndent", paragraph.text_indent)
            for run in paragraph.runs:
                attrs: dict[str, str] = {}
                _put_if(attrs, "style", run.style)
                _put_if(attrs, "FontFamily", run.font_family)
                _put_if(attrs, "FontSize", run.font_size)
                _put_if(attrs, "FontWeight", run.font_weight)
                _put_if(attrs, "FontStyle", run.font_style)
                _put_if(attrs, "Foreground", run.foreground)
                _put_if(attrs, "Background", run.background)
                _put_if(attrs, "TextDecorations", run.text_decorations)
                node = etree.SubElement(para, QN(WPF_NS, "Run"), attrib=attrs)
                node.text = run.text
        return etree.tostring(section, encoding="utf-8").decode("utf-8")


class FlowDocumentPackager:
    def create_package(self, xaml_bytes):
        # Create a ZIP package in memory
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # [Content_Types].xml
            self._writestr(zf, "[Content_Types].xml", self._get_content_types())
            # _rels/.rels
            self._writestr(zf, "_rels/.rels", self._get_rels())
            # Xaml/Document.xaml
            self._writestr(zf, "Xaml/Document.xaml", xaml_bytes)
        return buf.getvalue()

    @staticmethod
    def _writestr(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
        """Write a ZIP member with stable metadata for byte-identical builds."""

        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 0
        info.external_attr = 0
        zf.writestr(info, data)

    def _get_content_types(self):
        return b"""<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xaml" ContentType="application/vnd.ms-wpf.xaml+xml"/>
</Types>"""

    def _get_rels(self):
        # Split across adjacent literals (concatenated, byte-identical to the
        # single-line original) so no physical source line exceeds E501.
        return (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            b'  <Relationship Type="http://schemas.microsoft.com/wpf/2005/10/xaml/entry" '
            b'Target="/Xaml/Document.xaml" Id="R1"/>\n'
            b"</Relationships>"
        )
