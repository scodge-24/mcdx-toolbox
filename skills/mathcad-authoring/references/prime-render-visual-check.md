# Prime render visual checks

## Inspect the real output before accepting presentation

After generating or editing a worksheet, or changing code/templates that shape
one, render the affected worksheet through an installed Mathcad Prime/Express
and open every resulting page image with an available image-viewing tool:

```bash
pymcdx render calculation.mcdx -o calculation.render
```

Read the command's output for the page image paths. A successful command, an
existing PNG, or a render-diff score is not visual inspection. Check every page
for clipped regions, overlaps, text wrapping, legible formulae and figures,
units, headers, page breaks and unexpected blank pages. Inspect dense areas at
a readable scale. Fix the authoring source or generator and repeat the render
and inspection; do not repair the rendered image.

Use `layout-check` and `layout-preview` for cheap iteration, but they estimate
geometry. Schema validation does not prove Prime acceptance. If Prime is
unavailable, report structural/layout checks separately and leave real visual
acceptance pending. Never present an SVG preview as a Prime render.

## Licence and session boundaries

Mathcad Express may add a watermark and show red regions or `= ?` for premium
features. Distinguish confirmed licence limitations from malformed packages or
genuine calculation errors; do not assume every red region is licence-related.
An open/repair failure needs investigation. A visually correct render does not
independently verify numerical results or engineering correctness.

The rendering path uses Prime → XPS → PNG; optional PDF output assembles those
page images and is a raster PDF, not native searchable PDF export.

Prime's COM server can share the user's session. Never hide, quit or kill a
pre-existing or concurrently opened session. The renderer closes only its own
uniquely staged worksheet. On timeout, preserve recovery files and leave any
remaining dialog/session for the user; do not terminate all Prime processes.

## Probe uncertain format behaviour

Before implementing a presentation change based on an XML/XAML assumption,
build a minimal synthetic worksheet and a few variants that isolate the
uncertainty. Render and visually compare them before extending the generator.
Use crops or a side-by-side comparison for details such as number formatting,
table widths, header placement or spacing. Retain the original full-page renders
so a local improvement does not conceal a page-level regression.

Where alignment is ambiguous, use higher-resolution renders and pixel
measurements with controlled colours and identical stored positions. Record the
confirmed behaviour, Prime version/licence and reproducible example in the
project's format documentation. Keep assumptions distinct from observed results.

At handoff, identify the worksheet/render artifacts and pages inspected, any
remaining presentation problems, and the limits of numerical verification.
