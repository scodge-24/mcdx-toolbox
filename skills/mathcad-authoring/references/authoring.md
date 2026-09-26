# Semantic worksheet authoring

YAML is data, not executable Python; expressions
are parsed into the supported Mathcad syntax, not executed by a Python interpreter.

## Minimal worksheet

Save this as `calculation.yaml`, then follow the [worksheet workflow](workflow.md):

```yaml
schema_version: 1
document:
  title: Synthetic worksheet example
worksheet:
  paper: A4
  orientation: portrait
  margins_mm: {left: 5, top: 40, right: 5, bottom: 12.5}
  grid: fine
blocks:
  - kind: heading
    level: 1
    text: A simple rectangle
  - kind: math
    expression: "width = 3 * mm"
  - kind: math
    expression: "height = 4 * mm"
  - kind: math
    expression: "area = width * height"
  - kind: text
    runs:
      - kind: text
        text: "Area: "
      - kind: inline_math
        expression: area
        unit: mm2
```

## Document structure

Required top-level keys are `schema_version: 1`, `document`, `worksheet`, and
`blocks`; `styles` is optional. `document` accepts title, project, revision,
author, calc_number, date, checked_by, approved_by and provenance. Omit unknown
sign-offs rather than inventing them. `worksheet` supports only A4 portrait with
`grid: fine` and four margins in mm. The repeating header is designed for a 40 mm
top margin.

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
`text`; either run can select a named style. A bare name without `evaluate` or
`unit` is only a mathematical citation.

Definitions use Python-shaped `=` and render as Mathcad definitions. Underscores
represent subscripts. `sqrt(x)` and `root(x, n)` generate radicals. Function
blocks must precede calls. Programs require an appropriate Prime licence;
Express may display errors for them.

## Showing results

A `math` block is a definition or expression only; it accepts no `unit` or
`evaluate` field. To display a value, use an `inline_math` run. `unit` shows the
result in that unit; `evaluate: true` shows a unitless result. The run may be a
definition, so a text block holding one run gives a display row that defines and
evaluates together:

```yaml
- kind: math
  expression: "w_d = 12 * kN/m"
- kind: text
  runs:
    - {kind: inline_math, expression: "M_Ed = w_d * L**2 / 8", unit: kNm}
- kind: text
  runs:
    - {kind: text, text: "Utilisation "}
    - {kind: inline_math, expression: "M_Ed / M_Rd", evaluate: true}
```

Prime computes every displayed value when the worksheet opens. The toolbox does
not evaluate expressions, so report no number you have not seen in a Prime
render or computed independently.

## Units

Recognised unit names are `mm cm m km`, `N kN MN`, `Pa kPa MPa GPa`, `kg`, the
powers `mm2 cm2 m2`, `mm3 cm3 m3`, `mm4 cm4 m4`, `mm6 cm6 m6`, and the shorthands
`Nm kNm MNm kNm2`. Combine them with `*`, `/` and `**`, for example `kN/m` or
`kN/m**2`. A display `unit` containing any other name fails the build.

An unrecognised name in an expression does not fail: `5 * s` treats `s` as an
undefined variable, which Prime then flags. Keep to the recognised units, or
define values in a recognised unit and state the intended unit in prose. Do not
name variables after units (`m`, `N`, `kN`, `s`).

## Checks and summaries

`check` shows `label: utilisation ≤ limit` with the evaluated utilisation and
defines a verdict `Check_n := if(U ≤ limit, "OK", "NOT OK")`, numbered in
document order. `utilisation` is an expression string such as `M_Ed / M_Rd`.
`summary` repeats every preceding check and defines `U_max := max(...)`. Do not
define your own `Check_n` or `U_max`. The built-in `if()` evaluates in Express.

## Styles

The built-in `engineering` profile provides `title`, `heading1`, `heading2`,
`heading3`, `body`, `note`, `math`, `result`, `figure` and `half_body`. Headings
take `heading1`–`heading3` from their level (4–6 reuse `heading3`) and are
numbered automatically. A `style: title` heading is unnumbered; start the
numbered sections after it at level 1. Keep `note` for genuine caveats. Define extra named styles under
`styles:` and reference them from any block or run:

```yaml
styles:
  input:
    font_family: Arial
    font_size_pt: 10
    color: "#1F4E79"
    width: half
blocks:
  - {kind: math, expression: "L = 6 * m", style: input}
```

A style has `font_family`, `font_size_pt` and `color`, plus optional `bold`,
`italic`, `line_height`, `space_before_mm`, `space_after_mm` and
`width: full|half`. Two consecutive `half` blocks sit side by side. Indent is
0–4 fine grid steps. Use semantic styles and ordered blocks instead of absolute
coordinates. Layout is an estimate and may differ from Prime's actual
font/result measurements.

The Python API also accepts caller-supplied calculation providers through the
typed `CalculationProvider` protocol; no service or engine is bundled or contacted
by default. A calculation block needs an explicit Python provider. CLI/MCP users
should author ordinary expressions. Optional Python output checkers must supply
their own evaluator and evidence; absent a checker, no numerical check is claimed.

Import preserves supported expressions/text/styles/images and reports unsupported
regions. Review the generated YAML and warnings before rebuilding; arbitrary
Prime worksheets are not guaranteed lossless round-trips.
