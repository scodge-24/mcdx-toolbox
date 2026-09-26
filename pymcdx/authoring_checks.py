"""Lower check and summary blocks to ordinary text blocks.

Each of these semantic blocks becomes exactly one :class:`TextBlock` with the
same id, so layout and rendering handle them like any other text region and
block indices stay stable. The lowering is idempotent: a document without
these kinds comes back unchanged.

- A check shows ``label: U = value ≤ limit`` and a verdict variable
  ``Check_n := if(U ≤ limit, "OK", "NOT OK") = "OK"``, numbered in document
  order. The verdict uses Mathcad's built-in ``if()`` function, not a program,
  so Prime Express evaluates it.
- A summary repeats every check above it (label, utilisation, verdict), then
  ``U_max := max(...) = value``.
"""

from __future__ import annotations

from collections.abc import Sequence

from pymcdx.authoring_models import (
    AuthoringDocument,
    Block,
    CheckBlock,
    InlineMathRun,
    SummaryBlock,
    TextBlock,
    TextRun,
    TextRunNode,
)

PASS = "OK"  # noqa: S105 - a check verdict, not a credential
FAIL = "NOT OK"
SUMMARY_NAME = "U_max"


def verdict(utilisation: float, limit: float = 1.0) -> str:
    """The Python meaning of a check's verdict variable."""

    return PASS if utilisation <= limit else FAIL


def verdict_name(ordinal: int) -> str:
    """The Mathcad variable holding the verdict of the ``ordinal``-th check (1-based)."""

    return f"Check_{ordinal}"


def _limit(value: int | float) -> str:
    return repr(float(value)) if isinstance(value, int) else repr(value)


def _math(expression: str, *, evaluate: bool = False) -> InlineMathRun:
    return InlineMathRun(kind="inline_math", expression=expression, evaluate=evaluate)


def check_row(block: CheckBlock, ordinal: int) -> TextBlock:
    """Lower one check block to its utilisation row and verdict row."""

    limit = _limit(block.limit)
    runs: list[TextRunNode] = [
        TextRun(kind="text", text=f"{block.label}: "),
        _math(block.utilisation, evaluate=True),
        TextRun(kind="text", text=f" ≤ {limit}\n"),
        _math(
            f"{verdict_name(ordinal)} = if_({block.utilisation} <= {limit}, {PASS!r}, {FAIL!r})",
            evaluate=True,
        ),
    ]
    return TextBlock(kind="text", id=block.id, style=block.style, indent=block.indent, runs=runs)


def summary_row(block: SummaryBlock, checks: Sequence[tuple[CheckBlock, int]]) -> TextBlock:
    """Lower a summary over the given (check, ordinal) pairs, in document order."""

    if not checks:
        raise ValueError("a summary needs at least one check above it")
    runs: list[TextRunNode] = []
    for check, ordinal in checks:
        runs += [
            TextRun(kind="text", text=f"{check.label}: "),
            _math(check.utilisation, evaluate=True),
            TextRun(kind="text", text="   "),
            _math(verdict_name(ordinal), evaluate=True),
            TextRun(kind="text", text="\n"),
        ]
    utilisations = ", ".join(check.utilisation for check, _ in checks)
    runs.append(_math(f"{SUMMARY_NAME} = max({utilisations})", evaluate=True))
    return TextBlock(kind="text", id=block.id, style=block.style, indent=block.indent, runs=runs)


def lower_blocks(document: AuthoringDocument) -> AuthoringDocument:
    """Replace check/summary blocks with their text blocks."""

    semantic = (CheckBlock, SummaryBlock)
    if not any(isinstance(block, semantic) for block in document.blocks):
        return document
    checks: list[tuple[CheckBlock, int]] = []
    lowered: list[Block] = []
    for block in document.blocks:
        if isinstance(block, CheckBlock):
            checks.append((block, len(checks) + 1))
            lowered.append(check_row(block, len(checks)))
        elif isinstance(block, SummaryBlock):
            lowered.append(summary_row(block, checks))
        else:
            lowered.append(block)
    return document.model_copy(update={"blocks": lowered})


__all__ = [
    "FAIL",
    "PASS",
    "SUMMARY_NAME",
    "check_row",
    "lower_blocks",
    "summary_row",
    "verdict",
    "verdict_name",
]
