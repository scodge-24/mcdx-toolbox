"""
pymcdx CLI — Inspect, extract, audit, and generate Mathcad Prime .mcdx files.

Usage:
    pymcdx <command> [args]

Commands:
    generate   <input.py> <output.mcdx>     Convert Python to .mcdx
    build      <input.yaml> <output.mcdx>   Build a semantic YAML worksheet
    inspect    <file.mcdx|file.xml> [opts]   Pretty-print contents
    extract    <file.mcdx|file.xml> [opts]   Extract specific regions
    audit      <file.mcdx|file.xml> [opts]   Audit XML against XSDs
    render     <file.mcdx> -o <dir>         Render pages through Mathcad Prime to PNG
    render-diff <old> <new> -o <dir>        Compare two render directories page by page

"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import logging
import posixpath
import re
import sys
import zipfile
from collections import Counter
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from lxml import etree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WS_NS = "http://schemas.mathsoft.com/worksheet50"
ML_NS = "http://schemas.mathsoft.com/math50"
XS_NS = "http://www.w3.org/2001/XMLSchema"
NSMAP = {"ws": WS_NS, "ml": ML_NS}

XSD_DIR = Path(__file__).parent / "xsd"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_mcdx(path: Path) -> bool:
    return path.suffix.lower() == ".mcdx"


# Lossy fallbacks taken during a read command (e.g. a part that isn't
# well-formed XML dumped raw). Announced on stderr as they happen; with
# `inspect --strict` any entry here becomes a nonzero exit.
_degradations: list[str] = []


def _note_degradation(msg: str) -> None:
    _degradations.append(msg)
    print(f"Warning: {msg}", file=sys.stderr)


@contextlib.contextmanager
def _fail_loud_on_malformed(path: Path) -> Generator[None]:
    """Turn malformed-package/XML errors into a one-line Error + exit 1.

    KeyError is what zipfile raises for a missing archive member (e.g. a
    package without mathcad/worksheet.xml).
    """
    try:
        yield
    except zipfile.BadZipFile:
        print(f"Error: {path} is not a valid OPC package (corrupt or not a zip).", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"Error: {path} is missing an expected part: {e}", file=sys.stderr)
        sys.exit(1)
    except etree.XMLSyntaxError as e:
        print(f"Error: {path} contains malformed XML: {e}", file=sys.stderr)
        sys.exit(1)


def _read_worksheet_xml(path: Path) -> etree._Element:
    """Read worksheet XML from an .mcdx or raw .xml file."""
    if _is_mcdx(path):
        from pymcdx.opc import OpcPackageReader

        with OpcPackageReader(path) as reader:
            ws_bytes = reader.read_part("/mathcad/worksheet.xml")
        return etree.fromstring(ws_bytes)
    return etree.parse(str(path)).getroot()


def _get_regions(root: etree._Element) -> list[etree._Element]:
    """Extract region elements from a worksheet root."""
    # XPath element-selector queries always return a list of elements (never
    # bool/float/str, which only arise from XPath functions like count()).
    regions = cast("list[etree._Element]", root.xpath("//ws:region", namespaces=NSMAP))
    if not regions:
        regions = cast("list[etree._Element]", root.xpath("//*[local-name()='region']"))
    return regions


def _pretty_xml(element: etree._Element) -> str:
    return etree.tostring(element, pretty_print=True, encoding="unicode")


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _color(text: str, code: str) -> str:
    if _supports_color():
        return f"\033[{code}m{text}\033[0m"
    return text


def _green(text: str) -> str:
    return _color(text, "32")


def _red(text: str) -> str:
    return _color(text, "31")


def _yellow(text: str) -> str:
    return _color(text, "33")


def _bold(text: str) -> str:
    return _color(text, "1")


# ---------------------------------------------------------------------------
# generate command
# ---------------------------------------------------------------------------


def extract_docstring(code: str) -> str:
    """Extract the module-level docstring from Python code."""
    try:
        tree = ast.parse(code)
        return ast.get_docstring(tree) or ""
    except SyntaxError:
        return ""


def generate_mcdx(input_path: Path, output_path: Path) -> None:
    """Generate a .mcdx file from a Python source file."""
    from pymcdx.ast_converter import convert_python_to_math50
    from pymcdx.builder import WorksheetBuilder
    from pymcdx.packager import McdxPackager

    if not input_path.exists():
        print(f"Error: Input file {input_path} not found.", file=sys.stderr)
        sys.exit(1)

    code = input_path.read_text(encoding="utf-8")
    docstring = extract_docstring(code)
    math_elements = convert_python_to_math50(code)

    builder = WorksheetBuilder()
    builder.add_text_region(f"Generated from {input_path.name}")
    if docstring:
        builder.add_text_region(docstring)
    for elem in math_elements:
        builder.add_math_region(elem)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    McdxPackager(output_path, builder).create_package()


def cmd_generate(args) -> None:
    generate_mcdx(Path(args.input), Path(args.output))


def cmd_inspect(args):
    path = Path(args.file)
    if not path.exists():
        print(f"Error: {path} not found.")
        sys.exit(1)

    _degradations.clear()
    out_path: Path | None = Path(args.output) if args.output else None
    original_stdout = sys.stdout

    with contextlib.ExitStack() as stack:
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sys.stdout = stack.enter_context(out_path.open("w", encoding="utf-8"))

        try:
            with _fail_loud_on_malformed(path):
                if args.list_parts:
                    _inspect_list_parts(path)
                elif args.rels:
                    _inspect_rels(path)
                elif args.summary:
                    _inspect_summary(path)
                elif args.formatting:
                    _inspect_formatting(path, full=args.full)
                elif args.outline:
                    _inspect_outline(path, full=args.full)
                elif args.part:
                    _inspect_part(path, args.part)
                else:
                    _inspect_default(path)
        finally:
            if out_path is not None:
                sys.stdout = original_stdout
                print(f"Written to {out_path}", file=sys.stderr)

    if args.strict and _degradations:
        print(
            f"Error: --strict: {len(_degradations)} lossy fallback(s) encountered (see warnings).",
            file=sys.stderr,
        )
        sys.exit(2)


def _inspect_list_parts(path: Path):
    if not _is_mcdx(path):
        print("Error: --list-parts requires an .mcdx file.")
        sys.exit(1)
    from pymcdx.opc import OpcPackageReader

    with OpcPackageReader(path) as reader:
        print(f"Parts in {path.name}:")
        print(f"{'Part Name':<55} {'Content Type'}")
        print("-" * 100)
        for name, ct in reader.list_parts():
            print(f"  {name:<53} {ct}")


def _inspect_rels(path: Path):
    if not _is_mcdx(path):
        print("Error: --rels requires an .mcdx file.")
        sys.exit(1)
    from pymcdx.opc import OpcPackageReader

    with OpcPackageReader(path) as reader:
        pkg_rels = reader.get_relationships("/")
        if pkg_rels:
            print(_bold("Package-level relationships:"))
            for r in pkg_rels:
                print(f"  {r.rel_id:<10} {r.target:<50} {r.rel_type}")
            print()

        ws_rels = reader.get_relationships("/mathcad/worksheet.xml")
        if ws_rels:
            print(_bold("Worksheet relationships:"))
            for r in ws_rels:
                print(f"  {r.rel_id:<10} {r.target:<50} {r.rel_type}")


def _inspect_part(path: Path, part_name: str):
    if not _is_mcdx(path):
        print("Error: --part requires an .mcdx file.")
        sys.exit(1)
    from pymcdx.opc import OpcPackageReader

    with OpcPackageReader(path) as reader:
        data = reader.read_part(part_name)

        if part_name.endswith(".XamlPackage"):
            _print_xaml_package(data, part_name)
            return

        try:
            root = etree.fromstring(data)
            print(_pretty_xml(root))
        except etree.XMLSyntaxError:
            _note_degradation(f"part {part_name} is not well-formed XML; showing raw content")
            try:
                print(data.decode("utf-8"))
            except UnicodeDecodeError:
                print(f"Binary content ({len(data)} bytes)")


def _print_xaml_package(data: bytes, part_name: str):
    """Decode and print a FlowDocument XamlPackage (nested ZIP)."""
    print(f"XamlPackage: {part_name}")
    print("-" * 60)
    with zipfile.ZipFile(io.BytesIO(data)) as inner_zip:
        for name in inner_zip.namelist():
            print(f"\n--- {name} ---")
            content = inner_zip.read(name)
            try:
                root = etree.fromstring(content)
                print(_pretty_xml(root))
            except etree.XMLSyntaxError:
                _note_degradation(
                    f"{name} in {part_name} is not well-formed XML; showing raw content"
                )
                try:
                    print(content.decode("utf-8"))
                except UnicodeDecodeError:
                    print(f"  (binary, {len(content)} bytes)")


def _inspect_default(path: Path):
    """Pretty-print worksheet.xml or a raw XML file."""
    if _is_mcdx(path):
        from pymcdx.opc import OpcPackageReader

        with OpcPackageReader(path) as reader:
            data = reader.read_part("/mathcad/worksheet.xml")
        root = etree.fromstring(data)
    else:
        root = etree.parse(str(path)).getroot()
    print(_pretty_xml(root))


def _inspect_summary(path: Path):
    """Print a summary of regions, elements, and tags."""
    root = _read_worksheet_xml(path)
    regions = _get_regions(root)

    math_count = 0
    text_count = 0
    other_count = 0
    for r in regions:
        has_math = r.find(f"{{{WS_NS}}}math") is not None or r.find("math") is not None
        has_text = r.find(f"{{{WS_NS}}}text") is not None or r.find("text") is not None
        if has_math:
            math_count += 1
        elif has_text:
            text_count += 1
        else:
            other_count += 1

    ml_tags: Counter[str] = Counter()
    for elem in root.iter():
        tag = elem.tag
        if isinstance(tag, str) and ML_NS in tag:
            local = tag.split("}")[-1]
            ml_tags[f"ml:{local}"] += 1

    ws_tags: Counter[str] = Counter()
    for elem in root.iter():
        tag = elem.tag
        if isinstance(tag, str) and WS_NS in tag:
            local = tag.split("}")[-1]
            ws_tags[f"ws:{local}"] += 1

    label_values: Counter[str] = Counter()
    for elem in root.iter():
        labels = elem.get("labels")
        if labels:
            label_values[labels] += 1

    print(_bold(f"=== Summary: {path.name} ==="))
    print(f"Total regions: {len(regions)}")
    print(f"  Math regions:  {math_count}")
    print(f"  Text regions:  {text_count}")
    if other_count:
        print(f"  Other regions: {other_count}")
    print()

    print(f"Unique ml: tags ({len(ml_tags)}):")
    for tag, count in ml_tags.most_common():
        print(f"  {tag:<30} {count:>6}")
    print()

    if ws_tags:
        print(f"Unique ws: tags ({len(ws_tags)}):")
        for tag, count in ws_tags.most_common():
            print(f"  {tag:<30} {count:>6}")
        print()

    if label_values:
        print(f"Label values ({len(label_values)}):")
        for val, count in label_values.most_common():
            print(f"  {val:<30} {count:>6}")


# ---------------------------------------------------------------------------
# outline rendering — condensed pseudo-expression view
# ---------------------------------------------------------------------------

_OP_SYMBOLS = {
    "plus": "+",
    "minus": "-",
    "mult": "·",
    "div": "/",
    "pow": "^",
    "mod": "mod",
    "equal": "=",
    "notEqual": "≠",
    "lessThan": "<",
    "lessOrEqual": "≤",
    "greaterThan": ">",
    "greaterOrEqual": "≥",
    "and": "∧",
    "or": "∨",
}

_PRECEDENCE = {
    "or": 1,
    "and": 2,
    "equal": 3,
    "notEqual": 3,
    "lessThan": 3,
    "lessOrEqual": 3,
    "greaterThan": 3,
    "greaterOrEqual": 3,
    "plus": 4,
    "minus": 4,
    "mult": 5,
    "div": 5,
    "mod": 5,
    "pow": 6,
}


def _ml_local(elem: etree._Element) -> str:
    tag = elem.tag
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}")[-1]
    return str(tag)


def _get_op(elem: etree._Element) -> str | None:
    if _ml_local(elem) != "apply":
        return None
    children = list(elem)
    if len(children) < 3:
        return None
    op = _ml_local(children[0])
    return op if op in _OP_SYMBOLS else None


def _paren_child(child: etree._Element, parent_op: str, *, right: bool = False) -> str:
    text = _render_ml(child)
    child_op = _get_op(child)
    if child_op is None:
        return text
    parent_prec = _PRECEDENCE.get(parent_op, 0)
    child_prec = _PRECEDENCE.get(child_op, 0)
    if right and parent_op in ("div", "minus"):
        if child_prec <= parent_prec:
            return f"({text})"
    elif child_prec < parent_prec:
        return f"({text})"
    return text


def _render_ml(elem: etree._Element) -> str:
    """Recursively render an ml: element as a condensed pseudo-expression."""
    tag = _ml_local(elem)

    if tag == "real":
        return elem.text or "?"

    if tag == "str":
        return f'"{elem.text or ""}"'

    if tag == "id":
        text_parts = []
        if elem.text and elem.text.strip():
            text_parts.append(elem.text.strip())
        for child in elem:
            local = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""
            if local == "Span":
                text_parts.append(_render_span(child))
        if text_parts:
            return "".join(text_parts)
        return "?"

    if tag == "placeholder":
        return "⬚"

    if tag == "parens":
        children = list(elem)
        if children:
            return f"({_render_ml(children[0])})"
        return "()"

    if tag == "eval":
        children = list(elem)
        parts = []
        for child in children:
            local = _ml_local(child)
            if local not in ("unitOverride", "resultData", "resultFormat"):
                parts.append(_render_ml(child))
        expr = " ".join(parts) if parts else "?"
        return f"{expr} ⇒"

    if tag == "unitOverride":
        children = list(elem)
        if children:
            return _render_ml(children[0])
        return ""

    if tag == "define":
        children = list(elem)
        if len(children) >= 2:
            return f"{_render_ml(children[0])} := {_render_ml(children[1])}"
        return "define(?)"

    if tag == "localDefine":
        children = list(elem)
        if len(children) >= 2:
            return f"{_render_ml(children[0])} ← {_render_ml(children[1])}"
        return "localDefine(?)"

    if tag == "apply":
        children = list(elem)
        if not children:
            return "apply()"
        op = _ml_local(children[0])
        args = [_render_ml(c) for c in children[1:]]

        if op in _OP_SYMBOLS and len(args) == 2:
            sym = _OP_SYMBOLS[op]
            a = _paren_child(children[1], op)
            b = _paren_child(children[2], op, right=True)
            if op == "pow":
                return f"{a}^{b}"
            if op == "div":
                return f"{a} / {b}"
            return f"{a} {sym} {b}"

        if op == "neg" and len(args) == 1:
            return f"-{args[0]}"
        if op == "absval" and len(args) == 1:
            return f"|{args[0]}|"
        if op == "transpose" and len(args) == 1:
            return f"{args[0]}ᵀ"
        if op == "indexer" and len(args) >= 2:
            return f"{args[0]}[{', '.join(args[1:])}]"
        if op == "sum" and len(args) >= 1:
            return f"Σ({', '.join(args)})"

        # nth root: apply(nthRoot, degree, radicand) → degree√(radicand)
        if op == "nthRoot" and len(args) == 2:
            return f"{args[0]}√({args[1]})"

        # Integral: apply(integral, lambda(...), lowerBound, upperBound)
        if op == "integral":
            body = ""
            lower = ""
            upper = ""
            for child in children[1:]:
                local = _ml_local(child)
                if local == "lambda":
                    body = _render_ml(child)
                elif local == "lowerBound":
                    lc = list(child)
                    lower = _render_ml(lc[0]) if lc else "?"
                elif local == "upperBound":
                    uc = list(child)
                    upper = _render_ml(uc[0]) if uc else "?"
            bounds = f"({lower}..{upper})" if lower or upper else ""
            return f"∫{bounds} {body}"

        if op == "id":
            func_name = _render_ml(children[0])
            return f"{func_name}({', '.join(args)})"
        return f"{op}({', '.join(args)})"

    if tag == "function":
        parts = []
        bound_vars = []
        for child in elem:
            local = _ml_local(child)
            if local == "id":
                parts.append(_render_ml(child))
            elif local == "boundVars":
                for bv in child:
                    bound_vars.append(_render_ml(bv))
        name = parts[0] if parts else "?"
        return f"{name}({', '.join(bound_vars)})"

    if tag == "program":
        return " ; ".join(_render_ml(child) for child in elem)

    if tag == "if":
        parts = []
        for child in elem:
            local = _ml_local(child)
            if local == "test":
                parts.append(f"if {_render_ml_children(child)}")
            elif local == "then":
                parts.append(f"then {{ {_render_ml_children(child)} }}")
            elif local == "elseif":
                for sub in child:
                    sl = _ml_local(sub)
                    if sl == "test":
                        parts.append(f"elseif {_render_ml_children(sub)}")
                    elif sl == "then":
                        parts.append(f"then {{ {_render_ml_children(sub)} }}")
            elif local == "else":
                parts.append(f"else {{ {_render_ml_children(child)} }}")
        return " ".join(parts)

    if tag == "for":
        children = list(elem)
        var = _render_ml(children[0]) if children else "?"
        range_expr = ""
        body = ""
        for child in children[1:]:
            local = _ml_local(child)
            if local == "range":
                range_expr = _render_range(child)
            elif local == "program":
                body = _render_ml(child)
        return f"for {var} ∈ {range_expr} {{ {body} }}"

    if tag == "while":
        children = list(elem)
        cond = _render_ml(children[0]) if children else "?"
        body = ""
        for child in children[1:]:
            if _ml_local(child) == "program":
                body = _render_ml(child)
        return f"while {cond} {{ {body} }}"

    if tag == "lambda":
        bound_vars = []
        body = ""
        for child in elem:
            local = _ml_local(child)
            if local == "boundVars":
                for bv in child:
                    bound_vars.append(_render_ml(bv))
            else:
                body = _render_ml(child)
        return f"λ({', '.join(bound_vars)}) → {body}"

    if tag == "matrix":
        rows_attr = elem.get("rows", "?")
        cols_attr = elem.get("cols", "?")
        cells = [_render_ml(child) for child in elem]
        return f"[{' ; '.join(cells)}] ({rows_attr}×{cols_attr})"

    if tag == "return":
        children = list(elem)
        if children:
            return f"return {_render_ml(children[0])}"
        return "return"

    if tag == "range":
        return _render_range(elem)

    if tag == "sequence":
        children = list(elem)
        return ", ".join(_render_ml(c) for c in children)

    # Fallback
    children = list(elem)
    if children:
        return " ".join(_render_ml(c) for c in children)
    if elem.text and elem.text.strip():
        return elem.text.strip()
    return f"<{tag}>"


def _render_ml_children(elem: etree._Element) -> str:
    children = list(elem)
    if len(children) == 1:
        return _render_ml(children[0])
    return " ".join(_render_ml(c) for c in children)


# ---------------------------------------------------------------------------
# structured multi-line renderer — used by outline for nested constructs
# ---------------------------------------------------------------------------

# Tags whose bodies should be expanded into multiple lines
_BLOCK_TAGS = {"program", "if", "for", "while"}


def _render_ml_lines(elem: etree._Element, indent: int = 0) -> list[tuple[int, str]]:
    """Render an ml: element as structured lines: list of (indent_level, text).

    Block constructs (program, if, for, while) are expanded with indentation.
    Everything else falls back to the flat single-line renderer.
    """
    tag = _ml_local(elem)

    if tag == "define":
        children = list(elem)
        if len(children) >= 2:
            lhs = _render_ml(children[0])
            if _ml_local(children[1]) in _BLOCK_TAGS:
                lines = [(indent, f"{lhs} :=")]
                lines.extend(_render_ml_lines(children[1], indent + 1))
                return lines
            rhs = _render_ml(children[1])
            return [(indent, f"{lhs} := {rhs}")]
        return [(indent, "define(?)")]

    if tag == "localDefine":
        children = list(elem)
        if len(children) >= 2:
            lhs = _render_ml(children[0])
            if _ml_local(children[1]) in _BLOCK_TAGS:
                lines = [(indent, f"{lhs} ←")]
                lines.extend(_render_ml_lines(children[1], indent + 1))
                return lines
            rhs = _render_ml(children[1])
            return [(indent, f"{lhs} ← {rhs}")]
        return [(indent, "localDefine(?)")]

    if tag == "program":
        lines: list[tuple[int, str]] = []
        for child in elem:
            lines.extend(_render_ml_lines(child, indent))
        return lines

    if tag == "if":
        lines = []
        for child in elem:
            local = _ml_local(child)
            if local == "test":
                lines.append((indent, f"if {_render_ml_children(child)}"))
            elif local == "then":
                for sub in child:
                    lines.extend(_render_ml_lines(sub, indent + 1))
            elif local == "elseif":
                for sub in child:
                    sl = _ml_local(sub)
                    if sl == "test":
                        lines.append((indent, f"elseif {_render_ml_children(sub)}"))
                    elif sl == "then":
                        for s2 in sub:
                            lines.extend(_render_ml_lines(s2, indent + 1))
            elif local == "else":
                lines.append((indent, "else"))
                for sub in child:
                    lines.extend(_render_ml_lines(sub, indent + 1))
        return lines

    if tag == "for":
        children = list(elem)
        var = _render_ml(children[0]) if children else "?"
        range_expr = ""
        body = None
        for child in children[1:]:
            local = _ml_local(child)
            if local == "range":
                range_expr = _render_range(child)
            elif local == "program":
                body = child
        lines = [(indent, f"for {var} ∈ {range_expr}")]
        if body is not None:
            lines.extend(_render_ml_lines(body, indent + 1))
        return lines

    if tag == "while":
        children = list(elem)
        cond = _render_ml(children[0]) if children else "?"
        body = None
        for child in children[1:]:
            if _ml_local(child) == "program":
                body = child
        lines = [(indent, f"while {cond}")]
        if body is not None:
            lines.extend(_render_ml_lines(body, indent + 1))
        return lines

    # Everything else: flat single-line rendering
    return [(indent, _render_ml(elem))]


def _render_range(elem: etree._Element) -> str:
    children = list(elem)
    if len(children) == 2:
        return f"{_render_ml(children[0])}..{_render_ml(children[1])}"
    if len(children) == 3:
        return f"{_render_ml(children[0])},{_render_ml(children[1])}..{_render_ml(children[2])}"
    return "range(?)"


def _render_span(span: etree._Element) -> str:
    base = (span.text or "").strip()
    sub = ""
    has_subscript = False
    for child in span:
        local = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""
        if local == "Subscript":
            has_subscript = True
            sub = (child.text or "").strip()
    if sub:
        return f"{base}_{sub}"
    if has_subscript and base:
        return f"{base}."
    return base


def _read_text_content(path: Path, item_idref: str) -> str:
    """Read the plain text from a FlowDocument XamlPackage referenced by item-idref."""
    if not _is_mcdx(path):
        return f"(text: {item_idref})"

    from pymcdx.opc import OpcPackageReader

    try:
        with OpcPackageReader(path) as reader:
            ws_rels = reader.get_relationships("/mathcad/worksheet.xml")
            target = None
            for r in ws_rels:
                if r.rel_id == item_idref:
                    target = r.target
                    break
            if not target:
                return f"(text: {item_idref})"

            data = reader.read_part(target)
            with zipfile.ZipFile(io.BytesIO(data)) as inner:
                for name in inner.namelist():
                    if name.endswith(".xaml"):
                        xaml = inner.read(name).decode("utf-8")
                        return _extract_text_from_xaml(xaml)
    except Exception:
        logger.debug("Failed to read text content for %s", item_idref, exc_info=True)
        _note_degradation(f"could not read text content for {item_idref}; showing placeholder")
    return f"(text: {item_idref})"


def _extract_text_from_xaml(xaml: str) -> str:
    try:
        root = etree.fromstring(xaml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return xaml

    parts = []
    for elem in root.iter():
        local = etree.QName(elem.tag).localname if isinstance(elem.tag, str) else ""
        if local == "Run" and elem.text:
            parts.append(elem.text.strip())
    return " ".join(parts) if parts else "(empty text)"


def _is_section_header(text: str) -> bool:
    """Detect if text looks like a section/clause header."""
    t = text.strip()
    if re.search(r"\bCl\.\s*\d", t):
        return True
    return bool(re.match(r"(EN|EC\d|AS|NZS|BS)\s", t))


def _is_inputs_label(text: str) -> bool:
    """Detect 'Inputs:' or 'Inputs' text."""
    return text.strip().rstrip(":").lower() == "inputs"


# ---------------------------------------------------------------------------
# formatting-aware text inspection
# ---------------------------------------------------------------------------

# These are the WPF attributes that are useful in a compact inspection report.
# A FlowDocument contains many Typography.* attributes in a real Prime file;
# dumping all of them makes a report noisy without making the effective prose
# style easier to understand.
_FORMATTING_STYLE_ATTRS = (
    "FontFamily",
    "FontSize",
    "FontWeight",
    "FontStyle",
    "FontStretch",
    "Foreground",
    "Background",
    "TextAlignment",
    "TextIndent",
    "LineHeight",
    "style",
)


@dataclass(frozen=True, slots=True)
class _FormattingRun:
    paragraph_index: int
    run_index: int
    start_caret: int
    text: str
    style: dict[str, str]


@dataclass(frozen=True, slots=True)
class _FormattingParagraph:
    index: int
    start_caret: int
    runs: tuple[_FormattingRun, ...]


@dataclass(frozen=True, slots=True)
class _FormattingChild:
    region_id: str
    kind: str
    expression: str
    caret: int | None
    width: str
    height: str
    xml_index: int


def _xml_local(elem: etree._Element) -> str:
    """Return an element's local name without assuming a namespace."""

    tag = elem.tag
    if not isinstance(tag, str):
        return "?"
    return tag.rsplit("}", 1)[-1]


def _top_level_regions(root: etree._Element) -> list[tuple[int, etree._Element]]:
    """Return only worksheet ``<regions>`` children with their XML positions."""

    containers = [child for child in root if _xml_local(child) == "regions"]
    if not containers:
        return []
    return [
        (index, child) for index, child in enumerate(containers[0]) if _xml_local(child) == "region"
    ]


def _stored_float(region: etree._Element, attribute: str) -> float | None:
    value = region.get(attribute)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        _note_degradation(
            f"region {region.get('region-id', '?')} has a non-numeric {attribute}={value!r}"
        )
        return None


def _formatting_sort_key(item: tuple[int, etree._Element]) -> tuple[float, float, int]:
    xml_index, region = item
    top = _stored_float(region, "top")
    left = _stored_float(region, "left")
    # Missing geometry is retained and shown, but sorts after regions whose
    # stored position can be interpreted.  XML order remains the final tie
    # breaker, making reports deterministic even for malformed input.
    return (
        float("inf") if top is None else top,
        float("inf") if left is None else left,
        xml_index,
    )


def _region_kind_for_formatting(region: etree._Element) -> str:
    known = {"text", "math", "pageBreak", "picture", "Area"}
    for child in region:
        local = _xml_local(child)
        if local in known:
            return local
    children = [_xml_local(child) for child in region]
    return "unknown" if not children else f"unknown ({', '.join(children)})"


def _format_stored_box(region: etree._Element) -> str:
    fields = ("left", "top", "width", "height", "actualWidth", "actualHeight")
    return " ".join(f"{field}={region.get(field, '?')}" for field in fields)


def _format_style(style: Mapping[str, str]) -> str:
    if not style:
        return "(none)"
    return " ".join(f"{key}={style[key]}" for key in _FORMATTING_STYLE_ATTRS if key in style)


def _effective_style(*elements: etree._Element | None) -> dict[str, str]:
    """Merge root → paragraph → run style attributes in document order."""

    style: dict[str, str] = {}
    for element in elements:
        if element is None:
            continue
        for name in _FORMATTING_STYLE_ATTRS:
            value = element.get(name)
            if value is not None:
                style[name] = value
    return style


def _normalise_part_target(target: str) -> str:
    """Resolve a worksheet relationship target to an OPC part name."""

    if target.startswith("/"):
        return posixpath.normpath(target)
    return "/" + posixpath.normpath(posixpath.join("mathcad", target)).lstrip("/")


def _read_formatting_xaml(
    path: Path,
    text_element: etree._Element,
) -> tuple[etree._Element | None, str | None]:
    """Read a text region's external XAML root, degrading through inspect policy."""

    item_idref = text_element.get("item-idref", "")
    embedded = next(
        (child for child in text_element if _xml_local(child) in {"FlowDocument", "Section"}),
        None,
    )
    if not _is_mcdx(path):
        has_paragraphs = embedded is not None and any(
            _xml_local(child) == "Paragraph" for child in embedded.iter()
        )
        if has_paragraphs:
            return embedded, "embedded worksheet XAML"
        _note_degradation(
            "text region has no external FlowDocument in raw XML input "
            f"(item-idref={item_idref or '?'})"
        )
        return embedded, None

    if not item_idref:
        _note_degradation("text region is missing item-idref for its external FlowDocument")
        return embedded, None

    from pymcdx.opc import OpcPackageReader

    try:
        with OpcPackageReader(path) as reader:
            target = next(
                (
                    relationship.target
                    for relationship in reader.get_relationships("/mathcad/worksheet.xml")
                    if relationship.rel_id == item_idref
                ),
                None,
            )
            if target is None:
                _note_degradation(
                    f"no worksheet relationship for text FlowDocument {item_idref}; "
                    "showing stored worksheet metadata only"
                )
                return embedded, None
            part_name = _normalise_part_target(target)
            package_data = reader.read_part(part_name)
            with zipfile.ZipFile(io.BytesIO(package_data)) as package:
                xaml_name = next(
                    (name for name in sorted(package.namelist()) if name.lower().endswith(".xaml")),
                    None,
                )
                if xaml_name is None:
                    _note_degradation(
                        f"FlowDocument package {part_name} has no XAML document; "
                        "showing stored worksheet metadata only"
                    )
                    return embedded, part_name
                raw_xaml = package.read(xaml_name)
            root = etree.fromstring(raw_xaml)
            return root, part_name
    except (
        KeyError,
        UnicodeDecodeError,
        ValueError,
        zipfile.BadZipFile,
        etree.XMLSyntaxError,
    ) as exc:
        _note_degradation(
            f"could not read or parse text FlowDocument {item_idref}: {exc}; "
            "showing stored worksheet metadata only"
        )
        logger.debug("Failed to read formatting XAML for %s", item_idref, exc_info=True)
        return embedded, None


def _paragraphs_from_xaml(root: etree._Element) -> tuple[_FormattingParagraph, ...]:
    """Capture paragraph/run text and WPF caret starts using the known symbol rule."""

    paragraphs: list[_FormattingParagraph] = []
    cursor = 1
    paragraph_nodes = [element for element in root.iter() if _xml_local(element) == "Paragraph"]
    for paragraph_index, paragraph in enumerate(paragraph_nodes, start=1):
        paragraph_start = cursor
        runs: list[_FormattingRun] = []
        for run_index, run in enumerate(
            (element for element in paragraph.iter() if _xml_local(element) == "Run"),
            start=1,
        ):
            # WPF Run content is text; keeping the direct text node avoids
            # lxml's bytes-capable itertext typing and preserves whitespace.
            text = run.text or ""
            runs.append(
                _FormattingRun(
                    paragraph_index=paragraph_index,
                    run_index=run_index,
                    start_caret=cursor,
                    text=text,
                    style=_effective_style(root, paragraph, run),
                )
            )
            cursor += len(text) + 2
        paragraphs.append(
            _FormattingParagraph(
                index=paragraph_index,
                start_caret=paragraph_start,
                runs=tuple(runs),
            )
        )
        # A Paragraph contributes two container symbols between paragraphs.
        cursor += 2
    return tuple(paragraphs)


def _formatting_children(region: etree._Element) -> tuple[_FormattingChild, ...]:
    """Read direct nested regions in their persisted caret/XML order."""

    text_element = next(
        (child for child in region if _xml_local(child) == "text"),
        None,
    )
    if text_element is None:
        return ()
    nested_container = next(
        (child for child in text_element if _xml_local(child) == "regions"),
        None,
    )
    if nested_container is None:
        return ()

    children: list[_FormattingChild] = []
    for xml_index, child in enumerate(
        element for element in nested_container if _xml_local(element) == "region"
    ):
        raw_caret = child.get("text-caret-position")
        caret: int | None
        if raw_caret is None:
            caret = None
            _note_degradation(
                f"nested region {child.get('region-id', '?')} has no text-caret-position"
            )
        else:
            try:
                caret = int(raw_caret)
            except ValueError:
                caret = None
                _note_degradation(
                    "nested region "
                    f"{child.get('region-id', '?')} has invalid "
                    f"text-caret-position={raw_caret!r}"
                )

        math_element = next(
            (element for element in child if _xml_local(element) == "math"),
            None,
        )
        if math_element is None:
            expression = "(non-math child)"
        else:
            math_nodes = list(math_element)
            if not math_nodes:
                expression = "(empty math)"
            else:
                try:
                    expression = _render_ml(math_nodes[0])
                except (TypeError, ValueError, etree.LxmlError) as exc:
                    expression = "(unrenderable math)"
                    _note_degradation(
                        "nested math region "
                        f"{child.get('region-id', '?')} could not be rendered: {exc}"
                    )

        children.append(
            _FormattingChild(
                region_id=child.get("region-id", "?"),
                kind=_region_kind_for_formatting(child),
                expression=expression,
                caret=caret,
                width=child.get("actualWidth", child.get("width", "?")),
                height=child.get("actualHeight", child.get("height", "?")),
                xml_index=xml_index,
            )
        )
    return tuple(
        sorted(
            children,
            key=lambda child: (
                float("inf") if child.caret is None else child.caret,
                child.xml_index,
            ),
        )
    )


def _child_marker(child: _FormattingChild, *, full: bool) -> str:
    expression = _trunc(child.expression, 80, full=full)
    return f"[math {child.region_id}: {expression}]"


def _merge_formatting_text(
    paragraphs: tuple[_FormattingParagraph, ...],
    children: tuple[_FormattingChild, ...],
    *,
    full: bool,
) -> str:
    """Replace one-character WPF inline slots with deterministic math markers."""

    markers: dict[tuple[int, int, int], list[_FormattingChild]] = {}
    unplaced: list[_FormattingChild] = []
    runs = [run for paragraph in paragraphs for run in paragraph.runs]
    for child in children:
        if child.caret is None:
            unplaced.append(child)
            continue
        placed = False
        for run in runs:
            run_end = run.start_caret + len(run.text) + 2
            if child.caret == run.start_caret:
                markers.setdefault((run.paragraph_index, run.run_index, 0), []).append(child)
                placed = True
                break
            if run.start_caret < child.caret < run_end:
                offset = min(len(run.text), max(0, child.caret - run.start_caret - 1))
                markers.setdefault((run.paragraph_index, run.run_index, offset), []).append(child)
                placed = True
                break
        if not placed:
            _note_degradation(
                "nested math region "
                f"{child.region_id} caret {child.caret} does not map to a XAML Run"
            )
            unplaced.append(child)

    parts: list[str] = []
    for paragraph in paragraphs:
        for run in paragraph.runs:
            text = run.text
            run_markers = markers
            for offset in range(len(text) + 1):
                for child in run_markers.get((run.paragraph_index, run.run_index, offset), []):
                    parts.append(_child_marker(child, full=full))
                if offset < len(text) and not (
                    offset == 0
                    and len(text) == 1
                    and text == " "
                    and (run.paragraph_index, run.run_index, 0) in markers
                ):
                    # A one-character blank Run is Prime's persisted inline
                    # object slot.  Remove it only when a child occupies it.
                    parts.append(text[offset])
        if paragraph.index != paragraphs[-1].index:
            parts.append("\n")
    if unplaced:
        if parts and not parts[-1].endswith(" "):
            parts.append(" ")
        parts.extend(_child_marker(child, full=full) for child in unplaced)
    return "".join(parts) or "(empty text)"


def _inspect_formatting(path: Path, *, full: bool = False) -> None:
    """Print a text-first account of persisted worksheet presentation facts."""

    root = _read_worksheet_xml(path)
    indexed_regions = sorted(_top_level_regions(root), key=_formatting_sort_key)

    print(_bold(f"=== Formatting: {path.name} ({len(indexed_regions)} direct regions) ==="))
    print("Text-first inspection of stored worksheet geometry and external XAML.")
    print("Stored boxes, styles, and carets are persisted facts.")
    print("Line wrapping is not reproduced here.")
    print("This is not a Prime render (use layout-preview for geometry only).")
    print()

    for xml_index, region in indexed_regions:
        kind = _region_kind_for_formatting(region)
        region_id = region.get("region-id", "?")
        print(f"[{region_id}] kind={kind} xml-index={xml_index}")
        print(f"  stored box: {_format_stored_box(region)}")

        if kind != "text":
            if kind == "math":
                math_element = next(
                    (child for child in region if _xml_local(child) == "math"),
                    None,
                )
                math_nodes = [] if math_element is None else list(math_element)
                if math_nodes:
                    print(f"  expression: {_trunc(_render_ml(math_nodes[0]), 320, full=full)}")
            print()
            continue

        text_element = next(
            (child for child in region if _xml_local(child) == "text"),
            None,
        )
        if text_element is None:
            _note_degradation(f"text region {region_id} has no <text> element")
            print("  xaml: unavailable (missing <text> element)")
            print("  merged text: (unavailable)")
            print()
            continue

        xaml_root, xaml_source = _read_formatting_xaml(path, text_element)
        children = _formatting_children(region)
        if xaml_root is None:
            print(f"  xaml: unavailable (item-idref={text_element.get('item-idref', '?')})")
            print("  merged text: (unavailable)")
            if children:
                print("  nested children (not merged):")
                for child in children:
                    print(
                        f"    id={child.region_id} kind={child.kind} expression={child.expression} "
                        f"caret={child.caret if child.caret is not None else '?'} "
                        f"size={child.width}×{child.height} (stored)"
                    )
            print()
            continue

        paragraphs = _paragraphs_from_xaml(xaml_root)
        embedded_root = next(
            (child for child in text_element if _xml_local(child) in {"FlowDocument", "Section"}),
            None,
        )
        root_style = _effective_style(embedded_root, xaml_root)
        print(
            f"  xaml: source={xaml_source or 'embedded'} root={_xml_local(xaml_root)} "
            f"paragraphs={len(paragraphs)}"
        )
        print(f"  effective root style: {_format_style(root_style)}")
        style_overrides: list[str] = []
        for paragraph in paragraphs:
            for run in paragraph.runs:
                differences = {
                    key: value for key, value in run.style.items() if root_style.get(key) != value
                }
                if differences:
                    summary = _format_style(differences)
                    if summary not in style_overrides:
                        style_overrides.append(summary)
        if style_overrides:
            print(f"  run style overrides: {'; '.join(style_overrides)}")

        merged_text = _merge_formatting_text(paragraphs, children, full=full)
        print(f"  merged text: {_trunc(merged_text, 320, full=full)}")
        if children:
            print("  nested math children (stored caret/size):")
            for child in children:
                expression = _trunc(child.expression, 240, full=full)
                print(
                    f"    id={child.region_id} kind={child.kind} expression={expression} "
                    f"caret={child.caret if child.caret is not None else '?'} "
                    f"size={child.width}×{child.height} (stored)"
                )
        print()


def _is_simple_input(expr: str) -> bool:
    """Check if a math expression is a simple input definition (name := literal)."""
    if ":=" not in expr:
        return False
    _lhs, rhs = expr.split(":=", 1)
    rhs = rhs.strip()
    if rhs == "⬚":
        return True
    if re.match(r"^-?\d+\.?\d*$", rhs):
        return True
    return bool(rhs.startswith('"') and rhs.endswith('"'))


def _extract_lhs(expr: str) -> str:
    """Extract the variable name from a definition expression."""
    if ":=" in expr:
        return expr.split(":=")[0].strip()
    return expr


def _classify_region(region: etree._Element, path: Path) -> tuple[str, str, etree._Element | None]:
    """Classify a region and extract its rendered content.

    Returns (kind, content, ml_elem) where kind is 'math', 'text', 'pagebreak',
    or 'unknown'.  For math regions *ml_elem* is the top-level ml: element
    (needed by the structured renderer); for everything else it is ``None``.
    """
    math_elem = region.find(f"{{{WS_NS}}}math")
    if math_elem is None:
        math_elem = region.find("math")
    if math_elem is not None:
        ml_children = list(math_elem)
        if ml_children:
            return "math", _render_ml(ml_children[0]), ml_children[0]
        return "math", "(empty)", None

    text_elem = region.find(f"{{{WS_NS}}}text")
    if text_elem is None:
        text_elem = region.find("text")
    if text_elem is not None:
        idref = text_elem.get("item-idref", "")
        return "text", _read_text_content(path, idref), None

    pb = region.find(f"{{{WS_NS}}}pageBreak")
    if pb is None:
        pb = region.find("pageBreak")
    if pb is not None:
        return "pagebreak", "", None

    return "unknown", "", None


def _trunc(text: str, limit: int, *, full: bool) -> str:
    """Truncate to `limit` chars with an ellipsis unless `full` is set (bug #8).

    Long expressions silently cut with '...' have hidden real content (a wrong
    NR residual source term was once masked this way), so `--full` disables it.
    """
    if full or len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _inspect_outline(path: Path, *, full: bool = False):
    """Print a structured, hierarchical outline of the worksheet."""
    root = _read_worksheet_xml(path)
    regions = _get_regions(root)

    print(_bold(f"=== Outline: {path.name} ({len(regions)} regions) ==="))
    print()

    # Parse all regions into typed items
    items: list[tuple[str, str, str, etree._Element | None]] = []
    for r in regions:
        rid = r.get("region-id", "?")
        kind, content, ml_elem = _classify_region(r, path)
        items.append((rid, kind, content, ml_elem))

    # Render with structure
    first_section = True
    i = 0
    while i < len(items):
        rid, kind, content, ml_elem = items[i]

        if kind == "pagebreak":
            print(f"  {'─' * 50}")
            print()
            i += 1
            continue

        if kind == "unknown":
            print(f"  {_yellow(f'[{rid}]')} (unknown region type)")
            i += 1
            continue

        if kind == "text":
            # Unresolvable text (raw XML without FlowDocument access)
            is_unresolved = content.startswith("(text:")
            if is_unresolved:
                if not first_section:
                    print()
                first_section = False
                print(f"  ── {_yellow(f'[{rid}]')} {_bold('text')} ──────────────────────────────")
            elif _is_section_header(content):
                if not first_section:
                    print()
                first_section = False
                trunc = _trunc(content, 100, full=full)
                print(f"  {_bold('▸')} {_yellow(f'[{rid}]')} {_bold(trunc)}")
            elif _is_inputs_label(content):
                input_names: list[str] = []
                j = i + 1
                while j < len(items):
                    _jrid, jkind, jcontent, _jml = items[j]
                    if jkind == "math" and _is_simple_input(jcontent):
                        input_names.append(_extract_lhs(jcontent))
                        j += 1
                    else:
                        break
                if input_names:
                    names = ", ".join(input_names)
                    names = _trunc(names, 80, full=full)
                    print(f"  │  {_green('Inputs')}: {names}")
                    i = j
                    continue
                print(f"  │  {_green('Inputs:')}")
            else:
                trunc = _trunc(content, 100, full=full)
                print(f"  ├─ {trunc}")
            i += 1
            continue

        if kind == "math":
            # Try structured multi-line rendering for nested constructs
            if ml_elem is not None:
                lines = _render_ml_lines(ml_elem)
                if len(lines) > 1:
                    for lvl, text in lines:
                        pad = "  " * lvl
                        trunc = _trunc(text, 100, full=full)
                        print(f"  │  {pad}{trunc}")
                    i += 1
                    continue
            # Single-line fallback
            trunc = _trunc(content, 120, full=full)
            print(f"  │  {trunc}")
            i += 1
            continue

        i += 1


# ---------------------------------------------------------------------------
# extract command
# ---------------------------------------------------------------------------


def cmd_extract(args):
    path = Path(args.file)
    if not path.exists():
        print(f"Error: {path} not found.", file=sys.stderr)
        sys.exit(1)

    with _fail_loud_on_malformed(path):
        root = _read_worksheet_xml(path)
    regions = _get_regions(root)

    if not regions:
        print("No regions found.", file=sys.stderr)
        sys.exit(1)

    matched = []

    if args.region is not None:
        rid = str(args.region)
        for r in regions:
            if r.get("region-id") == rid:
                matched.append(r)
        if not matched:
            print(f"Error: region-id '{rid}' not found.", file=sys.stderr)
            sys.exit(1)

    elif args.contains:
        tag = args.contains
        if ":" in tag:
            prefix, local = tag.split(":", 1)
            ns = NSMAP.get(prefix)
            for r in regions:
                if ns:
                    found = r.xpath(f".//{prefix}:{local}", namespaces=NSMAP)
                else:
                    found = r.xpath(f".//*[local-name()='{local}']")
                if found:
                    matched.append(r)
        else:
            for r in regions:
                found = r.xpath(f".//*[local-name()='{tag}']")
                if found:
                    matched.append(r)

    elif args.grep:
        pattern = re.compile(args.grep)
        for r in regions:
            xml_str = etree.tostring(r, encoding="unicode")
            if pattern.search(xml_str):
                matched.append(r)

    elif args.first:
        matched = regions[: args.first]

    else:
        matched = regions

    output_parts = []
    for r in matched:
        rid = r.get("region-id", "?")
        header = f"<!-- source: {path.name}  region-id: {rid} -->"
        xml_str = _pretty_xml(r)
        output_parts.append(f"{header}\n{xml_str}")

    result = "\n".join(output_parts)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result, encoding="utf-8")
        print(f"Extracted {len(matched)} region(s) to {out_path}", file=sys.stderr)
    else:
        print(result)

    if not matched:
        print("No matching regions found.", file=sys.stderr)


# ---------------------------------------------------------------------------
# audit command
# ---------------------------------------------------------------------------


def cmd_audit(args):
    path = Path(args.file)
    if not path.exists():
        print(f"Error: {path} not found.", file=sys.stderr)
        sys.exit(1)

    xml_roots = []
    if path.is_dir():
        for f in sorted(path.iterdir()):
            if f.suffix in (".xml", ".mcdx"):
                try:
                    xml_roots.append((f.name, _read_worksheet_xml(f)))
                except Exception as e:
                    print(f"Warning: skipping {f.name}: {e}", file=sys.stderr)
    else:
        with _fail_loud_on_malformed(path):
            xml_roots.append((path.name, _read_worksheet_xml(path)))

    if not xml_roots:
        print("No XML files found to audit.", file=sys.stderr)
        sys.exit(1)

    schema_elements = _load_schema_elements()
    schema_attrs = _load_schema_attributes()

    ml_element_counts: Counter[str] = Counter()
    ml_attr_counts: Counter[str] = Counter()
    label_values: Counter[str] = Counter()

    for _filename, root in xml_roots:
        for elem in root.iter():
            tag = elem.tag
            if isinstance(tag, str) and ML_NS in tag:
                local = tag.split("}")[-1]
                ml_element_counts[local] += 1

            for attr_name, attr_val in elem.attrib.items():
                if attr_name.startswith("{"):
                    ns_part = attr_name.split("}")[0] + "}"
                    local_attr = attr_name[len(ns_part) :]
                    if ns_part == f"{{{ML_NS}}}":
                        ml_attr_counts[local_attr] += 1
                else:
                    if isinstance(tag, str) and ML_NS in tag:
                        ml_attr_counts[attr_name] += 1

                if attr_name == "labels" or attr_name.endswith("}labels"):
                    label_values[attr_val] += 1

    print(_bold("=== Element Audit ==="))
    for name, count in sorted(ml_element_counts.items(), key=lambda x: (-x[1], x[0])):
        in_schema = name in schema_elements
        status = _green("IN SCHEMA") if in_schema else _red("NOT IN SCHEMA")
        print(f"  ml:{name:<25} {count:>6} occurrences   {status}")

    print()

    print(_bold("=== Attribute Audit ==="))
    for name, count in sorted(ml_attr_counts.items(), key=lambda x: (-x[1], x[0])):
        in_schema = name in schema_attrs
        status = _green("IN SCHEMA") if in_schema else _yellow("NOT IN SCHEMA (lax)")
        print(f"  {name:<30} {count:>6} occurrences   {status}")

    print()

    if label_values:
        print(_bold("=== Label Values ==="))
        for val, count in label_values.most_common():
            print(f"  {val:<30} {count:>6}")

    print()

    total_elements = len(ml_element_counts)
    in_schema = sum(1 for n in ml_element_counts if n in schema_elements)
    missing = total_elements - in_schema
    print(_bold("=== Summary ==="))
    print(f"  Total unique ml: elements:  {total_elements}")
    print(f"  In schema:                  {_green(str(in_schema))}")
    print(f"  Missing from schema:        {_red(str(missing))}")

    if missing > 0:
        print("\n  Missing elements:")
        for name in sorted(ml_element_counts):
            if name not in schema_elements:
                print(f"    ml:{name}")


def _load_schema_elements() -> set:
    elements = set()
    for xsd_file in XSD_DIR.glob("*.xsd"):
        try:
            tree = etree.parse(str(xsd_file))
        except etree.XMLSyntaxError:
            continue
        els = cast("list[etree._Element]", tree.xpath("//xs:element", namespaces={"xs": XS_NS}))
        for el in els:
            name = el.get("name")
            if name:
                elements.add(name)
    return elements


def _load_schema_attributes() -> set:
    attrs = set()
    for xsd_file in XSD_DIR.glob("*.xsd"):
        try:
            tree = etree.parse(str(xsd_file))
        except etree.XMLSyntaxError:
            continue
        els = cast("list[etree._Element]", tree.xpath("//xs:attribute", namespaces={"xs": XS_NS}))
        for el in els:
            name = el.get("name")
            if name:
                attrs.add(name)
    return attrs


# ---------------------------------------------------------------------------
# to-python command
# ---------------------------------------------------------------------------


_ERROR_CHECKS = {"horizontal-overflow", "out-of-bounds", "zero-size"}


def cmd_layout_check(args):
    import json

    from pymcdx import layout

    path = Path(args.file)
    if not path.exists():
        print(f"Error: {path} not found.")
        sys.exit(1)

    summary = layout.summarize(path)
    findings = cast("list[dict[str, str]]", summary["findings"])

    def is_error(check: str) -> bool:
        return check in _ERROR_CHECKS or (args.strict and check == "overlap")

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(_bold(f"=== Layout: {path.name} ==="))
        print(
            f"  {summary['paper']} {summary['orientation']} · "
            f"content {summary['content_width_px']}×{summary['content_height_px']} px · "
            f"{summary['region_count']} regions"
        )
        if not findings:
            print(f"  {_green('OK')} — no layout problems")
        for f in findings:
            colour = _red if is_error(f["check"]) else _yellow
            print(f"  {colour(f['check'])} [{f['region']}]: {f['message']}")

    if any(is_error(f["check"]) for f in findings):
        sys.exit(1)


def cmd_layout_preview(args):
    from pymcdx import layout

    path = Path(args.file)
    if not path.exists():
        print(f"Error: {path} not found.")
        sys.exit(1)

    page = layout.load_page(path)
    regions = layout.load_regions(path, page=page)
    svg = layout.render_svg(page, regions)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(svg, encoding="utf-8")
        print(f"Written to {out_path}", file=sys.stderr)
    else:
        print(svg)


def cmd_render(args):
    from pymcdx.prime_render import PrimeRenderError, render_pages, worksheet_title, write_pdf

    pdf = Path(args.pdf) if args.pdf else None
    try:
        if pdf is not None and pdf.is_dir():
            raise PrimeRenderError(f"PDF output {pdf} is a directory")
        if pdf is not None and (
            pdf.resolve() == Path(args.file).resolve() or (pdf.exists() and pdf.samefile(args.file))
        ):
            raise PrimeRenderError("PDF output must differ from worksheet input")
        result = render_pages(
            Path(args.file),
            Path(args.output),
            dpi=args.dpi,
            timeout=args.timeout,
            resave=Path(args.resave) if args.resave else None,
        )
        if pdf is not None:
            write_pdf(result.pages, pdf, title=worksheet_title(Path(args.file)), dpi=args.dpi)
    except (PrimeRenderError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    for page in result.pages:
        print(page)
    print(f"{len(result.pages)} page(s) rendered by Mathcad Prime", file=sys.stderr)
    if pdf is not None:
        print(pdf)
        print(
            f"PDF written to {pdf}: {len(result.pages)} raster page(s), text is not selectable",
            file=sys.stderr,
        )


def cmd_render_diff(args):
    from pymcdx.render_compare import RenderCompareError, compare_renders

    try:
        report = compare_renders(
            Path(args.old), Path(args.new), Path(args.output), threshold=args.threshold
        )
    except (RenderCompareError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    for page in report.pages:
        print(page.describe())
    if report.changed:
        sys.exit(1)


def _figure_manifest(manifest: Path) -> None:
    """Regenerate every figure in a manifest; any failure exits non-zero after all run."""
    from pymcdx.figures import ManifestError, run_manifest

    try:
        outcomes = run_manifest(manifest)
    except ManifestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    failed = 0
    for outcome in outcomes:
        if outcome.result is not None:
            result = outcome.result
            print(f"{result.path} ({result.width_px} x {result.height_px} px)")
        else:
            failed += 1
            print(
                f"Error: entry {outcome.entry.index} ({outcome.entry.output}): {outcome.error}",
                file=sys.stderr,
            )
    print(f"{len(outcomes) - failed} of {len(outcomes)} figure(s) written", file=sys.stderr)
    if failed:
        sys.exit(1)


def cmd_figure(args):
    from pymcdx.figures import FigureError, prepare_figure

    if args.manifest:
        _figure_manifest(Path(args.manifest))
        return
    try:
        result = prepare_figure(
            Path(args.source),
            Path(args.output),
            page=args.page,
            crop=tuple(args.crop) if args.crop else None,
            dpi=args.dpi,
            trim=not args.no_trim,
            pad=args.pad,
            grid=args.grid,
        )
    except FigureError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    density = f"{result.dpi:g} dpi" if result.dpi else "no DPI (placed at 120 dpi)"
    mm = 25.4 / 96
    print(result.path)
    if args.grid:
        unit = "mm (10 mm lines)" if Path(args.source).suffix.lower() == ".pdf" else "px"
        print(f"crop grid in {unit}; read --crop X0 Y0 X1 Y1 off it", file=sys.stderr)
        return
    print(
        f"{result.width_px} x {result.height_px} px, {density}; an image block places it at "
        f"{result.display_width_px * mm:.0f} x {result.display_height_px * mm:.0f} mm "
        "(then fits it to the column and page)",
        file=sys.stderr,
    )


def cmd_import_yaml(args):
    from pymcdx.authoring_import import WorksheetImportError, import_worksheet

    try:
        result = import_worksheet(Path(args.input), Path(args.output), force=args.force)
    except WorksheetImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(result.output)
    for image in result.images:
        print(image)
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    for region in result.unsupported:
        print(f"Unsupported: {region.describe()} (placeholder written)", file=sys.stderr)
    print(
        f"{len(result.document.blocks)} block(s) imported, "
        f"{len(result.unsupported)} unsupported region(s) reported",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# MCP command
# ---------------------------------------------------------------------------


def cmd_build(args) -> None:
    """Build a semantic worksheet without a bundled numerical evaluator."""
    from pymcdx.authoring import AuthoringBuildError, build_authoring_document

    try:
        report = build_authoring_document(
            Path(args.input),
            Path(args.output),
            report_path=Path(args.report) if args.report else None,
            preview_path=Path(args.preview) if args.preview else None,
        )
    except AuthoringBuildError as exc:
        print(json.dumps({"error": exc.as_dict()}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(report.to_json())


def cmd_mcp(_args) -> None:
    from pymcdx.worksheet_mcp import main

    main()


def make_parser():
    parser = argparse.ArgumentParser(
        prog="pymcdx",
        description="Inspect, extract, audit, and generate Mathcad Prime .mcdx files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # --- generate ---
    gen_p = subparsers.add_parser("generate", help="Convert Python source to .mcdx")
    gen_p.add_argument("input", help="Input .py file")
    gen_p.add_argument("output", help="Output .mcdx file")

    # --- build ---
    build_p = subparsers.add_parser(
        "build", help="Build a semantic YAML document into a Mathcad worksheet"
    )
    build_p.add_argument("input", help="Input semantic YAML document")
    build_p.add_argument("output", help="Output .mcdx file")
    build_p.add_argument("--report", type=str, help="Write the deterministic build report as JSON")
    build_p.add_argument("--preview", type=str, help="Write a Mathcad-free SVG geometry preview")
    # --- inspect ---
    ins_p = subparsers.add_parser("inspect", help="Pretty-print .mcdx or XML contents")
    ins_p.add_argument("file", help="Input .mcdx or .xml file")
    ins_p.add_argument(
        "--list-parts", action="store_true", help="List all parts in an .mcdx package"
    )
    ins_p.add_argument("--part", type=str, help="Pretty-print a specific part")
    ins_p.add_argument(
        "--rels", action="store_true", help="Show package and worksheet relationships"
    )
    ins_p.add_argument(
        "--summary", action="store_true", help="Summary: count regions, elements, unique tags"
    )
    ins_p.add_argument(
        "--outline", action="store_true", help="Structured hierarchical outline of the worksheet"
    )
    ins_p.add_argument(
        "--formatting",
        action="store_true",
        help="Text-first formatting view with stored boxes, XAML styles, and inline math",
    )
    ins_p.add_argument(
        "--full",
        action="store_true",
        help="With --outline/--formatting: never truncate long expressions or text with '...'",
    )
    ins_p.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero if any lossy fallback (e.g. raw dump of non-XML content) occurred",
    )
    ins_p.add_argument("-o", "--output", type=str, help="Write output to file instead of stdout")

    # --- extract ---
    ext_p = subparsers.add_parser("extract", help="Extract specific regions from worksheet XML")
    ext_p.add_argument("file", help="Input .mcdx or .xml file")
    ext_p.add_argument("--region", type=int, default=None, help="Extract region by region-id")
    ext_p.add_argument(
        "--contains", type=str, help="Extract regions containing an element (e.g. ml:for)"
    )
    ext_p.add_argument("--grep", type=str, help="Extract regions matching a regex pattern")
    ext_p.add_argument("--first", type=int, help="Extract first N regions")
    ext_p.add_argument("-o", "--output", type=str, help="Write extracted XML to file")

    # --- audit ---
    aud_p = subparsers.add_parser("audit", help="Audit XML against XSD schemas")
    aud_p.add_argument("file", help="Input .mcdx, .xml file, or directory")

    # --- layout-check ---
    lc_p = subparsers.add_parser(
        "layout-check", help="Lint worksheet layout (overlaps, overflow, out-of-bounds)"
    )
    lc_p.add_argument("file", help="Input .mcdx file")
    lc_p.add_argument("--json", action="store_true", help="Emit the layout summary as JSON")
    lc_p.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on region overlaps (real Mathcad sheets often overlap boxes)",
    )

    # --- layout-preview ---
    lp_p = subparsers.add_parser(
        "layout-preview", help="Render the worksheet layout geometry as an SVG"
    )
    lp_p.add_argument("file", help="Input .mcdx file")
    lp_p.add_argument("-o", "--output", type=str, help="Write SVG to file instead of stdout")

    # --- render ---
    rn_p = subparsers.add_parser(
        "render", help="Render every page through Mathcad Prime to PNG (needs Windows/WSL)"
    )
    rn_p.add_argument("file", help="Input .mcdx file")
    rn_p.add_argument("-o", "--output", required=True, help="Directory for page-NN.png files")
    rn_p.add_argument("--dpi", type=int, default=110, help="Page image resolution (default 110)")
    rn_p.add_argument("--pdf", metavar="OUT.pdf", help="Also assemble the pages into a raster PDF")
    rn_p.add_argument("--timeout", type=float, default=300, help="Maximum worker seconds (0, 3600]")
    rn_p.add_argument(
        "--resave", metavar="OUT.mcdx", help="Also save a separate Prime-resaved worksheet"
    )

    # --- render-diff ---
    rd_p = subparsers.add_parser(
        "render-diff", help="Compare two page-PNG render directories and write diff images"
    )
    rd_p.add_argument("old", help="Directory of page-NN.png from the earlier render")
    rd_p.add_argument("new", help="Directory of page-NN.png from the later render")
    rd_p.add_argument("-o", "--output", required=True, help="Directory for diff page images")
    rd_p.add_argument(
        "--threshold",
        type=float,
        default=0.0,
        help="Fraction of a page's pixels allowed to change before it is reported (default 0)",
    )

    # --- figure ---
    fg_p = subparsers.add_parser(
        "figure", help="Crop a PDF page or image into a tight PNG for an image block"
    )
    fg_p.add_argument("source", nargs="?", help="PDF, PNG, JPEG, BMP, GIF, TIFF or WebP file")
    fg_p.add_argument("-o", "--output", help="Output .png (e.g. figures/x.png)")
    fg_p.add_argument(
        "--manifest",
        metavar="FILE",
        help="Regenerate every figure listed in a *.figures.yaml manifest instead",
    )
    fg_p.add_argument("--page", type=int, default=1, help="PDF page, 1-based (default 1)")
    fg_p.add_argument(
        "--crop",
        type=float,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        help="Region from the top-left: millimetres for a PDF, pixels for an image",
    )
    fg_p.add_argument(
        "--dpi", type=float, default=192.0, help="PDF render resolution (default 192)"
    )
    fg_p.add_argument("--no-trim", action="store_true", help="Keep the white border")
    fg_p.add_argument(
        "--pad", type=int, default=8, help="White padding after trimming, in px (default 8)"
    )
    fg_p.add_argument(
        "--grid",
        action="store_true",
        help="Write the page with a labelled crop grid (mm for PDF, px for images) instead",
    )

    # --- import-yaml ---
    iy_p = subparsers.add_parser(
        "import-yaml", help="Convert a .mcdx worksheet back into semantic YAML"
    )
    iy_p.add_argument("input", help="Input .mcdx worksheet")
    iy_p.add_argument("-o", "--output", required=True, help="Output semantic YAML file")
    iy_p.add_argument(
        "--force", action="store_true", help="Overwrite an existing YAML file or extracted image"
    )

    # --- mcp ---
    subparsers.add_parser("mcp", help="Run the worksheet MCP server over stdio")

    return parser, subparsers


def run_cli(parser, args):
    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "build":
        cmd_build(args)
    elif args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "layout-check":
        cmd_layout_check(args)
    elif args.command == "layout-preview":
        cmd_layout_preview(args)
    elif args.command == "render":
        cmd_render(args)
    elif args.command == "render-diff":
        cmd_render_diff(args)
    elif args.command == "figure":
        if bool(args.manifest) == bool(args.source or args.output):
            parser.error("give either SOURCE -o OUTPUT or --manifest FILE")
        if not args.manifest and not (args.source and args.output):
            parser.error("SOURCE and -o OUTPUT are both required")
        cmd_figure(args)
    elif args.command == "import-yaml":
        cmd_import_yaml(args)
    elif args.command == "mcp":
        cmd_mcp(args)


def configure_output() -> None:
    """Keep CLI output Unicode-safe when Windows redirects it through a code page."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def main():
    configure_output()
    parser, _ = make_parser()
    run_cli(parser, parser.parse_args())


if __name__ == "__main__":
    main()
