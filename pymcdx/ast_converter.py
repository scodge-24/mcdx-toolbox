"""
Python AST to Mathcad Math50 XML Converter.

This module converts Python code (specifically engineering calculations) into
Mathcad Prime's Math50 XML format, which can be embedded in .mcdx files.

Supported features:
- Variable assignments (x = 1)
- Function definitions (def f(x): return x + 1)
- Multi-statement function bodies with ml:program blocks
- Binary operators: +, -, *, /, **, //, %
- Unit quantities via Q_(value, unit) or Physical(value, unit)
- Complex unit parsing (e.g., 'm/s^2' -> m / s^2)
- step() unwrapping: step(expr, "desc") -> expr
- cast() unwrapping: cast(type, expr) -> expr
- Math builtins: pi, sqrt, root, max, min, abs
- Bare unit names: MPa, kN, mm, etc. tagged as UNIT
- Conditionals: if/elif/else -> ml:program + ml:if
- Lists/matrices: [[a], [b]] -> ml:matrix
- Transpose: M.T -> ml:apply + ml:transpose
- Range: range(0, n) -> ml:range
- For loops: for i in range(n) -> ml:for
- While loops: while cond -> ml:while
- Lambda: lambda x: expr -> ml:lambda
- Local assignments in program blocks -> ml:localDefine

Limitations:
- Class definitions, imports, and decorators are skipped with a warning
- Matrix operations (@) not supported
"""

import ast
import copy
import re
import warnings

from lxml import etree

# Namespaces
MATH50_NS = "http://schemas.mathsoft.com/math50"
NSMAP = {"ml": MATH50_NS}  # Use 'ml' prefix as seen in baseline
XML_NS = "http://www.w3.org/XML/1998/namespace"

# Bare unit names recognised in @equation functions (tagged as UNIT in Mathcad)
UNIT_NAMES = {
    "MPa",
    "GPa",
    "kPa",
    "Pa",
    "kN",
    "N",
    "MN",
    "kNm",
    "Nm",
    "MNm",
    "mm",
    "cm",
    "m",
    "km",
    "mm2",
    "cm2",
    "m2",
    "mm3",
    "cm3",
    "m3",
    "mm4",
    "cm4",
    "m4",
    "mm6",
    "cm6",
    "m6",
    "kg",
    "kNm2",
}

# Shorthand names with no Prime unit of their own: expand to real unit
# products/powers (Prime flags an id such as ``mm4`` or ``kNm`` as undefined).
COMPOUND_UNITS = {
    "kNm": "kN*m",
    "Nm": "N*m",
    "MNm": "MN*m",
    "kNm2": "kN*m^2",
    **{f"{base}{power}": f"{base}^{power}" for base in ("mm", "cm", "m") for power in (2, 3, 4, 6)},
}

# Python name → Mathcad symbol mappings
MATH_CONSTANTS = {
    "pi": "\u03c0",  # π
}

# Radicals: Python-side name -> required argument count.
RADICAL_ARITY = {"sqrt": 1, "root": 2}


def check_radical_arity(name: str, count: int) -> None:
    """Refuse ``sqrt``/``root`` calls with the wrong number of arguments."""

    expected = RADICAL_ARITY[name]
    if count != expected:
        usage = "sqrt(x)" if name == "sqrt" else "root(x, n)"
        plural = "" if expected == 1 else "s"
        raise ValueError(
            f"{name}() takes exactly {expected} argument{plural} ({usage}), got {count}"
        )


# Math builtins that need special handling
MATH_BUILTINS_PASSTHROUGH = {"sqrt", "abs"}
MATH_BUILTINS_FUNCTION = {
    "max",
    "min",
    "lsolve",
    "linsolve",
    "rref",
    "rank",
    "tr",
    "eigenvals",
    "eigenvecs",
    "det",
    "lu",
    "qr",
    "svd",
}

# Greek letter names → Unicode symbols for Mathcad display
GREEK_TO_UNICODE = {
    "alpha": "\u03b1",
    "beta": "\u03b2",
    "gamma": "\u03b3",
    "delta": "\u03b4",
    "epsilon": "\u03b5",
    "eta": "\u03b7",
    "theta": "\u03b8",
    "lambda": "\u03bb",
    "mu": "\u03bc",
    "nu": "\u03bd",
    "pi": "\u03c0",
    "rho": "\u03c1",
    "sigma": "\u03c3",
    "tau": "\u03c4",
    "phi": "\u03c6",
    "chi": "\u03c7",
    "psi": "\u03c8",
    "omega": "\u03c9",
    "Gamma": "\u0393",
    "Delta": "\u0394",
    "Theta": "\u0398",
    "Lambda": "\u039b",
    "Sigma": "\u03a3",
    "Phi": "\u03a6",
    "Psi": "\u03a8",
    "Omega": "\u03a9",
}


def _greek_base(name: str) -> str:
    """Convert a Greek letter name to its Unicode symbol, or return unchanged."""
    if name.lower() in GREEK_TO_UNICODE:
        return GREEK_TO_UNICODE[name.lower()]
    return name


XAML_NS = "http://schemas.microsoft.com/winfx/2006/xaml/presentation"
PW_NS = "clr-namespace:Ptc.Wpf;assembly=Ptc.Core"


def create_variable_id(name):
    """
    Create an ml:id element for a variable name, with XAML subscript rendering.

    Mathcad Prime encodes label subscripts as inline XAML inside the ml:id:
        <ml:id labels="VARIABLE" xml:space="preserve">
          <Span xmlns="..." xmlns:pw="...">base<pw:Subscript>sub</pw:Subscript></Span>
        </ml:id>

    Rules (mirroring _name_to_latex in equation.py):
    - No underscore: plain ml:id (with Greek conversion)
    - First underscore: split into base + subscript via XAML Span + pw:Subscript
    - Subsequent underscores in subscript part: removed
    - Greek letter base names become Unicode symbols (gamma → γ)
    - bar_ prefix is stripped

    Examples:
        A         → <ml:id>A</ml:id>
        A_v       → <ml:id><Span>A<pw:Subscript>v</pw:Subscript></Span></ml:id>
        A_v_min   → <ml:id><Span>A<pw:Subscript>vmin</pw:Subscript></Span></ml:id>
        gamma_M0  → <ml:id><Span>γ<pw:Subscript>M0</pw:Subscript></Span></ml:id>
        chi       → <ml:id>χ</ml:id>
    """
    # Handle bar_ prefix → just use the inner name
    if name.startswith("bar_"):
        name = name[4:]

    parts = name.split("_", 1)
    base = _greek_base(parts[0])

    if len(parts) == 1:
        # No subscript — plain id
        return create_element(
            "id",
            base,
            attrib={"labels": "VARIABLE", f"{{{XML_NS}}}space": "preserve"},
        )

    # Has subscript — use XAML Span + pw:Subscript inside ml:id
    subscript = parts[1].replace("_", "")

    id_elem = create_element(
        "id",
        attrib={"labels": "VARIABLE", f"{{{XML_NS}}}space": "preserve"},
    )

    span_nsmap = {None: XAML_NS, "pw": PW_NS}
    span = etree.SubElement(id_elem, f"{{{XAML_NS}}}Span", nsmap=span_nsmap)
    span.text = base

    sub = etree.SubElement(span, f"{{{PW_NS}}}Subscript")
    sub.text = subscript

    return id_elem


def create_element(tag, text=None, attrib=None):
    """Create an element in the Math50 namespace."""
    elem = etree.Element(f"{{{MATH50_NS}}}{tag}", nsmap=NSMAP)
    if text is not None:
        elem.text = str(text)
    if attrib:
        for k, v in attrib.items():
            elem.set(k, v)
    return elem


def create_eval(expr_elem):
    """Wrap an expression in a Mathcad eval region (`=` output form)."""
    eval_elem = create_element("eval")
    eval_elem.append(expr_elem)
    unit_override = create_element("unitOverride")
    unit_override.append(create_element("placeholder"))
    eval_elem.append(unit_override)
    return eval_elem


def parse_unit_string(unit_str):
    """
    Parse a unit string into a Mathcad XML element.

    Handles common patterns:
    - Simple units: 'm', 'kg', 's'
    - Compound units: 'm/s', 'kg*m', 'N/mm^2'
    - Powers: 'm^2', 's^-1', 'mm^2'

    Args:
        unit_str: Unit string like 'm/s^2' or 'kN*m'

    Returns:
        lxml Element representing the unit expression
    """
    unit_str = unit_str.strip()

    # Simple case: single unit without operators
    if re.match(r"^[a-zA-Z_]+$", unit_str):
        return create_element(
            "id", unit_str, attrib={"labels": "UNIT", f"{{{XML_NS}}}space": "preserve"}
        )

    # Handle power notation: unit^exponent
    power_match = re.match(r"^([a-zA-Z_]+)\^(-?\d+)$", unit_str)
    if power_match:
        base_unit = power_match.group(1)
        exponent = power_match.group(2)
        apply_elem = create_element("apply")
        apply_elem.append(create_element("pow"))
        apply_elem.append(
            create_element(
                "id",
                base_unit,
                attrib={"labels": "UNIT", f"{{{XML_NS}}}space": "preserve"},
            )
        )
        apply_elem.append(create_element("real", exponent))
        return apply_elem

    # Handle division: e.g., 'm/s' or 'm/s^2'
    if "/" in unit_str:
        parts = unit_str.split("/", 1)
        numerator = parse_unit_string(parts[0])
        denominator = parse_unit_string(parts[1])
        apply_elem = create_element("apply")
        apply_elem.append(create_element("div"))
        apply_elem.append(numerator)
        apply_elem.append(denominator)
        return apply_elem

    # Handle multiplication: e.g., 'kg*m' or 'kN*m'
    if "*" in unit_str:
        parts = unit_str.split("*", 1)
        left = parse_unit_string(parts[0])
        right = parse_unit_string(parts[1])
        apply_elem = create_element("apply")
        apply_elem.append(create_element("mult"))
        apply_elem.append(left)
        apply_elem.append(right)
        return apply_elem

    # Fallback: treat as single unit identifier
    return create_element(
        "id", unit_str, attrib={"labels": "UNIT", f"{{{XML_NS}}}space": "preserve"}
    )


class PythonToMathcadConverter(ast.NodeVisitor):
    """
    AST visitor that converts Python nodes to Mathcad Math50 XML elements.

    This visitor handles:
    - Assignments (Assign) -> ml:define (or ml:localDefine in program context)
    - Function definitions (FunctionDef) -> ml:define with ml:function
    - Multi-statement function bodies -> ml:program blocks
    - Binary operations (BinOp) -> ml:apply with operator
    - Function calls (Call) -> ml:apply, with special handling for Q_/Physical
    - Names (Name) -> ml:id with VARIABLE label
    - Constants (Constant) -> ml:real or ml:str
    - Lists (List) -> ml:matrix
    - Attribute access (.T) -> ml:transpose
    - For loops (For) -> ml:for
    - While loops (While) -> ml:while
    - Lambda (Lambda) -> ml:lambda

    Unsupported nodes are skipped with a warning.
    """

    def __init__(self):
        self._in_program = False
        # True while visiting a statement in tail position of the function
        # body, where Mathcad's implicit last value equals Python's return.
        self._tail = False

    def convert(self, node):
        """Convert an AST node to a Mathcad XML element."""
        return self.visit(node)

    def generic_visit(self, node):
        """
        Handle unsupported AST nodes by issuing a warning.

        This is called for any node type without a specific visit_* method.
        """
        # Skip common harmless nodes that don't need conversion
        if isinstance(node, (ast.Load, ast.Store, ast.Del)):
            return

        warnings.warn(
            f"Skipping unsupported AST node: {type(node).__name__}",
            UserWarning,
            stacklevel=2,
        )
        return

    def visit_Module(self, node):
        """Handle module node (should not be called for single expressions)."""
        if node.body:
            return self.visit(node.body[0])
        return None

    def visit_Expr(self, node):
        """Handle expression statement.

        At top level, bare expressions are rendered as Mathcad eval regions,
        matching the UI behavior of typing a trailing `=` to show an output.
        Inside program blocks, preserve the legacy raw-expression behavior.
        """
        value_elem = self.visit(node.value)
        if value_elem is None:
            return None
        if self._in_program:
            return value_elem
        return create_eval(value_elem)

    def visit_Constant(self, node):
        """
        Convert Python constants to Mathcad elements.

        - int/float -> ml:real
        - str -> ml:str with xml:space="preserve"
        """
        if isinstance(node.value, (int, float)):
            return create_element("real", node.value)
        if isinstance(node.value, str):
            return create_element("str", node.value, attrib={f"{{{XML_NS}}}space": "preserve"})
        if node.value is None:
            # Python None — emit as real 0 (placeholder in Mathcad context)
            return create_element("real", 0)
        return None

    def visit_Name(self, node):
        """
        Convert Python name to Mathcad variable ID.

        Handles:
        - Unit names (MPa, kN, mm, ...) -> labels="UNIT"
        - Regular variables -> labels="VARIABLE" with subscript formatting
        """
        name = node.id

        if name in MATH_CONSTANTS:
            return create_element(
                "id",
                MATH_CONSTANTS[name],
                attrib={"labels": "CONSTANT", f"{{{XML_NS}}}space": "preserve"},
            )

        if name in COMPOUND_UNITS:
            return parse_unit_string(COMPOUND_UNITS[name])

        # Bare unit names → UNIT label (no subscript transformation)
        if name in UNIT_NAMES:
            return create_element(
                "id",
                name,
                attrib={"labels": "UNIT", f"{{{XML_NS}}}space": "preserve"},
            )

        elem = create_variable_id(name)
        if self._in_program and isinstance(getattr(node, "ctx", None), ast.Load):
            elem.set("label-is-contextual", "true")
        return elem

    def visit_List(self, node):
        """
        Convert Python list to Mathcad matrix element.

        - Nested lists [[a], [b]] -> ml:matrix with rows/cols
        - Flat list [a, b, c] -> ml:matrix as column vector (cols=1)
        """
        if node.elts and all(isinstance(elt, ast.List) for elt in node.elts):
            # Nested list of lists → matrix
            first_row = node.elts[0]
            assert isinstance(first_row, ast.List)
            rows = len(node.elts)
            cols = len(first_row.elts)
            matrix = create_element("matrix", attrib={"rows": str(rows), "cols": str(cols)})
            for row in node.elts:
                assert isinstance(row, ast.List)
                for item in row.elts:
                    matrix.append(self.visit(item))
            return matrix

        # Flat list → column vector
        matrix = create_element("matrix", attrib={"rows": str(len(node.elts)), "cols": "1"})
        for item in node.elts:
            matrix.append(self.visit(item))
        return matrix

    def visit_Attribute(self, node):
        """
        Convert attribute access to Mathcad elements.

        Handles .T for matrix transpose:
            M.T -> <ml:apply><ml:transpose/><M/></ml:apply>
        """
        if node.attr == "T":
            apply_elem = create_element("apply")
            apply_elem.append(create_element("transpose"))
            apply_elem.append(self.visit(node.value))
            return apply_elem

        warnings.warn(
            f"Unsupported attribute access: .{node.attr}",
            UserWarning,
            stacklevel=2,
        )
        return None

    def visit_Subscript(self, node):
        """
        Convert subscript access to Mathcad indexer.

        Python: v[0] -> <ml:apply><ml:indexer/><v/><0/></ml:apply>
        """
        apply_elem = create_element("apply")
        apply_elem.append(create_element("indexer"))
        apply_elem.append(self.visit(node.value))
        apply_elem.append(self.visit(node.slice))
        return apply_elem

    def visit_BinOp(self, node):
        """
        Convert binary operations to Mathcad apply elements.

        Supported operators:
        - Add (+) -> ml:plus
        - Sub (-) -> ml:minus
        - Mult (*) -> ml:mult
        - Div (/) -> ml:div
        - Pow (**) -> ml:pow
        - FloorDiv (//) -> ml:floor(ml:div(...))
        - Mod (%) -> mod(a, b), Mathcad's builtin (sign of the dividend)
        """
        op_map = {
            ast.Add: "plus",
            ast.Sub: "minus",
            ast.Mult: "mult",
            ast.Div: "div",
            ast.Pow: "pow",
        }

        op_tag = op_map.get(type(node.op))

        # Special case: floor division -> floor(a / b)
        if isinstance(node.op, ast.FloorDiv):
            # Create inner division
            div_elem = create_element("apply")
            div_elem.append(create_element("div"))
            div_elem.append(self.visit(node.left))
            div_elem.append(self.visit(node.right))
            # Wrap in floor function
            floor_elem = create_element("apply")
            floor_elem.append(
                create_element(
                    "id",
                    "floor",
                    attrib={"labels": "FUNCTION", f"{{{XML_NS}}}space": "preserve"},
                )
            )
            floor_elem.append(div_elem)
            return floor_elem

        # a % b -> mod(a, b): Prime has no mod operator; the builtin takes its
        # two arguments as an ml:sequence.
        if isinstance(node.op, ast.Mod):
            mod_elem = create_element("apply")
            mod_elem.append(
                create_element(
                    "id",
                    "mod",
                    attrib={"labels": "FUNCTION", f"{{{XML_NS}}}space": "preserve"},
                )
            )
            sequence = create_element("sequence")
            sequence.append(self.visit(node.left))
            sequence.append(self.visit(node.right))
            mod_elem.append(sequence)
            return mod_elem

        if not op_tag:
            raise NotImplementedError(f"Operator {type(node.op).__name__} not supported")

        apply_elem = create_element("apply")
        apply_elem.append(create_element(op_tag))
        apply_elem.append(self.visit(node.left))
        apply_elem.append(self.visit(node.right))
        return apply_elem

    def visit_UnaryOp(self, node):
        """
        Convert unary operations to Mathcad elements.

        Supported: USub (-x) -> ml:apply with ml:minus and 0
        """
        if isinstance(node.op, ast.USub):
            # -x becomes 0 - x in Mathcad, or use ml:neg if available
            apply_elem = create_element("apply")
            apply_elem.append(create_element("neg"))
            apply_elem.append(self.visit(node.operand))
            return apply_elem
        if isinstance(node.op, ast.UAdd):
            # +x is just x
            return self.visit(node.operand)

        raise NotImplementedError(f"Unary operator {type(node.op).__name__} not supported")

    def visit_BoolOp(self, node):
        """
        Convert boolean operations (and/or) to Mathcad apply elements.

        Python: a and b -> <ml:apply><ml:and/><a/><b/></ml:apply>
        Python: a or b  -> <ml:apply><ml:or/><a/><b/></ml:apply>
        """
        op_tag = "and" if isinstance(node.op, ast.And) else "or"

        # BoolOp can have 2+ values; chain them pairwise
        result = self.visit(node.values[0])
        for val in node.values[1:]:
            apply_elem = create_element("apply")
            apply_elem.append(create_element(op_tag))
            apply_elem.append(result)
            apply_elem.append(self.visit(val))
            result = apply_elem
        return result

    def visit_Compare(self, node):
        """
        Convert comparison operations to Mathcad apply elements.

        Supported operators:
        - Eq (==) -> ml:equal
        - NotEq (!=) -> ml:notEqual
        - Lt (<) -> ml:lessThan
        - Gt (>) -> ml:greaterThan
        - LtE (<=) -> ml:lessOrEqual
        - GtE (>=) -> ml:greaterOrEqual

        Chained comparisons (a < b < c) are converted to (a < b) AND (b < c).
        """
        cmp_map = {
            ast.Eq: "equal",
            ast.NotEq: "notEqual",
            ast.Lt: "lessThan",
            ast.Gt: "greaterThan",
            ast.LtE: "lessOrEqual",
            ast.GtE: "greaterOrEqual",
            ast.Is: "equal",
            ast.IsNot: "notEqual",
        }

        # `x in (a, b, c)` → `x == a or x == b or x == c`
        # `x not in (a, b)` → `x != a and x != b`
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            comparator = node.comparators[0]
            if isinstance(comparator, (ast.Tuple, ast.List)):
                is_in = isinstance(node.ops[0], ast.In)
                eq_tag = "equal" if is_in else "notEqual"
                chain_tag = "or" if is_in else "and"

                left_elem = self.visit(node.left)
                terms = []
                for elt in comparator.elts:
                    cmp_elem = create_element("apply")
                    cmp_elem.append(create_element(eq_tag))
                    cmp_elem.append(copy.deepcopy(left_elem) if terms else left_elem)
                    cmp_elem.append(self.visit(elt))
                    terms.append(cmp_elem)

                result = terms[0]
                for term in terms[1:]:
                    chain_elem = create_element("apply")
                    chain_elem.append(create_element(chain_tag))
                    chain_elem.append(result)
                    chain_elem.append(term)
                    result = chain_elem
                return result

        # Simple case: single comparison (x < y)
        if len(node.ops) == 1 and len(node.comparators) == 1:
            op_tag = cmp_map.get(type(node.ops[0]))
            if not op_tag:
                raise NotImplementedError(
                    f"Comparison operator {type(node.ops[0]).__name__} not supported"
                )

            apply_elem = create_element("apply")
            apply_elem.append(create_element(op_tag))
            apply_elem.append(self.visit(node.left))
            apply_elem.append(self.visit(node.comparators[0]))
            return apply_elem

        # Chained comparisons: a < b < c -> (a < b) AND (b < c)
        comparisons = []
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            op_tag = cmp_map.get(type(op))
            if not op_tag:
                raise NotImplementedError(f"Comparison operator {type(op).__name__} not supported")

            cmp_elem = create_element("apply")
            cmp_elem.append(create_element(op_tag))
            cmp_elem.append(self.visit(left))
            cmp_elem.append(self.visit(right))
            comparisons.append(cmp_elem)
            left = right

        # Chain with AND (ml:and)
        result = comparisons[0]
        for cmp in comparisons[1:]:
            and_elem = create_element("apply")
            and_elem.append(create_element("and"))
            and_elem.append(result)
            and_elem.append(cmp)
            result = and_elem

        return result

    def _range_last(self, stop):
        """Last element of a Mathcad range from a Python range() stop.

        Python's stop is exclusive, a Mathcad range includes its last element:
        constant stops fold to stop - 1, the `expr + 1` idiom folds to expr,
        anything else emits an explicit `stop - 1` apply.
        """
        if isinstance(stop, ast.Constant) and isinstance(stop.value, int):
            return create_element("real", stop.value - 1)
        if (
            isinstance(stop, ast.BinOp)
            and isinstance(stop.op, ast.Add)
            and isinstance(stop.right, ast.Constant)
            and stop.right.value == 1
        ):
            return self.visit(stop.left)
        minus_elem = create_element("apply")
        minus_elem.append(create_element("minus"))
        minus_elem.append(self.visit(stop))
        minus_elem.append(create_element("real", 1))
        return minus_elem

    def visit_Call(self, node):
        """
        Convert function calls to Mathcad apply elements.

        Special handling for:
        - step(expr, "desc") -> convert expr (unwrap step)
        - cast(type, expr) -> convert expr (unwrap cast)
        - Q_(value, unit) / Physical(value, unit) -> value * unit_expression
        - sqrt(x) -> <ml:apply><ml:sqrt/><x/></ml:apply>
        - root(x, n) -> <ml:apply><ml:nthRoot/><n/><x/></ml:apply>
        - abs(x) -> <ml:apply><ml:absval/><x/></ml:apply>
        - max/min(a, b) -> <ml:apply><ml:id labels="FUNCTION">max</ml:id>...
        - sum(gen) -> ml:sum structure

        Other calls: <ml:apply><ml:id>func</ml:id><args...></ml:apply>
        """
        func_name = getattr(node.func, "id", None) if isinstance(node.func, ast.Name) else None

        # step(expr, "description") → unwrap to just expr
        if func_name == "step" and len(node.args) >= 1:
            return self.visit(node.args[0])

        # cast(type, expr) → unwrap to just expr
        if func_name == "cast" and len(node.args) == 2:
            return self.visit(node.args[1])

        # Q_ or Physical (unit quantities)
        if func_name in ("Q_", "Physical") and len(node.args) == 2:
            value_node = node.args[0]
            unit_node = node.args[1]

            value_elem = self.visit(value_node)

            if isinstance(unit_node, ast.Constant) and isinstance(unit_node.value, str):
                unit_str = unit_node.value
                unit_elem = parse_unit_string(unit_str)

                apply_elem = create_element("apply")
                apply_elem.append(create_element("mult"))
                apply_elem.append(value_elem)
                apply_elem.append(unit_elem)
                return apply_elem

        # if_(cond, a, b) → Mathcad's built-in if() function: a KEYWORD apply
        # over a three-element sequence (Math50 apply,
        # "Keyword dispatch — inline if"). Unlike the program-statement ml:if
        # that a Python ternary lowers to, Express evaluates it.
        if func_name == "if_" and len(node.args) == 3:
            apply_elem = create_element("apply")
            apply_elem.append(
                create_element(
                    "id", "if", attrib={"labels": "KEYWORD", f"{{{XML_NS}}}space": "preserve"}
                )
            )
            sequence = create_element("sequence")
            for arg in node.args:
                sequence.append(self.visit(arg))
            apply_elem.append(sequence)
            return apply_elem

        # sqrt(x) → radical; root(x, n) → n-th root radical
        if func_name in RADICAL_ARITY:
            check_radical_arity(func_name, len(node.args))
            apply_elem = create_element("apply")
            if func_name == "sqrt":
                apply_elem.append(create_element("sqrt"))
                apply_elem.append(self.visit(node.args[0]))
            else:
                # Prime stores the index before the radicand.
                apply_elem.append(create_element("nthRoot"))
                apply_elem.append(self.visit(node.args[1]))
                apply_elem.append(self.visit(node.args[0]))
            return apply_elem

        # abs(x) → <ml:apply><ml:absval/><x/></ml:apply>
        if func_name == "abs" and len(node.args) == 1:
            apply_elem = create_element("apply")
            apply_elem.append(create_element("absval"))
            apply_elem.append(self.visit(node.args[0]))
            return apply_elem

        # max/min → <ml:apply><ml:id labels="FUNCTION">max/min</ml:id><args...>
        if func_name in MATH_BUILTINS_FUNCTION:
            apply_elem = create_element("apply")
            apply_elem.append(
                create_element(
                    "id",
                    func_name,
                    attrib={"labels": "FUNCTION", f"{{{XML_NS}}}space": "preserve"},
                )
            )
            for arg in node.args:
                apply_elem.append(self.visit(arg))
            return apply_elem

        # range(start, stop) → ml:range. Python's stop is EXCLUSIVE; a Mathcad
        # range includes its last element, so the last element is stop - 1
        # (the sum()-generator path below already does this).
        if func_name == "range":
            if len(node.args) not in (1, 2):
                raise ValueError(
                    "range() with an explicit step is not supported for Mathcad conversion"
                )
            range_elem = create_element("range")
            if len(node.args) == 1:
                range_elem.append(create_element("real", 0))
                range_elem.append(self._range_last(node.args[0]))
            else:
                range_elem.append(self.visit(node.args[0]))
                range_elem.append(self._range_last(node.args[1]))
            return range_elem

        # sum() over a generator: Prime's summation operator has no confirmed
        # Math50 encoding, and the earlier ml:sum guess failed the Worksheet50 schema.
        if (
            func_name == "sum"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.GeneratorExp)
        ):
            raise NotImplementedError(
                "sum() over a generator has no confirmed Mathcad Prime encoding; "
                "write the summation as a function block with a for loop"
            )

        # String method calls (e.g. x.lower(), x.upper()) → pass through base variable
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "lower",
            "upper",
            "strip",
            "lstrip",
            "rstrip",
        ):
            return self.visit(node.func.value)

        # Standard function call
        apply_elem = create_element("apply")
        apply_elem.append(self.visit(node.func))

        for arg in node.args:
            apply_elem.append(self.visit(arg))

        if node.keywords:
            warnings.warn(
                f"Function '{getattr(node.func, 'id', 'unknown')}' called with keyword arguments. "
                "Mathcad functions are positional; keywords will be ignored and arguments "
                "treated as positional.",
                UserWarning,
                stacklevel=2,
            )
            for keyword in node.keywords:
                apply_elem.append(self.visit(keyword.value))

        return apply_elem

    def visit_Assign(self, node):
        """
        Convert assignment to Mathcad define.

        Python: x = 1
        Mathcad: <ml:define><ml:id>x</ml:id><ml:real>1</ml:real></ml:define>

        Skips assignments where the RHS is an unsupported node type.
        """
        if len(node.targets) != 1:
            warnings.warn("Skipping multiple target assignment", UserWarning, stacklevel=2)
            return None

        target_elem = self.visit(node.targets[0])
        value_elem = self.visit(node.value)

        # Skip if either side couldn't be converted
        if target_elem is None or value_elem is None:
            target_name = getattr(node.targets[0], "id", "<unknown>")
            warnings.warn(
                f"Skipping assignment to '{target_name}' (unsupported RHS type)",
                UserWarning,
                stacklevel=2,
            )
            return None

        tag = "localDefine" if self._in_program else "define"
        define_elem = create_element(tag)
        define_elem.append(target_elem)
        define_elem.append(value_elem)
        return define_elem

    def visit_AnnAssign(self, node):
        """
        Convert annotated assignment to Mathcad define.

        Python: F: Physical = step(A * f_y, "Force")
        Treated the same as a plain assignment (annotation is ignored).
        Uses ml:localDefine when inside a program block.
        """
        if node.value is None:
            return None

        target_elem = self.visit(node.target)
        value_elem = self.visit(node.value)

        if target_elem is None or value_elem is None:
            return None

        tag = "localDefine" if self._in_program else "define"
        define_elem = create_element(tag)
        define_elem.append(target_elem)
        define_elem.append(value_elem)
        return define_elem

    def visit_Return(self, node):
        """
        Convert return statement.

        In program context (multi-statement body), generates ml:return element.
        In simple context (@equation single-return), unwraps to just the value.
        Returns None for bare `return` statements.
        """
        if node.value is None:
            return None
        if self._in_program:
            return_elem = create_element("return")
            return_elem.append(self.visit(node.value))
            return return_elem
        return self.visit(node.value)

    def visit_FunctionDef(self, node):
        """
        Convert function definition to Mathcad function define.

        Simple case (single return):
            def f(x): return x + 1  →  <ml:define><ml:function>...</ml:function><expr/></ml:define>

        Multi-statement body:
            def f(x):
                a = x - 1
                return a
            →  <ml:define><ml:function>...</ml:function>
                 <ml:program>
                   <ml:localDefine>...</ml:localDefine>
                   <ml:id>a</ml:id>
                 </ml:program>
               </ml:define>

        In multi-statement bodies, the final return becomes a bare expression
        (Mathcad implicit return), and assignments use ml:localDefine.
        """
        define_elem = create_element("define")

        # Function signature
        func_elem = create_element("function")
        func_elem.append(create_variable_id(node.name))

        if node.args.args:
            bound_vars = create_element("boundVars")
            for arg in node.args.args:
                bound_vars.append(create_variable_id(arg.arg))
            func_elem.append(bound_vars)

        define_elem.append(func_elem)

        # Simple case: single return statement → bare expression (no program block)
        if (
            len(node.body) == 1
            and isinstance(node.body[0], ast.Return)
            and node.body[0].value is not None
        ):
            body_elem = self.visit(node.body[0].value)
            define_elem.append(body_elem)
            return define_elem

        # Multi-statement body → program block
        if len(node.body) >= 1:
            body_elem = self._convert_program_body(node.body, tail=True)
            if len(body_elem) > 0:
                define_elem.append(body_elem)
                return define_elem

        warnings.warn(
            f"Skipping function '{node.name}' (no convertible body)",
            UserWarning,
            stacklevel=2,
        )
        return None

    def _convert_program_body(self, body, *, tail=False):
        """
        Convert a list of statements to a ml:program element.

        Sets _in_program context so assignments become ml:localDefine.
        ``tail`` marks a body whose last statement ends the function (the
        function body itself, or a branch of an ``if`` in tail position).
        Only there does a final return become a bare expression (Mathcad's
        implicit last value); anywhere else it stays ``ml:return`` so an early
        exit from a branch or loop leaves the whole program, as in Python.
        """
        program = create_element("program")
        old_in_program = self._in_program
        old_tail = self._tail
        self._in_program = True
        try:
            for i, stmt in enumerate(body):
                self._tail = tail and i == len(body) - 1
                if self._tail and isinstance(stmt, ast.Return) and stmt.value is not None:
                    # Final return → bare expression (Mathcad implicit return)
                    elem = self.visit(stmt.value)
                else:
                    elem = self.visit(stmt)
                if elem is not None:
                    program.append(elem)
        finally:
            self._in_program = old_in_program
            self._tail = old_tail

        if len(program) == 0:
            raise NotImplementedError(
                "Empty program body — branch contains no convertible statements"
            )

        return program

    def visit_If(self, node):
        """
        Convert if/elif/else to Mathcad ml:if block.

        Python: if cond: x = expr1 elif cond2: x = expr2 else: x = expr3
        Mathcad:
            <ml:if>
              <ml:test>cond</ml:test>
              <ml:then><ml:program>...</ml:program></ml:then>
              <ml:elseif>
                <ml:test>cond2</ml:test>
                <ml:then><ml:program>...</ml:program></ml:then>
              </ml:elseif>
              <ml:else><ml:program>...</ml:program></ml:else>
            </ml:if>

        Every then/else body is wrapped in <ml:program> via _convert_program_body,
        supporting multi-statement branches with local defines, nested loops, etc.
        """
        if_elem = create_element("if")
        # Branches of an if in tail position end the function too.
        tail = self._tail

        # test + then
        test_elem = create_element("test")
        test_elem.append(self.visit(node.test))
        if_elem.append(test_elem)

        then_elem = create_element("then")
        then_elem.append(self._convert_program_body(node.body, tail=tail))
        if_elem.append(then_elem)

        # elif → elseif, else → else
        current = node
        while current.orelse:
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                # elif branch → elseif
                elif_node = current.orelse[0]
                elseif_elem = create_element("elseif")

                test_elem = create_element("test")
                test_elem.append(self.visit(elif_node.test))
                elseif_elem.append(test_elem)

                then_elem = create_element("then")
                then_elem.append(self._convert_program_body(elif_node.body, tail=tail))
                elseif_elem.append(then_elem)

                if_elem.append(elseif_elem)
                current = elif_node
            else:
                # else branch
                else_elem = create_element("else")
                else_elem.append(self._convert_program_body(current.orelse, tail=tail))
                if_elem.append(else_elem)
                break

        return if_elem

    def visit_IfExp(self, node):
        """
        Convert inline ternary (x if cond else y) to Mathcad ml:if.

        Python: 1.0 if t < 28 else 2/3
        Mathcad:
            <ml:if>
              <ml:test>t < 28</ml:test>
              <ml:then><ml:program>1.0</ml:program></ml:then>
              <ml:else><ml:program>2/3</ml:program></ml:else>
            </ml:if>
        """
        if_elem = create_element("if")

        test_elem = create_element("test")
        test_elem.append(self.visit(node.test))
        if_elem.append(test_elem)

        then_elem = create_element("then")
        then_prog = create_element("program")
        then_prog.append(self.visit(node.body))
        then_elem.append(then_prog)
        if_elem.append(then_elem)

        else_elem = create_element("else")
        else_prog = create_element("program")
        else_prog.append(self.visit(node.orelse))
        else_elem.append(else_prog)
        if_elem.append(else_elem)

        return if_elem

    def visit_For(self, node):
        """
        Convert for loop to Mathcad ml:for.

        Python: for i in range(0, n): s = s + i
        Mathcad (range last element is n - 1 — Python's stop is exclusive,
        Mathcad ranges include their last element):
            <ml:for>
              <ml:id>i</ml:id>
              <ml:range><ml:real>0</ml:real><ml:apply><ml:minus/>…n - 1…</ml:apply></ml:range>
              <ml:program><ml:localDefine>...</ml:localDefine></ml:program>
            </ml:for>
        """
        for_elem = create_element("for")
        for_elem.append(self.visit(node.target))
        for_elem.append(self.visit(node.iter))
        for_elem.append(self._convert_program_body(node.body))
        return for_elem

    def visit_While(self, node):
        """
        Convert while loop to Mathcad ml:while.

        Python: while err > tol: x = x / 2
        Mathcad:
            <ml:while>
              <ml:apply><ml:greaterThan/>...</ml:apply>
              <ml:program><ml:localDefine>...</ml:localDefine></ml:program>
            </ml:while>
        """
        while_elem = create_element("while")
        while_elem.append(self.visit(node.test))
        while_elem.append(self._convert_program_body(node.body))
        return while_elem

    def visit_Lambda(self, node):
        """
        Convert lambda to Mathcad ml:lambda.

        Python: lambda x: x + 1
        Mathcad:
            <ml:lambda>
              <ml:boundVars><ml:id>x</ml:id></ml:boundVars>
              <ml:apply><ml:plus/>...</ml:apply>
            </ml:lambda>
        """
        lambda_elem = create_element("lambda")
        bound_vars = create_element("boundVars")
        for arg in node.args.args:
            bound_vars.append(create_variable_id(arg.arg))
        lambda_elem.append(bound_vars)
        lambda_elem.append(self.visit(node.body))
        return lambda_elem

    def visit_Import(self, node):
        """Skip import statements with a warning."""
        warnings.warn(
            f"Skipping import statement: {', '.join(alias.name for alias in node.names)}",
            UserWarning,
            stacklevel=2,
        )
        return

    def visit_ImportFrom(self, node):
        """Skip from...import statements with a warning."""
        warnings.warn(
            f"Skipping 'from {node.module} import ...' statement",
            UserWarning,
            stacklevel=2,
        )
        return

    def visit_ClassDef(self, node):
        """Skip class definitions with a warning."""
        warnings.warn(f"Skipping class definition: {node.name}", UserWarning, stacklevel=2)
        return


def convert_python_to_math50(code_str):
    """
    Convert Python source code to a list of Mathcad Math50 XML elements.

    Args:
        code_str: Python source code as a string

    Returns:
        List of lxml Elements representing Mathcad math regions

    Example:
        >>> elements = convert_python_to_math50("x = 1\\ny = x + 2")
        >>> len(elements)
        2
    """
    tree = ast.parse(code_str)
    converter = PythonToMathcadConverter()
    elements = []

    # Check for module docstring to skip
    docstring = ast.get_docstring(tree)
    nodes = tree.body

    if docstring is not None and len(nodes) > 0:
        # Verify the first node is indeed the docstring expression
        first = nodes[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            # Skip it (it's handled as a text region by the builder)
            nodes = nodes[1:]

    for node in nodes:
        res = converter.visit(node)
        if res is not None:
            elements.append(res)
    return elements


def convert_python_expression_to_math50(code_str: str) -> etree._Element:
    """Lower one expression without adding the top-level evaluation wrapper.

    The normal module converter intentionally turns an expression statement
    into ``ml:eval``. Inline notation citations need the expression tree itself
    so a bare identifier can be embedded without an evaluation arrow.
    """

    tree = ast.parse(code_str, mode="eval")
    result = PythonToMathcadConverter().visit(tree.body)
    if not isinstance(result, etree._Element):
        raise NotImplementedError(f"Expression {code_str!r} did not lower to one Math50 element")
    return result
