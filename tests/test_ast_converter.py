"""Unit tests for the Python -> Mathcad AST converter.

Exercises the visitor for the major node kinds it supports, plus the
variable-id subscript/greek-letter naming rules and unit-string parsing.
"""

import pytest
from lxml import etree

from pymcdx.ast_converter import (
    convert_python_to_math50,
    create_variable_id,
    parse_unit_string,
)


def xml(elem) -> str:
    return etree.tostring(elem, encoding="unicode")


def convert_one(code: str):
    elems = convert_python_to_math50(code)
    assert len(elems) == 1
    return elems[0]


# ---------------------------------------------------------------------------
# Assignment / binary ops
# ---------------------------------------------------------------------------


def test_simple_assignment():
    elem = convert_one("x = 1")
    assert elem.tag.endswith("}define")
    assert xml(elem).count("ml:real") == 2  # open+close tag


def test_binary_op_plus():
    elem = convert_one("y = x + 2")
    s = xml(elem)
    assert "ml:plus" in s


def test_module_docstring_is_skipped():
    elems = convert_python_to_math50('"""A calc."""\nx = 1\n')
    assert len(elems) == 1


def test_chained_comparison_uses_and():
    elem = convert_one("ok = a < b < c")
    s = xml(elem)
    assert "ml:lessThan" in s
    assert "ml:and" in s


# ---------------------------------------------------------------------------
# Function calls: unit quantities, math builtins
# ---------------------------------------------------------------------------


def test_q_unit_quantity():
    elem = convert_one('F = Q_(5, "kN")')
    s = xml(elem)
    assert "ml:mult" in s
    assert 'labels="UNIT"' in s


def test_sqrt_becomes_a_radical():
    elem = convert_one("y = sqrt(x)")
    s = xml(elem)
    assert "ml:sqrt" in s
    assert "ml:pow" not in s


def test_abs_becomes_absval():
    elem = convert_one("y = abs(x)")
    assert "ml:absval" in xml(elem)


def test_max_min_are_function_calls():
    elem = convert_one("y = max(a, b)")
    s = xml(elem)
    assert 'labels="FUNCTION"' in s
    assert ">max<" in s


def test_step_unwraps_to_inner_expr():
    elem = convert_one('y = step(x + 1, "compute y")')
    s = xml(elem)
    assert "ml:plus" in s


def test_cast_unwraps_to_inner_expr():
    elem = convert_one("y = cast(int, x)")
    s = xml(elem)
    assert "ml:id" in s
    assert ">x<" in s


def test_range_call():
    # Python's stop is exclusive; the Mathcad range's last element is stop - 1.
    elem = convert_one("r = range(0, 5)")
    s = xml(elem)
    assert "ml:range" in s
    assert ">4<" in s
    assert ">5<" not in s


def test_range_call_plus_one_idiom_folds_to_bare_stop():
    # range(1, n + 1) is the inclusive-upper idiom: it becomes 1..n exactly,
    # with no minus apply.
    elem = convert_one("r = range(1, n + 1)")
    s = xml(elem)
    assert "ml:range" in s
    assert ">n<" in s
    assert "ml:minus" not in s


def test_range_call_single_arg_starts_at_zero():
    elem = convert_one("r = range(4)")
    s = xml(elem)
    assert "ml:range" in s
    assert ">0<" in s
    assert ">3<" in s


def test_range_call_symbolic_stop_emits_minus_one():
    elem = convert_one("r = range(a, b)")
    s = xml(elem)
    assert "ml:range" in s
    assert "ml:minus" in s
    assert ">1<" in s


def test_range_call_with_step_is_rejected():
    with pytest.raises(ValueError, match="step"):
        convert_one("r = range(0, 10, 2)")


def test_sum_generator_is_rejected_until_prime_encoding_is_confirmed():
    with pytest.raises(NotImplementedError, match="sum\\(\\) over a generator"):
        convert_one("total = sum(i for i in range(n))")


def test_mod_lowers_to_the_mod_builtin_over_a_sequence():
    s = xml(convert_one("r = a % b"))

    assert "ml:mod" not in s
    assert 'labels="FUNCTION"' in s and ">mod</ml:id>" in s
    assert "<ml:sequence>" in s


# ---------------------------------------------------------------------------
# Lists / matrices, transpose
# ---------------------------------------------------------------------------


def test_flat_list_is_column_vector():
    elem = convert_one("v = [1, 2, 3]")
    s = xml(elem)
    assert 'cols="1"' in s
    assert 'rows="3"' in s


def test_nested_list_is_matrix():
    elem = convert_one("m = [[1, 2], [3, 4]]")
    s = xml(elem)
    assert 'rows="2"' in s
    assert 'cols="2"' in s


def test_transpose():
    elem = convert_one("y = M.T")
    assert "ml:transpose" in xml(elem)


# ---------------------------------------------------------------------------
# Control flow: function def, if/else, for, while, lambda
# ---------------------------------------------------------------------------


def test_function_def_with_default_and_if_else():
    code = (
        "def f(x, y=2):\n"
        "    if x > 0:\n"
        "        z = x + y\n"
        "    else:\n"
        "        z = x - y\n"
        "    return z\n"
    )
    elem = convert_one(code)
    s = xml(elem)
    assert "ml:function" in s
    assert "ml:if" in s
    assert "ml:then" in s
    assert "ml:else" in s
    assert "ml:localDefine" in s


def test_for_loop():
    code = "s = 0\nfor i in range(3):\n    s = s + i\n"
    elems = convert_python_to_math50(code)
    assert len(elems) == 2
    s = xml(elems[1])
    assert "ml:for" in s


def test_while_loop():
    code = "i = 0\nwhile i < 3:\n    i = i + 1\n"
    elems = convert_python_to_math50(code)
    s = xml(elems[1])
    assert "ml:while" in s


def test_lambda():
    elem = convert_one("f = lambda x: x + 1")
    assert "ml:lambda" in xml(elem)


# ---------------------------------------------------------------------------
# Skips + warnings for unsupported constructs
# ---------------------------------------------------------------------------


def test_class_def_skipped_with_warning():
    with pytest.warns(UserWarning, match="Skipping class definition"):
        elems = convert_python_to_math50("class Foo:\n    pass\n")
    assert elems == []


def test_import_skipped_with_warning():
    with pytest.warns(UserWarning, match="Skipping import statement"):
        elems = convert_python_to_math50("import math\n")
    assert elems == []


def test_call_with_keyword_args_warns():
    with pytest.warns(UserWarning, match="keyword arguments"):
        convert_python_to_math50("y = f(x, mode='fast')\n")


# ---------------------------------------------------------------------------
# create_variable_id: subscript / greek-letter naming
# ---------------------------------------------------------------------------


def test_plain_variable_id():
    elem = create_variable_id("A")
    assert elem.text == "A"
    assert elem.get("labels") == "VARIABLE"


def test_variable_id_with_subscript():
    elem = create_variable_id("A_v")
    s = xml(elem)
    assert "Span" in s
    assert "Subscript" in s
    assert ">A<" in s
    assert ">v<" in s


def test_variable_id_with_multi_part_subscript_collapses_underscores():
    elem = create_variable_id("A_v_min")
    s = xml(elem)
    assert ">vmin<" in s


def test_variable_id_greek_base_converted():
    elem = create_variable_id("gamma_M0")
    s = xml(elem)
    assert "γ" in s  # γ
    assert ">M0<" in s


def test_variable_id_plain_greek():
    elem = create_variable_id("chi")
    assert elem.text == "χ"  # χ


def test_variable_id_bar_prefix_stripped():
    elem = create_variable_id("bar_x")
    assert elem.text == "x"


# ---------------------------------------------------------------------------
# parse_unit_string
# ---------------------------------------------------------------------------


def test_parse_unit_simple():
    elem = parse_unit_string("m")
    assert elem.get("labels") == "UNIT"
    assert elem.text == "m"


def test_parse_unit_power():
    elem = parse_unit_string("m^2")
    s = xml(elem)
    assert "ml:pow" in s
    assert ">2<" in s


def test_parse_unit_division():
    elem = parse_unit_string("m/s")
    s = xml(elem)
    assert "ml:div" in s


def test_parse_unit_multiplication():
    elem = parse_unit_string("kN*m")
    s = xml(elem)
    assert "ml:mult" in s


def test_parse_unit_compound_division_and_power():
    elem = parse_unit_string("m/s^2")
    s = xml(elem)
    assert "ml:div" in s
    assert "ml:pow" in s
