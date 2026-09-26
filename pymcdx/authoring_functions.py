"""Semantic function blocks: validate a Python ``def`` and lower it to Mathcad.

A function block's ``source`` is one Python function in a documented subset.
Everything outside the subset is rejected here, at the semantic boundary, so
the converter never silently skips a statement (it only warns) and Prime never
receives a region it cannot draw. See ``contract-py2m-functions``.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass

from lxml import etree

from pymcdx.ast_converter import (
    RADICAL_ARITY,
    UNIT_NAMES,
    PythonToMathcadConverter,
    check_radical_arity,
)


class FunctionSourceError(ValueError):
    """A function block's source is outside the supported v1 subset."""


@dataclass(frozen=True, slots=True)
class FunctionDefinition:
    """A validated, normalised function ready for lowering."""

    name: str
    params: tuple[str, ...]
    calls: frozenset[str]
    node: ast.FunctionDef


_STATEMENT_NAMES: dict[type[ast.stmt], str] = {
    ast.Pass: "pass",
    ast.Break: "break",
    ast.Continue: "continue",
    ast.Try: "try",
    ast.With: "with",
    ast.Raise: "raise",
    ast.Global: "global",
    ast.Nonlocal: "nonlocal",
    ast.FunctionDef: "a nested def",
    ast.AsyncFunctionDef: "a nested def",
    ast.ClassDef: "class",
    ast.Import: "import",
    ast.ImportFrom: "import",
    ast.Delete: "del",
    ast.Assert: "assert",
    ast.AnnAssign: "annotated assignment",
    ast.Match: "match",
}
_EXPRESSION_NAMES: dict[type[ast.AST], str] = {
    ast.Lambda: "lambda",
    ast.Starred: "star-args",
    ast.Tuple: "tuples",
    ast.Dict: "dicts",
    ast.Set: "sets",
    ast.ListComp: "list comprehensions",
    ast.SetComp: "set comprehensions",
    ast.DictComp: "dict comprehensions",
    ast.JoinedStr: "f-strings",
    ast.NamedExpr: "the walrus operator",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield",
    ast.Slice: "slices",
}
_AUGMENTED_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.FloorDiv, ast.Mod)


def _error(node: ast.AST, message: str) -> FunctionSourceError:
    line = getattr(node, "lineno", None)
    return FunctionSourceError(message if line is None else f"line {line}: {message}")


def _check_name(name: str, node: ast.AST, role: str) -> None:
    if name in UNIT_NAMES:
        raise _error(node, f"{role} {name!r} is a Mathcad unit name; rename it")


def _check_expressions(node: ast.AST) -> set[str]:
    """Reject unsupported expression syntax and collect called function names."""

    calls: set[str] = set()
    for child in ast.walk(node):
        rejected = _EXPRESSION_NAMES.get(type(child))
        if rejected is not None:
            raise _error(child, f"{rejected} are not supported in a function block")
        if isinstance(child, ast.Call):
            if child.keywords:
                raise _error(child, "keyword arguments are not supported")
            if isinstance(child.func, ast.Name):
                calls.add(child.func.id)
                if child.func.id in RADICAL_ARITY:
                    try:
                        check_radical_arity(child.func.id, len(child.args))
                    except ValueError as exc:
                        raise _error(child, str(exc)) from None
    return calls


def _check_target(target: ast.expr) -> None:
    if isinstance(target, ast.Name):
        _check_name(target.id, target, "local")
        return
    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
        return
    raise _error(target, "only a name or an indexed element can be assigned (no destructuring)")


def _ends_with_value(body: list[ast.stmt]) -> bool:
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Expr)):
        return True
    if isinstance(last, ast.If):
        return bool(last.orelse) and _ends_with_value(last.body) and _ends_with_value(last.orelse)
    return False


def _check_body(body: list[ast.stmt], *, tail: bool, calls: set[str]) -> None:
    for index, statement in enumerate(body):
        last = index == len(body) - 1
        rejected = _STATEMENT_NAMES.get(type(statement))
        if rejected is not None:
            raise _error(statement, f"{rejected} is not supported in a function block")
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1:
                raise _error(statement, "chained assignment is not supported")
            _check_target(statement.targets[0])
            calls |= _check_expressions(statement)
        elif isinstance(statement, ast.AugAssign):
            if not isinstance(statement.target, ast.Name):
                raise _error(statement, "augmented assignment needs a plain name target")
            if not isinstance(statement.op, _AUGMENTED_OPS):
                raise _error(statement, "unsupported augmented assignment operator")
            _check_name(statement.target.id, statement, "local")
            calls |= _check_expressions(statement)
        elif isinstance(statement, ast.Return):
            if statement.value is None:
                raise _error(statement, "a return needs a value")
            calls |= _check_expressions(statement)
        elif isinstance(statement, ast.Expr):
            if not (last and tail):
                raise _error(
                    statement,
                    "an expression statement is only allowed as the function's final value",
                )
            calls |= _check_expressions(statement)
        elif isinstance(statement, ast.If):
            calls |= _check_expressions(statement.test)
            _check_body(statement.body, tail=tail and last, calls=calls)
            if statement.orelse:
                _check_body(statement.orelse, tail=tail and last, calls=calls)
        elif isinstance(statement, ast.For):
            if not isinstance(statement.target, ast.Name):
                raise _error(statement, "a for loop needs a single loop variable")
            _check_name(statement.target.id, statement, "loop variable")
            iterator = statement.iter
            if not (
                isinstance(iterator, ast.Call)
                and isinstance(iterator.func, ast.Name)
                and iterator.func.id == "range"
                and len(iterator.args) in (1, 2)
            ):
                raise _error(statement, "a for loop must iterate range(stop) or range(start, stop)")
            if statement.orelse:
                raise _error(statement, "for ... else is not supported")
            calls |= _check_expressions(iterator) - {"range"}
            _check_body(statement.body, tail=False, calls=calls)
        elif isinstance(statement, ast.While):
            if statement.orelse:
                raise _error(statement, "while ... else is not supported")
            calls |= _check_expressions(statement.test)
            _check_body(statement.body, tail=False, calls=calls)
        else:
            raise _error(statement, f"{type(statement).__name__} is not supported")


class _ExpandAugmented(ast.NodeTransformer):
    """Rewrite ``s += x`` as ``s = s + x``, which the converter lowers exactly."""

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.stmt:
        if not isinstance(node.target, ast.Name):  # rejected by _check_body
            return node
        load = ast.Name(id=node.target.id, ctx=ast.Load())
        store = ast.Name(id=node.target.id, ctx=ast.Store())
        value = ast.BinOp(left=load, op=node.op, right=node.value)
        return ast.copy_location(ast.Assign(targets=[store], value=value), node)


def parse_function(source: str) -> FunctionDefinition:
    """Validate one ``def`` against the v1 subset and normalise it."""

    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise FunctionSourceError(f"line {exc.lineno}: {exc.msg}") from None
    if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
        raise FunctionSourceError("a function block holds exactly one def and nothing else")
    function = module.body[0]
    arguments = function.args
    if function.decorator_list:
        raise _error(function, "decorators are not supported")
    if function.returns is not None or any(a.annotation for a in arguments.args):
        raise _error(function, "annotations are not supported")
    if arguments.posonlyargs or arguments.kwonlyargs or arguments.vararg or arguments.kwarg:
        raise _error(function, "only plain positional parameters are supported")
    if arguments.defaults:
        raise _error(function, "parameter defaults are not supported")
    _check_name(function.name, function, "function name")
    params = tuple(argument.arg for argument in arguments.args)
    for argument in arguments.args:
        _check_name(argument.arg, argument, "parameter")

    body = function.body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if not body:
        raise _error(function, "the function has no body")
    calls: set[str] = set()
    _check_body(body, tail=True, calls=calls)
    if not _ends_with_value(body):
        raise _error(
            body[-1],
            "the function can reach its end without a value (Python would return None)",
        )

    function.body = body
    normalised = ast.fix_missing_locations(_ExpandAugmented().visit(function))
    if not isinstance(normalised, ast.FunctionDef):
        raise _error(function, "function normalisation failed")
    return FunctionDefinition(function.name, params, frozenset(calls), normalised)


def render_function_block(source: str) -> etree._Element:
    """Lower a validated function source to one Math50 ``define`` element."""

    definition = parse_function(source)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            element = PythonToMathcadConverter().visit(definition.node)
        except (ValueError, NotImplementedError) as exc:
            raise FunctionSourceError(str(exc)) from exc
    if caught:
        raise FunctionSourceError("; ".join(str(warning.message) for warning in caught))
    if not isinstance(element, etree._Element):
        raise FunctionSourceError(f"function {definition.name!r} produced no Mathcad definition")
    return element


def called_names(expression: str) -> frozenset[str]:
    """Names called anywhere in a math expression (empty if it does not parse)."""

    try:
        tree = ast.parse(expression)
    except SyntaxError:
        return frozenset()
    return frozenset(
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )


def reject_function_syntax(expression: str) -> None:
    """Refuse ``def``/``lambda`` outside a function block."""

    try:
        tree = ast.parse(expression)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            raise ValueError("define functions with a `function` block, not inside math")


def reject_bad_radicals(expression: str) -> None:
    """Refuse ``sqrt``/``root`` calls with the wrong number of arguments."""

    try:
        tree = ast.parse(expression)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in RADICAL_ARITY
        ):
            check_radical_arity(node.func.id, len(node.args))
