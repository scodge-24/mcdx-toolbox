---
name: mathcad-authoring
description: Create, edit, import, transcribe, explain and review Mathcad Prime worksheets using semantic YAML and pymcdx. Use for worksheet calculations, prose and inline mathematics, page layout, figures, turning PDFs, scans or hand calculations into worksheets or templates, explaining the method inside existing worksheets, validation and optional real Prime visual review.
---

# Mathcad worksheet authoring

Use the workflow: inspect/import → author/edit → validate → layout-check → render
in Prime → visually inspect. Use the installed `pymcdx` CLI or this plugin's MCP
tools. When working from the plugin checkout, `uv run --project <plugin-directory>
--locked pymcdx ...` runs its pinned environment. Do not assume a developer checkout.

For a new worksheet, start from the minimal example in
[Semantic authoring](references/authoring.md). For an existing worksheet, inspect its
outline and formatting, then import it as YAML; read all unsupported-region and
approximation warnings before editing. Preserve the original worksheet.

Two other jobs have their own references. To rebuild a PDF, scan, photo or hand
calculation as a worksheet or template, read
[Transcribing documents](references/transcribing-sources.md). To explain,
document or compare the method inside existing worksheets without changing
them, read [Reading existing worksheets](references/reading-existing-sheets.md).

## Author document intent

Prefer semantic YAML over a one-off coordinate generator or raw package edits.
Use ordered heading, text, math, function, image, check, summary, contents and
page-break blocks. Read [Semantic authoring](references/authoring.md) for the public
schema and examples. Unknown blocks and expressions should fail clearly; do not
inject XML to conceal unsupported semantics.

Keep compact related prose and inline maths together. Put substantial definitions,
fractions and evaluated results on their own paragraph rows or in display math.
Do not pack an entire derivation into one prose line. Use inline math even for
bare variable references, especially subscripted names. A bare identifier without
`unit` or `evaluate` is notation only. Only `inline_math` runs display results:
request one with `unit` or `evaluate: true`. A `math` block cannot; to show a
definition with its value, put the definition in a text block as a single
`inline_math` run with `unit`. See
[Showing results](references/authoring.md#showing-results).

Reuse the built-in named styles listed in the
[authoring reference](references/authoring.md#styles). `indent: 0..4` establishes hierarchy; `width: half` styles
allow paired groups. Use the same indent in paired columns. Keep `note` styling
for actual caveats. Document metadata supplies the repeating header; do not invent
authors, approval names, calculation references or dates. The built-in profile
uses a 40 mm top margin; preserve it unless changing and checking the page design.
Headings are numbered automatically; do not type numbering into their text.

Use `sqrt(x)` and `root(x, n)` for radicals. An underscore starts a subscript.
Only a small set of SI units is recognised; see
[Units](references/authoring.md#units). An unrecognised unit such as `s` becomes
an undefined variable without failing the build. Avoid variable names that
collide with units such as `m`, `N` or `kN`. Write
user functions as `function` blocks holding a supported Python `def`, above their
callers; do not put `def` or `lambda` in ordinary math. `if_(test, a, b)` expresses
the built-in conditional; a Python conditional expression becomes a program.

Images are PNG files referenced relative to the YAML, with optional captions and
`width_mm`. Use `pymcdx figure` to prepare a licensed or user-owned image, inspect
the result, then let layout size and place it. Confirm legibility in the final
render. Do not bundle third-party figures without redistribution permission.

## Build and inspect evidence

Read [Worksheet workflow and host setup](references/workflow.md) when importing,
running CLI/MCP tools, configuring Windows/WSL, or troubleshooting a render.

```bash
pymcdx build calculation.yaml calculation.mcdx --report build.json --preview layout.svg
pymcdx audit calculation.mcdx
pymcdx layout-check calculation.mcdx --json --strict
pymcdx inspect calculation.mcdx --outline --full
pymcdx inspect calculation.mcdx --formatting
```

Read the build report: validation, layout findings, warnings and estimation flags.
The outline is a semantic digest. Formatting inspection reports stored boxes,
styles and inline order, not final wrapping. Layout preview estimates geometry;
it is not a Prime render. Schema-valid does not imply Prime-valid.

When a Windows host has Prime/Express, render every page and inspect the PNGs:

Read [Prime render visual checks](references/prime-render-visual-check.md) before accepting a generated or edited worksheet, or changing code that shapes its presentation.

```bash
pymcdx render calculation.mcdx -o calculation.render --resave accepted.mcdx
pymcdx render-diff previous.render calculation.render -o differences
```

An optional `--pdf calculation.pdf` assembles a raster PDF; its text is not
selectable. The real Prime render is the presentation acceptance oracle. Check
cropping, overlap, wrapping, units and page breaks. Fix the YAML or compiler,
not the rendered image. Prime may be unavailable on the current host: report
that visual acceptance is pending, never substitute an SVG as that evidence.

Do not hide, quit or kill a pre-existing or concurrently opened Prime session.
The renderer closes only its own uniquely staged worksheet. A timeout retains
recovery files and leaves Prime running; let the user resolve any remaining
dialog. Do not “clean up” by terminating every Prime process.

Express licence limits can produce watermarks, red regions and `= ?` for premium
features without implying a malformed package. A repair/open failure is a separate
format issue. Rendering does not independently verify numerical results. State
what was structurally checked, what was visually reviewed, and what numerical
verification was actually performed. Engineering calculations still require
appropriate independent engineering verification.

## Report gaps

When the toolbox can't do something the user needs, crashes, or behaves
differently from these references, tell the user and offer to draft a GitHub
issue. Read [Reporting issues](references/reporting-issues.md) for when to offer,
the privacy rules and the report skeletons. Never file one without the user's
go-ahead.

## Maintaining the distribution

Only when developing or releasing the tool, read
[Release validation](references/releasing.md) for package/plugin checks and
publication boundaries. It is not required for ordinary worksheet work.
