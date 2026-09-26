"""Import a Mathcad Prime ``.mcdx`` worksheet back into semantic YAML.

The reverse of ``pymcdx build``: each top-level region becomes one semantic
block, Math50 becomes the Python-shaped expressions ``ast_converter`` reads,
and pictures are extracted as PNG files beside the YAML.  Every conversion is
checked by lowering the result again and comparing it with the stored Math50;
anything that does not survive that check, and any region kind the semantic
format cannot hold, is reported and replaced by a visible placeholder rather
than dropped.  See ``contract-py2m-import``.
"""

from __future__ import annotations

import ast
import io
import keyword
import math
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml  # type: ignore[reportMissingTypeStubs]
from lxml import etree
from PIL import Image, UnidentifiedImageError

from pymcdx.ast_converter import (
    COMPOUND_UNITS,
    GREEK_TO_UNICODE,
    MATH_BUILTINS_FUNCTION,
    UNIT_NAMES,
)
from pymcdx.authoring_functions import FunctionSourceError, render_function_block
from pymcdx.authoring_images import PNG_SIGNATURE
from pymcdx.authoring_io import AuthoringDocumentError, parse_document
from pymcdx.authoring_layout import GRID_PX, PX_PER_MM, PX_PER_PT, spaced_runs
from pymcdx.authoring_models import (
    AuthoringDocument,
    DocumentMetadata,
    InlineMathRun,
    StyleToken,
    TextRun,
    profile_styles,
)
from pymcdx.authoring_render import (
    RenderError,
    _expression_element,  # pyright: ignore[reportPrivateUsage]
    _inline_expression_element,  # pyright: ignore[reportPrivateUsage]
    _with_display_unit,  # pyright: ignore[reportPrivateUsage]
)
from pymcdx.opc import OpcPackageReader

ML_NS = "http://schemas.mathsoft.com/math50"
_WORKSHEET_PART = "/mathcad/worksheet.xml"
_HEADER_PART = "/mathcad/header.xml"
_PRESENTATION_PART = "/mathcad/settings/presentation.xml"
_BODY_STYLE = "body"
_FIGURE_CAPTION = re.compile(r"^Figure \d+: (?P<caption>.+)$", re.DOTALL)
_HEADING_LABEL = re.compile(r"^(?P<label>\d+(?:\.\d+)*)\.? (?P<text>.+)$", re.DOTALL)
_TITLE_LABELS = {
    "Project:": "project",
    "Title:": "title",
    "Calc No.:": "calc_number",
    "Date:": "date",
    "By:": "author",
    "Checked:": "checked_by",
    "Approved:": "approved_by",
}

YamlValue = str | int | float | bool | list["YamlValue"] | dict[str, "YamlValue"] | None
YamlMap = dict[str, YamlValue]


class WorksheetImportError(ValueError):
    """The input cannot be imported, or the output must not be written."""


class _Unrepresentable(ValueError):
    """One piece of Math50 has no exact semantic-YAML spelling."""


@dataclass(frozen=True, slots=True)
class UnsupportedRegion:
    """A region the import could not represent, kept as a placeholder block."""

    region_id: str
    kind: str
    reason: str

    def describe(self) -> str:
        return f"region {self.region_id} ({self.kind}): {self.reason}"


@dataclass(frozen=True, slots=True)
class ImportResult:
    """The written YAML document, its extracted images, and what did not map."""

    output: Path
    document: AuthoringDocument
    images: tuple[Path, ...]
    unsupported: tuple[UnsupportedRegion, ...]
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Math50 -> Python-shaped expressions
# ---------------------------------------------------------------------------

_P_IFEXP = 1
_P_OR = 2
_P_AND = 3
_P_COMPARE = 5
_P_ADD = 10
_P_MUL = 11
_P_UNARY = 12
_P_POW = 13
_P_ATOM = 15

_ARITHMETIC = {
    "plus": ("+", _P_ADD),
    "minus": ("-", _P_ADD),
    "mult": ("*", _P_MUL),
    "div": ("/", _P_MUL),
    "mod": ("%", _P_MUL),
}
_COMPARISONS = {
    "equal": "==",
    "notEqual": "!=",
    "lessThan": "<",
    "greaterThan": ">",
    "lessOrEqual": "<=",
    "greaterOrEqual": ">=",
}
_BOOLEANS = {"and": _P_AND, "or": _P_OR}
_ASCII_GREEK = {symbol: name for name, symbol in GREEK_TO_UNICODE.items() if name.islower()}
_SPECIAL_NAMES = frozenset(
    {"step", "cast", "Q_", "Physical", "sqrt", "abs", "range", "sum", "pi"}
    | UNIT_NAMES
    | set(COMPOUND_UNITS)
)
_UNIT_TOKEN = re.compile(r"^[A-Za-z_]+$")
_INT_TEXT = re.compile(r"^-?\d+$")


def _local(element: etree._Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(element: etree._Element) -> list[etree._Element]:
    return [child for child in element if isinstance(child.tag, str)]


def _arguments(apply: etree._Element) -> list[etree._Element]:
    """An apply's operands, with Prime's ``ml:sequence`` argument wrapper opened."""

    operands = _children(apply)[1:]
    if len(operands) == 1 and _local(operands[0]) == "sequence":
        return _children(operands[0])
    return operands


def _id_parts(element: etree._Element) -> tuple[str, str | None]:
    """Base text and optional subscript of an ``ml:id``."""

    spans = _children(element)
    if not spans:
        return (element.text or "").strip(), None
    if len(spans) != 1 or _local(spans[0]) != "Span" or (element.text or "").strip():
        raise _Unrepresentable("identifier with unexpected inline markup")
    span = spans[0]
    marks = _children(span)
    if not marks:
        return (span.text or "").strip(), None
    if len(marks) != 1 or _local(marks[0]) != "Subscript" or (marks[0].tail or "").strip():
        raise _Unrepresentable("identifier with more than one subscript")
    subscript = (marks[0].text or "").strip()
    return (span.text or "").strip(), subscript or None


def _python_name(element: etree._Element) -> str:
    """The Python name that ``create_variable_id`` lowers to this identifier."""

    base, subscript = _id_parts(element)
    if not base:
        raise _Unrepresentable("empty identifier")
    ascii_base = _ASCII_GREEK.get(base, base)
    name = ascii_base if subscript is None else f"{ascii_base}_{subscript}"
    if subscript is None and (keyword.iskeyword(name) or name in _SPECIAL_NAMES):
        name = base
    if name.startswith("bar_"):
        name = f"bar_{name}"
    if (
        not name.isidentifier()
        or keyword.iskeyword(name)
        or unicodedata.normalize("NFKC", name) != name
    ):
        raise _Unrepresentable(f"identifier {base!r} has no Python spelling")
    return name


def _unit_name(element: etree._Element) -> str:
    base, subscript = _id_parts(element)
    if subscript is not None or base not in UNIT_NAMES or base in COMPOUND_UNITS:
        raise _Unrepresentable(f"unit {base!r} is not a bare semantic unit name")
    return base


def _unit_string(element: etree._Element) -> str:
    """Inverse of ``parse_unit_string`` for the ``Q_(value, "unit")`` spelling."""

    tag = _local(element)
    if tag == "id" and element.get("labels") == "UNIT":
        base, subscript = _id_parts(element)
        if subscript is None and _UNIT_TOKEN.match(base):
            return base
    elif tag == "apply":
        operator = _local(_children(element)[0])
        operands = _arguments(element)
        if len(operands) == 2:
            left, right = operands
            if operator == "pow" and _local(left) == "id" and _local(right) == "real":
                exponent = (right.text or "").strip()
                if _INT_TEXT.match(exponent):
                    return f"{_unit_string(left)}^{exponent}"
            elif operator == "div":
                numerator = _unit_string(left)
                if "/" not in numerator:
                    return f"{numerator}/{_unit_string(right)}"
            elif operator == "mult":
                first, second = _unit_string(left), _unit_string(right)
                if "*" not in first and "/" not in first + second:
                    return f"{first}*{second}"
    raise _Unrepresentable("unit expression has no Q_() spelling")


def _paren(text: str, precedence: int, required: int) -> str:
    return f"({text})" if precedence < required else text


def _real(element: etree._Element) -> tuple[str, int]:
    text = (element.text or "").strip()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        raise _Unrepresentable(f"number {text!r} has no Python spelling") from None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Unrepresentable(f"number {text!r} has no Python spelling")
    return text, _P_UNARY if text.startswith("-") else _P_ATOM


def _call(name: str, operands: Sequence[etree._Element]) -> tuple[str, int]:
    return f"{name}({', '.join(expression(operand) for operand in operands)})", _P_ATOM


def _range_arguments(element: etree._Element) -> str:
    """Python ``range`` arguments whose lowering is this inclusive Math50 range."""

    bounds = _children(element)
    if len(bounds) != 2:
        raise _Unrepresentable("stepped range")
    start, last = bounds
    last_text = (last.text or "").strip() if _local(last) == "real" else ""
    if _INT_TEXT.match(last_text):
        stop = str(int(last_text) + 1)
    elif (
        _local(last) == "apply"
        and _local(_children(last)[0]) == "minus"
        and len(_arguments(last)) == 2
        and _local(_arguments(last)[1]) == "real"
        and (_arguments(last)[1].text or "").strip() == "1"
        and not _folds_in_range(_arguments(last)[0])
    ):
        stop = expression(_arguments(last)[0])
    else:
        text, precedence = _expr(last)
        stop = f"{_paren(text, precedence, _P_ADD)} + 1"
    if _local(start) == "real" and (start.text or "").strip() == "0":
        return stop
    return f"{expression(start)}, {stop}"


def _folds_in_range(stop: etree._Element) -> bool:
    """Whether ``_range_last`` would fold this stop instead of subtracting one."""

    if _local(stop) == "real":
        return bool(_INT_TEXT.match((stop.text or "").strip()))
    if _local(stop) == "apply" and _local(_children(stop)[0]) == "plus":
        operands = _arguments(stop)
        return (
            len(operands) == 2
            and _local(operands[1]) == "real"
            and (operands[1].text or "").strip() == "1"
        )
    return False


def _sum(operands: Sequence[etree._Element]) -> tuple[str, int]:
    """``sum(body for i in range(...))`` from Σ over a lambda and two bounds."""

    if len(operands) != 3 or _local(operands[0]) != "lambda":
        raise _Unrepresentable("summation form")
    parts = _children(operands[0])
    if len(parts) != 2 or _local(parts[0]) != "boundVars" or len(_children(parts[0])) != 1:
        raise _Unrepresentable("summation form")
    variable = _python_name(_children(parts[0])[0])
    upper = operands[2]
    upper_operands = _arguments(upper) if _local(upper) == "apply" else []
    if (
        _local(upper) != "apply"
        or _local(_children(upper)[0]) != "minus"
        or len(upper_operands) != 2
        or (upper_operands[1].text or "").strip() != "1"
    ):
        raise _Unrepresentable("summation bound")
    start = operands[1]
    stop = expression(upper_operands[0])
    bounds = (
        stop
        if _local(start) == "real" and (start.text or "").strip() == "0"
        else f"{expression(start)}, {stop}"
    )
    return f"sum({expression(parts[1])} for {variable} in range({bounds}))", _P_ATOM


def _apply(element: etree._Element) -> tuple[str, int]:
    head = _children(element)[0]
    operator = _local(head)
    operands = _arguments(element)
    if operator in _ARITHMETIC and len(operands) == 2:
        symbol, precedence = _ARITHMETIC[operator]
        left, right = operands
        if operator == "mult":
            try:
                _expr(right)
            except _Unrepresentable:
                return f'Q_({expression(left)}, "{_unit_string(right)}")', _P_ATOM
        left_text, left_precedence = _expr(left)
        right_text, right_precedence = _expr(right)
        return (
            f"{_paren(left_text, left_precedence, precedence)} {symbol} "
            f"{_paren(right_text, right_precedence, precedence + 1)}",
            precedence,
        )
    if operator == "pow" and len(operands) == 2:
        base_text, base_precedence = _expr(operands[0])
        power_text, power_precedence = _expr(operands[1])
        return (
            f"{_paren(base_text, base_precedence, _P_ATOM)} ** "
            f"{_paren(power_text, power_precedence, _P_UNARY)}",
            _P_POW,
        )
    if operator in _COMPARISONS and len(operands) == 2:
        left_text, left_precedence = _expr(operands[0])
        right_text, right_precedence = _expr(operands[1])
        return (
            f"{_paren(left_text, left_precedence, _P_COMPARE + 1)} {_COMPARISONS[operator]} "
            f"{_paren(right_text, right_precedence, _P_COMPARE + 1)}",
            _P_COMPARE,
        )
    if operator in _BOOLEANS and len(operands) == 2:
        precedence = _BOOLEANS[operator]
        left_text, left_precedence = _expr(operands[0])
        right_text, right_precedence = _expr(operands[1])
        return (
            f"{_paren(left_text, left_precedence, precedence)} {operator} "
            f"{_paren(right_text, right_precedence, precedence + 1)}",
            precedence,
        )
    if operator == "neg" and len(operands) == 1:
        text, precedence = _expr(operands[0])
        return f"-{_paren(text, precedence, _P_UNARY)}", _P_UNARY
    if operator == "absval" and len(operands) == 1:
        return _call("abs", operands)
    if operator == "sqrt" and len(operands) == 1:
        return _call("sqrt", operands)
    if operator == "nthRoot" and len(operands) == 2:
        # Index first; Prime saves a square root with a placeholder index.
        index, radicand = operands
        if _local(index) == "placeholder":
            return _call("sqrt", [radicand])
        return _call("root", [radicand, index])
    if operator == "transpose" and len(operands) == 1:
        text, precedence = _expr(operands[0])
        return f"{_paren(text, precedence, _P_ATOM)}.T", _P_ATOM
    if operator == "indexer" and len(operands) == 2:
        text, precedence = _expr(operands[0])
        return f"{_paren(text, precedence, _P_ATOM)}[{expression(operands[1])}]", _P_ATOM
    if operator == "sum":
        return _sum(operands)
    if operator == "id":
        base, subscript = _id_parts(head)
        if (
            base == "floor"
            and subscript is None
            and head.get("labels") == "FUNCTION"
            and len(operands) == 1
            and _local(operands[0]) == "apply"
            and _local(_children(operands[0])[0]) == "div"
        ):
            left, right = _arguments(operands[0])
            left_text, left_precedence = _expr(left)
            right_text, right_precedence = _expr(right)
            return (
                f"{_paren(left_text, left_precedence, _P_MUL)} // "
                f"{_paren(right_text, right_precedence, _P_MUL + 1)}",
                _P_MUL,
            )
        if (
            base == "mod"
            and subscript is None
            and head.get("labels") == "FUNCTION"
            and len(operands) == 2
        ):
            # a % b lowers to Mathcad's mod builtin (its ml:sequence is opened above).
            left, right = operands
            left_text, left_precedence = _expr(left)
            right_text, right_precedence = _expr(right)
            return (
                f"{_paren(left_text, left_precedence, _P_MUL)} % "
                f"{_paren(right_text, right_precedence, _P_MUL + 1)}",
                _P_MUL,
            )
        if subscript is None and base in MATH_BUILTINS_FUNCTION:
            return _call(base, operands)
        return _call(_python_name(head), operands)
    raise _Unrepresentable(f"Math50 operator {operator!r}")


def _branch_value(branch: etree._Element) -> etree._Element:
    parts = _children(branch)
    if len(parts) == 1 and _local(parts[0]) == "program" and len(_children(parts[0])) == 1:
        return _children(parts[0])[0]
    if len(parts) == 1 and _local(parts[0]) != "program":
        return parts[0]
    raise _Unrepresentable("if branch with statements")


def _if_expression(element: etree._Element) -> tuple[str, int]:
    parts = _children(element)
    if [_local(part) for part in parts] != ["test", "then", "else"]:
        raise _Unrepresentable("if expression without exactly one else")
    tests = _children(parts[0])
    if len(tests) != 1:
        raise _Unrepresentable("if test")
    body_text, body_precedence = _expr(_branch_value(parts[1]))
    test_text, test_precedence = _expr(tests[0])
    else_text, else_precedence = _expr(_branch_value(parts[2]))
    return (
        f"{_paren(body_text, body_precedence, _P_OR)} if "
        f"{_paren(test_text, test_precedence, _P_OR)} else "
        f"{_paren(else_text, else_precedence, _P_IFEXP)}",
        _P_IFEXP,
    )


def _matrix(element: etree._Element) -> tuple[str, int]:
    cells = [expression(cell) for cell in _children(element)]
    try:
        rows, columns = int(element.get("rows", "")), int(element.get("cols", ""))
    except ValueError:
        raise _Unrepresentable("matrix without a size") from None
    if rows * columns != len(cells) or not cells:
        raise _Unrepresentable("matrix cell count")
    if columns == 1:
        return f"[{', '.join(cells)}]", _P_ATOM
    lines = (f"[{', '.join(cells[row * columns : (row + 1) * columns])}]" for row in range(rows))
    return f"[{', '.join(lines)}]", _P_ATOM


def _expr(element: etree._Element) -> tuple[str, int]:
    tag = _local(element)
    if tag == "real":
        return _real(element)
    if tag == "str":
        return repr(element.text or ""), _P_ATOM
    if tag == "id":
        labels = element.get("labels")
        if labels == "UNIT":
            return _unit_name(element), _P_ATOM
        if labels == "CONSTANT":
            base, subscript = _id_parts(element)
            if base == GREEK_TO_UNICODE["pi"] and subscript is None:
                return "pi", _P_ATOM
            raise _Unrepresentable(f"built-in constant {base!r}")
        return _python_name(element), _P_ATOM
    if tag == "apply" and _children(element):
        return _apply(element)
    if tag == "if":
        return _if_expression(element)
    if tag == "matrix":
        return _matrix(element)
    if tag == "parens" and len(_children(element)) == 1:
        return f"({expression(_children(element)[0])})", _P_ATOM
    raise _Unrepresentable(f"Math50 element {tag!r}")


def expression(element: etree._Element) -> str:
    """Spell one Math50 expression the way ``ast_converter`` reads it back."""

    return _expr(element)[0]


def _target(element: etree._Element) -> str:
    if _local(element) == "id":
        return _python_name(element)
    if _local(element) == "apply" and _local(_children(element)[0]) == "indexer":
        return expression(element)
    raise _Unrepresentable("definition target")


def _display_unit(unit_override: etree._Element | None) -> str | None:
    if unit_override is None:
        return None
    parts = _children(unit_override)
    if not parts or _local(parts[0]) == "placeholder":
        return None
    return expression(parts[0]).replace(" ", "")


def _evaluation(element: etree._Element) -> tuple[str, str | None]:
    """The evaluated expression and display unit of an ``ml:eval``."""

    parts = [
        part for part in _children(element) if _local(part) not in {"resultFormat", "resultData"}
    ]
    values = [part for part in parts if _local(part) != "unitOverride"]
    overrides = [part for part in parts if _local(part) == "unitOverride"]
    if len(values) != 1 or len(overrides) > 1:
        raise _Unrepresentable("evaluation form")
    return expression(values[0]), _display_unit(overrides[0] if overrides else None)


# ---------------------------------------------------------------------------
# Function (program) regions -> Python def
# ---------------------------------------------------------------------------


def _program_lines(program: etree._Element, depth: int, *, tail: bool) -> list[str]:
    if _local(program) != "program" or not _children(program):
        raise _Unrepresentable("program body")
    pad = "    " * depth
    lines: list[str] = []
    statements = _children(program)
    for index, statement in enumerate(statements):
        last = index == len(statements) - 1
        tag = _local(statement)
        parts = _children(statement)
        if tag == "localDefine" and len(parts) == 2:
            lines.append(f"{pad}{_target(parts[0])} = {expression(parts[1])}")
        elif tag == "return" and len(parts) == 1:
            lines.append(f"{pad}return {expression(parts[0])}")
        elif tag == "if":
            lines.extend(_if_lines(statement, depth, tail=tail and last))
        elif tag == "for" and len(parts) == 3 and _local(parts[1]) == "range":
            lines.append(
                f"{pad}for {_python_name(parts[0])} in range({_range_arguments(parts[1])}):"
            )
            lines.extend(_program_lines(parts[2], depth + 1, tail=False))
        elif tag == "while" and len(parts) == 2:
            lines.append(f"{pad}while {expression(parts[0])}:")
            lines.extend(_program_lines(parts[1], depth + 1, tail=False))
        elif tail and last:
            lines.append(f"{pad}return {expression(statement)}")
        else:
            raise _Unrepresentable(f"program statement {tag!r}")
    return lines


def _if_lines(element: etree._Element, depth: int, *, tail: bool) -> list[str]:
    pad = "    " * depth
    lines: list[str] = []

    def branch(keyword_text: str, test: etree._Element | None, body: etree._Element) -> None:
        bodies = _children(body)
        if len(bodies) != 1:
            raise _Unrepresentable("if branch")
        if test is None:
            lines.append(f"{pad}{keyword_text}:")
        else:
            tests = _children(test)
            if len(tests) != 1:
                raise _Unrepresentable("if test")
            lines.append(f"{pad}{keyword_text} {expression(tests[0])}:")
        lines.extend(_program_lines(bodies[0], depth + 1, tail=tail))

    parts = _children(element)
    if len(parts) < 2 or _local(parts[0]) != "test" or _local(parts[1]) != "then":
        raise _Unrepresentable("if statement")
    branch("if", parts[0], parts[1])
    for part in parts[2:]:
        tag = _local(part)
        inner = _children(part)
        if tag == "elseif" and [_local(p) for p in inner] == ["test", "then"]:
            branch("elif", inner[0], inner[1])
        elif tag == "else":
            branch("else", None, part)
        else:
            raise _Unrepresentable(f"if clause {tag!r}")
    return lines


def function_source(define: etree._Element) -> str:
    """The Python ``def`` whose function-block lowering is this definition."""

    parts = _children(define)
    signature = _children(parts[0])
    if not signature or _local(signature[0]) != "id" or len(signature) > 2:
        raise _Unrepresentable("function signature")
    params: list[str] = []
    if len(signature) == 2:
        if _local(signature[1]) != "boundVars":
            raise _Unrepresentable("function signature")
        params = [_python_name(param) for param in _children(signature[1])]
    header = f"def {_python_name(signature[0])}({', '.join(params)}):"
    body = parts[1]
    if _local(body) == "program":
        lines = _program_lines(body, 1, tail=True)
    else:
        lines = [f"    return {expression(body)}"]
    return "\n".join([header, *lines]) + "\n"


# ---------------------------------------------------------------------------
# Round-trip self-check
# ---------------------------------------------------------------------------


def _label_class(element: etree._Element) -> str:
    labels = element.get("labels")
    return labels if labels in {"UNIT", "CONSTANT"} else "NAME"


def _normalised(element: etree._Element) -> tuple[object, ...]:
    """A comparison key that ignores label bookkeeping and Prime's wrappers."""

    tag = _local(element)
    if tag == "id":
        return ("id", _label_class(element), _id_parts(element))
    if tag == "real":
        text = (element.text or "").strip()
        try:
            return ("real", float(text))
        except ValueError:
            return ("real", text)
    if tag == "str":
        return ("str", element.text or "")
    children: list[etree._Element] = []
    for child in _children(element):
        if _local(child) in {"resultFormat", "resultData"}:
            continue
        if _local(child) == "sequence":
            children.extend(_children(child))
        elif _local(child) == "parens" and len(_children(child)) == 1:
            children.append(_children(child)[0])
        else:
            children.append(child)
    if tag in {"then", "else"} and len(children) == 1 and _local(children[0]) == "program":
        statements = _children(children[0])
        if len(statements) == 1:
            children = statements
    if (
        tag == "apply"
        and len(children) == 3
        and _local(children[0]) == "nthRoot"
        and _local(children[1]) == "placeholder"
    ):
        # Prime's square root (nthRoot with an empty index) is the generator's ml:sqrt.
        return ("apply", (), (("sqrt", (), ()), _normalised(children[2])))
    attributes = tuple(sorted((k, v) for k, v in element.attrib.items() if k in {"rows", "cols"}))
    return (tag, attributes, tuple(_normalised(child) for child in children))


def _same_math(first: etree._Element, second: etree._Element) -> bool:
    return _normalised(first) == _normalised(second)


def _checked(stored: etree._Element, lower: Callable[[], etree._Element]) -> None:
    try:
        rebuilt = lower()
    except (RenderError, FunctionSourceError, SyntaxError, NotImplementedError, ValueError) as exc:
        raise _Unrepresentable(f"re-lowering failed: {exc}") from exc
    if not _same_math(stored, rebuilt):
        raise _Unrepresentable("the Python spelling does not lower back to the same Math50")


# ---------------------------------------------------------------------------
# Region conversion
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Inline:
    """One inline math child of a text region, converted or not."""

    run: YamlMap
    region_id: str
    problem: str | None = None


@dataclass(slots=True)
class _Piece:
    text: str | None
    style: str | None
    attributes: dict[str, str]
    inline: _Inline | None = None


@dataclass(slots=True)
class _Region:
    element: etree._Element
    xml_index: int
    region_id: str
    kind: str
    top: float
    left: float
    width: float | None


@dataclass(slots=True)
class _Context:
    reader: OpcPackageReader
    relationships: dict[str, str]
    grid_px: float
    content_width_px: float
    image_stem: str
    image_dir: Path
    images: list[tuple[Path, bytes]] = field(default_factory=list)
    unsupported: list[UnsupportedRegion] = field(default_factory=list)
    styles: dict[str, YamlMap] = field(default_factory=dict)
    heading_counters: list[int] = field(default_factory=lambda: [0] * 6)


_STYLE_ATTRIBUTES = ("FontFamily", "FontSize", "FontWeight", "FontStyle", "Foreground")
_PROFILE = profile_styles("engineering")


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _part_name(target: str, source_dir: str = "/mathcad") -> str:
    if target.startswith("/"):
        return posixpath.normpath(target)
    return posixpath.normpath(posixpath.join(source_dir, target))


def _read_xaml(context: _Context, text: etree._Element) -> etree._Element:
    target = context.relationships.get(text.get("item-idref", ""))
    if target is None:
        raise _Unrepresentable("text region without its FlowDocument part")
    try:
        package = zipfile.ZipFile(io.BytesIO(context.reader.read_part(_part_name(target))))
        name = next(n for n in sorted(package.namelist()) if n.lower().endswith(".xaml"))
        return etree.fromstring(package.read(name))
    except (KeyError, StopIteration, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        raise _Unrepresentable(f"unreadable FlowDocument: {exc}") from exc


def _symbols(text: str) -> int:
    """WPF text-container length of a string (UTF-16 code units)."""

    return len(text.encode("utf-16-le")) // 2


def _formatting(element: etree._Element) -> dict[str, str]:
    """The font attributes a XAML element sets for its content."""

    return {name: str(element.get(name)) for name in _STYLE_ATTRIBUTES if element.get(name)}


def _paragraphs(
    root: etree._Element, inlines: dict[int, _Inline]
) -> tuple[list[list[_Piece]], list[etree._Element]]:
    """Paragraph pieces in reading order, with each inline math at its caret."""

    paragraphs: list[list[_Piece]] = []
    tables: list[etree._Element] = []
    pending = dict(sorted(inlines.items()))
    position = 0

    def place_up_to(limit: int, pieces: list[_Piece]) -> None:
        for caret in [c for c in pending if c <= limit]:
            pieces.append(_Piece(None, None, {}, pending.pop(caret)))

    def inline(element: etree._Element, pieces: list[_Piece], attributes: dict[str, str]) -> None:
        nonlocal position
        merged = attributes | _formatting(element)
        tag = _local(element)
        if tag == "Run":
            text = element.text or ""
            start = position
            content = start + 1
            place_up_to(start, pieces)
            style = element.get("style") or None
            # Split the run at any caret that falls inside its text.
            offset = 0
            for caret in [c for c in pending if content <= c < content + _symbols(text)]:
                cut = _python_index(text, caret - content)
                if cut > offset:
                    pieces.append(_Piece(text[offset:cut], style, merged))
                pieces.append(_Piece(None, None, {}, pending.pop(caret)))
                offset = cut
            if offset < len(text):
                pieces.append(_Piece(text[offset:], style, merged))
            position = content + _symbols(text) + 1
            return
        if tag == "LineBreak":
            place_up_to(position, pieces)
            position += 2
            pieces.append(_Piece("\n", None, merged))
            return
        position += 1
        for child in _children(element):
            inline(child, pieces, merged)
        position += 1

    def block(element: etree._Element, attributes: dict[str, str]) -> None:
        nonlocal position
        merged = attributes | _formatting(element)
        tag = _local(element)
        if tag == "Paragraph":
            pieces: list[_Piece] = []
            position += 1
            for child in _children(element):
                inline(child, pieces, merged)
            place_up_to(position, pieces)
            position += 1
            paragraphs.append(pieces)
            return
        if tag == "Table":
            tables.append(element)
        position += 1
        for child in _children(element):
            block(child, merged)
        position += 1

    for child in _children(root):
        block(child, _formatting(root))
    if pending:
        if not paragraphs:
            paragraphs.append([])
        paragraphs[-1].extend(_Piece(None, None, {}, run) for run in pending.values())
    return paragraphs, tables


def _python_index(text: str, symbols: int) -> int:
    """The string index that sits ``symbols`` UTF-16 units into ``text``."""

    count = 0
    for index, character in enumerate(text):
        if count >= symbols:
            return index
        count += 2 if ord(character) > 0xFFFF else 1
    return len(text)


def _drop_slots(paragraphs: list[list[_Piece]]) -> None:
    """Remove the one-space Run that reserves each inline math object's slot."""

    for pieces in paragraphs:
        for index, piece in enumerate(pieces):
            if piece.inline is None:
                continue
            following = pieces[index + 1] if index + 1 < len(pieces) else None
            if following is not None and following.inline is None and following.text == " ":
                following.text = ""
        pieces[:] = [p for p in pieces if p.inline is not None or p.text]


def _inline_run(context: _Context, child: etree._Element) -> _Inline:
    region_id = child.get("region-id", "?")
    maths = [part for part in _children(child) if _local(part) == "math"]
    nodes = _children(maths[0]) if maths else []
    if len(nodes) != 1:
        return _placeholder_inline(region_id, "inline region is not a single math expression")
    node = nodes[0]
    try:
        run = _inline_math(node)
        expression_text = cast(str, run["expression"])
        unit = cast("str | None", run.get("unit"))
        evaluate = bool(run.get("evaluate", False))
        _checked(
            node,
            lambda: _with_display_unit(
                _inline_expression_element(expression_text, unit, evaluate=evaluate), unit
            ),
        )
        InlineMathRun.model_validate(run)
    except (_Unrepresentable, ValueError) as exc:
        return _placeholder_inline(region_id, str(exc), node)
    return _Inline(run, region_id)


def _placeholder_inline(region_id: str, reason: str, node: etree._Element | None = None) -> _Inline:
    return _Inline(
        {"kind": "text", "text": _placeholder_text(region_id, "math", node)}, region_id, reason
    )


def _placeholder_text(region_id: str, kind: str, node: etree._Element | None = None) -> str:
    detail = ""
    if node is not None:
        detail = f" <{_local(node)}>"
    return f"[not imported: region {region_id} ({kind}){detail}]"


def _inline_math(node: etree._Element) -> YamlMap:
    tag = _local(node)
    run: YamlMap = {"kind": "inline_math"}
    if tag == "define":
        parts = _children(node)
        if len(parts) != 2 or _local(parts[0]) == "function":
            raise _Unrepresentable("inline function definition")
        target = _target(parts[0])
        if _local(parts[1]) == "eval":
            value, unit = _evaluation(parts[1])
            run["expression"] = f"{target} = {value}"
            _set_result(run, unit)
        else:
            run["expression"] = f"{target} = {expression(parts[1])}"
        return run
    if tag == "eval":
        value, unit = _evaluation(node)
        run["expression"] = value
        _set_result(run, unit)
        return run
    run["expression"] = expression(node)
    return run


def _set_result(run: YamlMap, unit: str | None) -> None:
    if unit is None:
        run["evaluate"] = True
    else:
        run["unit"] = unit


def _indent(context: _Context, region: _Region, style: str | None) -> int:
    grid = context.grid_px
    left = region.left
    token = _PROFILE.get(style or "")
    if token is not None and token.width == "half":
        column = (context.content_width_px - 2.0 * grid) / 2.0 + 2.0 * grid
        if left >= column - grid / 2:
            left -= column
    return max(0, min(4, round(left / grid)))


def _block_style(pieces: Sequence[_Piece], default: str) -> str | None:
    counts = Counter(
        p.style for p in pieces if p.inline is None and p.style and (p.text or "").strip()
    )
    if not counts:
        return None
    style = counts.most_common(1)[0][0]
    return None if style == default else style


def _register_style(context: _Context, style: str | None, attributes: dict[str, str]) -> None:
    """Recreate a document-local style token the profile does not define."""

    if style is None or style in _PROFILE or style in context.styles:
        return
    size_px = _float(attributes.get("FontSize")) or 13.333333333333334
    token: YamlMap = {
        "font_family": attributes.get("FontFamily", "Arial"),
        "font_size_pt": round(size_px / PX_PER_PT, 2),
        "color": attributes.get("Foreground", "#FF000000"),
    }
    if attributes.get("FontWeight") == "Bold":
        token["bold"] = True
    if attributes.get("FontStyle") == "Italic":
        token["italic"] = True
    StyleToken.model_validate(token)
    context.styles[style] = token


def _is_blank(pieces: Sequence[_Piece]) -> bool:
    return all(p.inline is None and not (p.text or "").strip() for p in pieces)


def _model_runs(pieces: Sequence[_Piece]) -> list[TextRun | InlineMathRun]:
    runs: list[TextRun | InlineMathRun] = []
    for piece in pieces:
        if piece.inline is None:
            runs.append(TextRun(kind="text", text=piece.text or ""))
        elif piece.inline.problem is None:
            runs.append(InlineMathRun.model_validate(piece.inline.run))
        else:
            runs.append(TextRun(kind="text", text=cast(str, piece.inline.run["text"])))
    return runs


def _auto_gap(before: Sequence[_Piece], after: Sequence[_Piece]) -> bool:
    """Whether ``spaced_runs`` itself puts a blank line between two paragraphs."""

    runs = [*_model_runs(before), TextRun(kind="text", text="\n"), *_model_runs(after)]
    return any(isinstance(run, TextRun) and run.text == "\n\n" for run in spaced_runs(runs))


def _text_runs(
    context: _Context, paragraphs: list[list[_Piece]], block_style: str | None
) -> list[YamlMap]:
    """Merge pieces into semantic runs, dropping blanks the compiler re-adds."""

    kept: list[tuple[list[_Piece], int]] = []  # content paragraph, blank lines before it
    blanks = 0
    for pieces in paragraphs:
        if _is_blank(pieces):
            blanks += 1
            continue
        if kept and blanks == 1 and _auto_gap(kept[-1][0], pieces):
            blanks = 0
        kept.append((pieces, blanks))
        blanks = 0
    runs: list[YamlMap] = []

    effective = block_style or _BODY_STYLE

    def add_text(text: str, style: str | None) -> None:
        run_style = None if style == effective or style is None else style
        previous = runs[-1] if runs else None
        if (
            previous is not None
            and previous["kind"] == "text"
            and previous.get("style") == run_style
            and not str(previous["text"]).startswith("[not imported")
        ):
            previous["text"] = f"{previous['text']}{text}"
            return
        run: YamlMap = {"kind": "text", "text": text}
        if run_style is not None:
            run["style"] = run_style
        runs.append(run)

    for index, (pieces, blank_lines) in enumerate(kept):
        if index or blank_lines:
            add_text("\n" * (blank_lines + (1 if index else 0)), None)
        for piece in pieces:
            if piece.inline is not None:
                runs.append(dict(piece.inline.run))
                if piece.inline.problem is not None:
                    context.unsupported.append(
                        UnsupportedRegion(piece.inline.region_id, "math", piece.inline.problem)
                    )
                continue
            if piece.style is not None:
                _register_style(context, piece.style, piece.attributes)
            add_text(piece.text or "", piece.style)
    if blanks:
        add_text("\n" * blanks, None)
    return runs


def _heading(context: _Context, region: _Region, paragraphs: list[list[_Piece]]) -> YamlMap | None:
    content = [pieces for pieces in paragraphs if not _is_blank(pieces)]
    if len(content) != 1 or any(p.inline is not None for p in content[0]):
        return None
    styles = {p.style for p in content[0]}
    if len(styles) != 1:
        return None
    style = styles.pop()
    if style not in {"title", "heading1", "heading2", "heading3"}:
        return None
    text = "".join(p.text or "" for p in content[0]).strip()
    if not text:
        return None
    block: YamlMap = {"kind": "heading", "level": 1, "text": text}
    if style == "title":
        block["style"] = "title"
    else:
        level = int(style[-1])
        match = _HEADING_LABEL.match(text)
        if match is not None:
            parts = [int(part) for part in match.group("label").split(".")]
            depth = len(parts)
            counters = context.heading_counters
            expected = [*counters[: depth - 1], counters[depth - 1] + 1]
            if parts == expected and 1 <= depth <= 6 and min(depth, 3) == level:
                counters[depth - 1] += 1
                counters[depth:] = [0] * (6 - depth)
                level = depth
                block["text"] = match.group("text")
        block["level"] = level
    indent = _indent(context, region, None)
    if indent:
        block["indent"] = indent
    return block


def _contents(tables: list[etree._Element], paragraphs: list[list[_Piece]]) -> YamlMap | None:
    table = tables[0]
    rows = [row for row in table.iter() if _local(row) == "TableRow"]
    if any(len([c for c in row if _local(c) == "TableCell"]) != 3 for row in rows):
        return None
    block: YamlMap = {"kind": "contents"}
    captions = ["".join(p.text or "" for p in pieces).strip() for pieces in paragraphs[:1]]
    if captions and captions[0] and captions[0] != "Contents":
        block["title"] = captions[0]
    columns = [c for c in table.iter() if _local(c) == "TableColumn"]
    body_px = float(_PROFILE[_BODY_STYLE].font_size_pt) * PX_PER_PT
    width = _float(columns[0].get("Width")) if columns else None
    if width is not None:
        depth = round((width / body_px - 1.0) / 1.1)
        if 1 <= depth <= 6 and depth != 2:
            block["depth"] = depth
    return block


def _text_region(context: _Context, region: _Region) -> YamlMap:
    text = next(child for child in _children(region.element) if _local(child) == "text")
    root = _read_xaml(context, text)
    inlines: dict[int, _Inline] = {}
    for container in (c for c in _children(text) if _local(c) == "regions"):
        for child in (c for c in _children(container) if _local(c) == "region"):
            caret = child.get("text-caret-position")
            position = int(caret) if caret is not None and caret.isdigit() else 1 << 30
            while position in inlines:
                position += 1
            inlines[position] = _inline_run(context, child)
    paragraphs, tables = _paragraphs(root, inlines)
    _drop_slots(paragraphs)
    if tables:
        contents = _contents(tables, paragraphs)
        if contents is None:
            raise _Unrepresentable("table")
        return contents
    heading = _heading(context, region, paragraphs)
    if heading is not None:
        return heading
    pieces = [p for pieces in paragraphs for p in pieces]
    style = _block_style(pieces, _BODY_STYLE)
    block: YamlMap = {"kind": "text"}
    if style is not None:
        block["style"] = style
        _register_style(context, style, next(p.attributes for p in pieces if p.style == style))
    indent = _indent(context, region, style)
    if indent:
        block["indent"] = indent
    runs = _text_runs(context, paragraphs, style)
    if not runs:
        runs = [{"kind": "text", "text": " "}]
    block["runs"] = cast(YamlValue, runs)
    return block


def _math_region(context: _Context, region: _Region) -> YamlMap:
    maths = [part for part in _children(region.element) if _local(part) == "math"]
    nodes = _children(maths[0]) if maths else []
    if len(nodes) != 1:
        raise _Unrepresentable("math region without exactly one expression")
    node = nodes[0]
    indent = _indent(context, region, None)
    parts = _children(node)
    if _local(node) == "define" and len(parts) == 2 and _local(parts[0]) == "function":
        source = function_source(node)
        _checked(node, lambda: render_function_block(source))
        block: YamlMap = {"kind": "function", "source": source}
    elif (_local(node) == "define" and len(parts) == 2 and _local(parts[1]) == "eval") or (
        _local(node) == "eval" and _display_unit(node.find(f"{{{ML_NS}}}unitOverride"))
    ):
        # Evaluated definitions and display units only exist as inline math.
        inline = _inline_math(node)
        text_expression = cast(str, inline["expression"])
        unit = cast("str | None", inline.get("unit"))
        evaluate = bool(inline.get("evaluate", False))
        _checked(
            node,
            lambda: _with_display_unit(
                _inline_expression_element(text_expression, unit, evaluate=evaluate), unit
            ),
        )
        block = {"kind": "text", "runs": [cast(YamlValue, inline)]}
    else:
        if _local(node) == "define":
            text_expression = f"{_target(parts[0])} = {expression(parts[1])}"
        elif _local(node) == "eval":
            text_expression, _unit = _evaluation(node)
        else:
            text_expression = expression(node)
        _checked(node, lambda: _expression_element(text_expression))
        block = {"kind": "math", "expression": text_expression}
    if indent:
        block["indent"] = indent
    return block


def _picture_region(context: _Context, region: _Region) -> YamlMap:
    picture = next(child for child in _children(region.element) if _local(child) == "picture")
    media = _children(picture)
    if len(media) != 1:
        raise _Unrepresentable("picture without one media element")
    target = context.relationships.get(media[0].get("item-idref", ""))
    if target is None:
        raise _Unrepresentable("picture without its media part")
    try:
        data = context.reader.read_part(_part_name(target))
    except KeyError:
        raise _Unrepresentable(f"missing media part {target}") from None
    if not data.startswith(PNG_SIGNATURE):
        try:
            with Image.open(io.BytesIO(data)) as image:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
        except (UnidentifiedImageError, OSError) as exc:
            raise _Unrepresentable(f"picture media is not a readable image: {exc}") from exc
        data = buffer.getvalue()
    name = f"{context.image_stem}-image-{len(context.images) + 1:02d}.png"
    context.images.append((context.image_dir / name, data))
    block: YamlMap = {"kind": "image", "path": name}
    width = _float(media[0].get("display-width")) or region.width
    if width is not None and width > 0:
        block["width_mm"] = round(width / PX_PER_MM, 2)
    return block


def _region_kind(element: etree._Element) -> str:
    for child in _children(element):
        return _local(child)
    return "empty"


def _regions(root: etree._Element) -> list[_Region]:
    containers = [child for child in _children(root) if _local(child) == "regions"]
    regions: list[_Region] = []
    for container in containers:
        for index, element in enumerate(c for c in _children(container) if _local(c) == "region"):
            regions.append(
                _Region(
                    element=element,
                    xml_index=index,
                    region_id=element.get("region-id", "?"),
                    kind=_region_kind(element),
                    top=_float(element.get("top")) or 0.0,
                    left=_float(element.get("left")) or 0.0,
                    width=_float(element.get("width")),
                )
            )
    # Prime keeps regions in edit order (an added page break lands last), so
    # document order is the stored position, top to bottom then left to right.
    return sorted(regions, key=lambda r: (r.top, r.left, r.xml_index))


def _caption(block: YamlMap) -> str | None:
    runs = block.get("runs")
    if block.get("kind") != "text" or not isinstance(runs, list) or len(runs) != 1:
        return None
    run = runs[0]
    if not isinstance(run, dict) or run.get("kind") != "text":
        return None
    match = _FIGURE_CAPTION.match(str(run.get("text", "")).strip())
    return None if match is None else match.group("caption")


def _blocks(context: _Context, root: etree._Element) -> list[YamlMap]:
    blocks: list[YamlMap] = []
    last_image: YamlMap | None = None
    for region in _regions(root):
        block: YamlMap
        try:
            if region.kind == "text":
                block = _text_region(context, region)
            elif region.kind == "math":
                block = _math_region(context, region)
            elif region.kind == "picture":
                block = _picture_region(context, region)
            elif region.kind == "pageBreak":
                block = {"kind": "page_break"}
            else:
                raise _Unrepresentable("no semantic block kind holds this region")
        except _Unrepresentable as exc:
            context.unsupported.append(UnsupportedRegion(region.region_id, region.kind, str(exc)))
            block = {
                "kind": "text",
                "style": "note",
                "runs": [
                    {"kind": "text", "text": _placeholder_text(region.region_id, region.kind)}
                ],
            }
        caption = _caption(block)
        if last_image is not None and caption is not None and "caption" not in last_image:
            last_image["caption"] = caption
            indent = _indent(context, region, None)
            if indent:
                last_image["indent"] = indent
            last_image = None
            continue
        last_image = block if block["kind"] == "image" else None
        blocks.append(block)
    return blocks


# ---------------------------------------------------------------------------
# Header metadata and page settings
# ---------------------------------------------------------------------------


def _metadata(context: _Context) -> YamlMap:
    """Document metadata recovered from the page-header title block."""

    try:
        header = etree.fromstring(context.reader.read_part(_HEADER_PART))
    except (KeyError, etree.XMLSyntaxError):
        return {}
    relationships = {
        rel.rel_id: rel.target for rel in context.reader.get_relationships(_HEADER_PART)
    }
    header_context = _Context(
        reader=context.reader,
        relationships=relationships,
        grid_px=context.grid_px,
        content_width_px=context.content_width_px,
        image_stem=context.image_stem,
        image_dir=context.image_dir,
    )
    cells: list[tuple[_Region, str, set[str | None]]] = []
    for region in _regions(header):
        if region.kind != "text":
            continue
        text = next(child for child in _children(region.element) if _local(child) == "text")
        try:
            paragraphs, _tables = _paragraphs(_read_xaml(header_context, text), {})
        except _Unrepresentable:
            continue
        pieces = [p for pieces in paragraphs for p in pieces]
        value = " ".join("".join(p.text or "" for p in pieces).split())
        if value:
            cells.append((region, value, {p.style for p in pieces}))
    found: YamlMap = {}
    for label, label_text, _styles in cells:
        key = _TITLE_LABELS.get(label_text)
        if key is None:
            continue
        values = [
            (region.left, text)
            for region, text, _ in cells
            if abs(region.top - label.top) < 1.0 and region.left > label.left
        ]
        if values:
            found[key] = min(values)[1]
    if "title" not in found:
        titles = [text for _region, text, styles in cells if "title" in styles]
        if titles:
            found["title"] = titles[0]
    return {key: found[key] for key in DocumentMetadata.model_fields if key in found}


def _worksheet(reader: OpcPackageReader, warnings: list[str]) -> tuple[YamlMap, float]:
    """Page settings in millimetres and the content width in pixels."""

    left, top, right, bottom = 5.0 * PX_PER_MM, 40.0 * PX_PER_MM, 5.0 * PX_PER_MM, 12.5 * PX_PER_MM
    try:
        presentation = etree.fromstring(reader.read_part(_PRESENTATION_PART))
    except (KeyError, etree.XMLSyntaxError):
        presentation = None
        warnings.append("no page settings part; using the default A4 margins")
    model = None
    if presentation is not None:
        model = next((e for e in presentation.iter() if _local(e) == "pageModel"), None)
    if model is not None:
        margins = [_float(value) for value in model.get("page-margin", "").split(",")]
        if len(margins) == 4 and all(m is not None and m >= 0 for m in margins):
            left, top, right, bottom = cast(list[float], margins)
        if model.get("paper-code", "A4") != "A4" or model.get("orientation", "Portrait") != (
            "Portrait"
        ):
            warnings.append(
                f"page is {model.get('paper-code')} {model.get('orientation')}; "
                "semantic YAML supports A4 portrait only"
            )
        if model.get("grid-size", "Fine") != "Fine":
            warnings.append(f"grid {model.get('grid-size')} imported as the fine grid")

    def millimetres(pixels: float) -> int | float:
        value = round(pixels / PX_PER_MM, 2)
        return int(value) if value == int(value) else value

    settings: YamlMap = {
        "paper": "A4",
        "orientation": "portrait",
        "margins_mm": {
            "left": millimetres(left),
            "top": millimetres(top),
            "right": millimetres(right),
            "bottom": millimetres(bottom),
        },
        "grid": "fine",
    }
    return settings, 210.0 * PX_PER_MM - left - right


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):  # type: ignore[misc]
    """Safe dumper with flow-style short runs and literal-block function source."""


class _Flow(dict[str, YamlValue]):
    pass


class _Literal(str):
    __slots__ = ()


def _represent_flow(dumper: yaml.SafeDumper, data: _Flow) -> yaml.Node:  # type: ignore[name-defined]
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=True)


def _represent_literal(dumper: yaml.SafeDumper, data: _Literal) -> yaml.Node:  # type: ignore[name-defined]
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:  # type: ignore[name-defined]
    style = '"' if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _represent_str)  # type: ignore[reportUnknownMemberType]
_Dumper.add_representer(_Flow, _represent_flow)  # type: ignore[reportUnknownMemberType]
_Dumper.add_representer(_Literal, _represent_literal)  # type: ignore[reportUnknownMemberType]


def _presentable(block: YamlMap) -> YamlMap:
    shaped: YamlMap = dict(block)
    runs = shaped.get("runs")
    if isinstance(runs, list):
        shaped["runs"] = [
            _Flow(run) if isinstance(run, dict) and len(repr(run)) <= 96 else run for run in runs
        ]
    source = shaped.get("source")
    if isinstance(source, str):
        shaped["source"] = _Literal(source)
    return shaped


def _dump(document: YamlMap) -> str:
    shaped = dict(document)
    blocks = shaped.get("blocks")
    if isinstance(blocks, list):
        shaped["blocks"] = [_presentable(b) if isinstance(b, dict) else b for b in blocks]
    text = yaml.dump(  # type: ignore[reportUnknownMemberType]
        shaped,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )
    return cast(str, text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _open(source: Path) -> OpcPackageReader:
    if not source.exists():
        raise WorksheetImportError(f"input file {source} not found")
    if not source.is_file():
        raise WorksheetImportError(f"input path {source} is not a file")
    if source.suffix.lower() != ".mcdx":
        raise WorksheetImportError(f"input {source} is not a .mcdx worksheet")
    try:
        return OpcPackageReader(source)
    except (zipfile.BadZipFile, KeyError, OSError, etree.XMLSyntaxError) as exc:
        raise WorksheetImportError(f"{source} is not a readable .mcdx package: {exc}") from exc


def import_worksheet(source: Path, output: Path, *, force: bool = False) -> ImportResult:
    """Convert ``source`` (.mcdx) to semantic YAML at ``output``, PNGs beside it.

    Nothing is written unless the whole import succeeds; an existing output
    file (YAML or image) is only replaced with ``force``.
    """

    source = Path(source)
    output = Path(output)
    if output.exists() and not force:
        raise WorksheetImportError(f"output {output} exists; pass --force to overwrite it")
    if output.is_dir():
        raise WorksheetImportError(f"output path {output} is a directory")
    warnings: list[str] = []
    with _open(source) as reader:
        try:
            root = etree.fromstring(reader.read_part(_WORKSHEET_PART))
        except KeyError:
            raise WorksheetImportError(f"{source} has no {_WORKSHEET_PART} part") from None
        except etree.XMLSyntaxError as exc:
            raise WorksheetImportError(f"{source} has malformed worksheet XML: {exc}") from exc
        settings, content_width = _worksheet(reader, warnings)
        context = _Context(
            reader=reader,
            relationships={
                rel.rel_id: rel.target for rel in reader.get_relationships(_WORKSHEET_PART)
            },
            grid_px=GRID_PX["fine"],
            content_width_px=content_width,
            image_stem=output.stem,
            image_dir=output.parent,
        )
        metadata = _metadata(context)
        blocks = _blocks(context, root)
    document: YamlMap = {
        "schema_version": 1,
        "document": metadata,
        "worksheet": settings,
        "profile": "engineering",
    }
    if context.styles:
        document["styles"] = cast(YamlValue, context.styles)
    document["blocks"] = cast(YamlValue, blocks)
    text = _dump(document)
    try:
        validated = parse_document(text, source_name=str(output))
    except AuthoringDocumentError as exc:
        raise WorksheetImportError(f"imported document does not validate: {exc}") from exc
    if not force:
        existing = [path for path, _data in context.images if path.exists()]
        if existing:
            raise WorksheetImportError(f"output {existing[0]} exists; pass --force to overwrite it")
    output.parent.mkdir(parents=True, exist_ok=True)
    for path, data in context.images:
        path.write_bytes(data)
    output.write_text(text, encoding="utf-8")
    return ImportResult(
        output=output,
        document=validated,
        images=tuple(path for path, _data in context.images),
        unsupported=tuple(context.unsupported),
        warnings=tuple(warnings),
    )


__all__ = [
    "ImportResult",
    "UnsupportedRegion",
    "WorksheetImportError",
    "expression",
    "function_source",
    "import_worksheet",
]
