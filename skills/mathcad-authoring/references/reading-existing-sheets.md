# Reading existing worksheets

Use this when the user wants to understand what a worksheet does, not change it:
write up its method as a procedure, trace what feeds a result, compare several
sheets, or check a sheet against a standard. Never modify the original file.

## Gather the evidence

```bash
pymcdx inspect sheet.mcdx --outline --full    # headings, prose, every expression in order
pymcdx inspect sheet.mcdx --formatting        # stored styles, boxes, inline order
pymcdx import-yaml sheet.mcdx -o sheet.yaml   # structured, searchable form; lists unsupported regions
pymcdx extract sheet.mcdx --region 13         # raw XML of one region, e.g. one import reported
```

The outline is the main source: it gives every heading, text region and
expression in document order, with definitions shown as `:=` and displayed
results as `⇒`. `import-yaml` gives the same content as YAML, which is easier to
search and cite, and reports each region it could not map by region id. Read
every unsupported region report. Those regions are gaps in what you can see, not
empty space; `extract --region` shows their raw XML.

To find where a name is used, search the imported YAML or the outline, not the
raw XML. `extract --grep` matches raw XML, where a subscripted name such as
`M_Ed` is stored as `M` and `Ed` in separate elements.

The outline and import show expressions, not the values Prime computed. To read
actual numbers, render the worksheet in Prime and read the page images (see
[Prime render visual checks](prime-render-visual-check.md)). If Prime is
unavailable, say that no values were read.

## Trace the method

Mathcad evaluates top to bottom, then left to right, so document order is
calculation order. For each result the user cares about:

1. Find its definition (`name :=`).
2. List the names on the right-hand side and find each one's definition, working
   back until you reach inputs: definitions with plain numbers and units.
3. Note any user function (`f(x) :=`) and read its body. Programs and function
   bodies may be summarised in the outline; use `extract` for the full XML when
   the detail matters.
4. Note any name used but never defined in the sheet. It may be a Mathcad
   built-in, or it may come from an included or referenced worksheet the file
   does not contain.
5. A later definition of the same name overrides an earlier one from that point
   on. Say so where it happens; it is a common source of error.

## Write it up

Match the user's request, but by default give:

- **Purpose and scope:** what the sheet checks, and to which standard or
  clauses, as stated in its own headings and prose.
- **Inputs:** each input with its value, unit, and where the sheet says it comes
  from.
- **Method:** numbered steps in calculation order. Give each step's formula in
  the sheet's own symbols, its clause reference if the sheet cites one, and what
  it feeds.
- **Checks:** each utilisation or pass/fail test and its limit.
- **Gaps:** unsupported regions, undefined names, redefinitions, and anything
  you inferred rather than read.

Keep what the sheet says separate from what you infer. If the sheet cites no
clause for a formula, don't supply one as if it did; offer it as your
suggestion. When comparing several sheets, align them step by step and list
differences in inputs, formulae, factors, clause editions and check limits.

## Limits

This is your reading of the sheet. The toolbox has no Mathcad-to-Python or
Mathcad-to-procedure converter. Import is not lossless, programs and some
built-ins may not map, and the outline shows no computed values. State which of
these limited the write-up, and don't present an inferred method as the sheet's
verified behaviour.
