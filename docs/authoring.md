# Semantic worksheet authoring

Start with `examples/basic.yaml`. YAML is data, not executable Python; expressions
are parsed into the supported Mathcad syntax, not executed by a Python interpreter.

Required top-level keys are `schema_version: 1`, `document`, `worksheet`, and
`blocks`. `document` accepts title, project, revision, author, calc_number, date,
checked_by, approved_by and provenance. Omit unknown sign-offs rather than inventing
them. `worksheet` specifies A4 portrait, `grid: fine`, and four margins in mm.
The built-in `engineering` style profile supplies body, note and heading styles;
the repeating header is designed for a 40 mm top margin.

## Blocks

| kind | Main fields |
|---|---|
| heading | `text`, `level` (1–6), optional `style`, `id`, `indent` |
| text | ordered `runs`, optional `style`, `id`, `indent` |
| math | `expression`, optional `style`, `id`, `indent` |
| function | Python `source` containing one supported `def` |
| image | PNG `path`, optional `caption`, `width_mm`, `id`, `style`, `indent` |
| check | `label`, `utilisation`, optional `limit` (default 1), style/id/indent |
| summary | summarises preceding checks; optional style/id/indent |
| contents | optional heading `depth` |
| page_break | optional `id` |

Unknown fields and kinds are rejected. IDs must be unique. Inline math lives in
text runs: `{kind: inline_math, expression: area, unit: mm2}`. A `text` run has
`text`; either run can select a named style. `evaluate: true` requests a unitless
result. A bare name without evaluation or unit is only a mathematical citation.

Definitions use Python-shaped `=` and render as Mathcad definitions. Underscores
represent subscripts. Use explicit units and avoid naming variables after units.
`sqrt(x)` and `root(x, n)` generate radicals. Function blocks must precede calls.
Programs require an appropriate Prime licence; Express may display errors for them.

Styles specify `font_family`, `font_size_pt`, `color` and optional bold/italic,
line-height, spacing and `width: full|half`. Indent is 0–4 fine grid steps. Use
semantic styles and ordered blocks instead of absolute coordinates. Layout is an
estimate and may differ from Prime's actual font/result measurements.

The Python API also accepts caller-supplied calculation providers through the
typed `CalculationProvider` protocol; no service or engine is bundled or contacted
by default. A calculation block needs an explicit Python provider. CLI/MCP users
should author ordinary expressions. Optional Python output checkers must supply
their own evaluator and evidence; absent a checker, no numerical check is claimed.

Import preserves supported expressions/text/styles/images and reports unsupported
regions. Review the generated YAML and warnings before rebuilding; arbitrary
Prime worksheets are not guaranteed lossless round-trips.
