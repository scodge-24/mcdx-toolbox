# Transcribing documents into worksheets

Use this when the user supplies a PDF, scan, photo, hand calculation or other
document and wants it rebuilt as a live Mathcad worksheet or reusable template.
The loop is: **read the source → crop what must be reused → author → verify
values → render and look → report what was uncertain.**

## Read the source faithfully

- Keep the source's symbols, subscripts, clause and equation numbers, and units.
  A reader should be able to put the worksheet beside the source and match them
  line by line.
- Record where each input and formula came from (document, page, clause or
  equation number) in prose next to it, or in `document.provenance`.
- Never guess an illegible digit, symbol, exponent or unit. Put a visible
  placeholder in the worksheet, for example a `note` reading "value illegible in
  source, page 3", and list every such item in your handoff.
- Keep the source's method, even where you would do it differently. Suggest
  changes separately; don't silently correct the source.
- Where a scan's text layer disagrees with the page image, trust the image.
  Scanned text layers often garble subscripts and Greek letters.

For a long PDF with a text layer, search it before paging through. The printed
page number is often not the PDF page index. This runs in the toolbox's own
environment, which already includes pypdfium2:

```bash
uv run --project <plugin-directory> python - <<'EOF'
import pypdfium2 as pdfium
doc = pdfium.PdfDocument("source.pdf")
for i in range(len(doc)):
    text = doc[i].get_textpage().get_text_range()
    for key in ("Figure 6.2", "Table 3.4", "6.2.5"):
        if key in text:
            print("pdf page", i + 1, key)
EOF
```

## Crop figures and tables with the grid

Never estimate crop coordinates from a scaled-down page image. Write the page
under a labelled grid (millimetres for a PDF, pixels for an image), read the
coordinates off it, and zoom where needed:

```bash
pymcdx figure source.pdf --page 26 --grid --dpi 90 -o p26-grid.png
pymcdx figure source.pdf --page 26 --crop 40 20 110 70 --grid -o p26-zoom.png
pymcdx figure source.pdf --page 26 --crop 43 25 104 63 -o figures/fig-6-2.png
```

The final crop is rendered at 192 dpi and tagged so it places at printed size,
then trimmed of white border. **Open the PNG and check all four edges.** Typical
faults, each fixed by moving one coordinate 1–3 mm:

| In the PNG | Fix |
|---|---|
| A label cut off at one side | widen that edge |
| A fragment of the caption or next line | move the edge inside it |
| A sliver of the next table row | pull the bottom edge up |
| A table's bottom border missing | push the bottom edge down |
| Stubs of an adjacent frame | tighten to within about 0.5 mm of the border |

Crop only the rows or parts the worksheet uses. Photos and screenshots work the
same way with pixel coordinates.

To regenerate a set of crops later (for example on another machine that has
the source), list them in a `<name>.figures.yaml` manifest beside the YAML and
run `pymcdx figure --manifest <name>.figures.yaml`:

```yaml
figures:
  - {output: figures/fig-6-2.png, source: ~/docs/source.pdf, page: 26, crop: [43, 25, 104, 63]}
```

**Copyright.** Only crop from material the user may copy. Extracts from paid
standards or licensed documents usually must not be committed or shared. Keep
them out of version control, keep the manifest so anyone with the source can
regenerate them, and add a note to the worksheet saying which figures are
extracts and from where.

## Author the worksheet like a designer

Follow the main skill and the [authoring reference](authoring.md). A structure
that reads well for a code-clause template:

1. An intro paragraph: what is checked, and to which document and clause.
2. A `contents` block for anything longer than a page or two.
3. **Design basis:** assumptions, partial factors and their sources, and a
   `note` for any national-annex or project-specific deviation.
4. **Inputs:** geometry, materials and actions, often in paired `half` columns
   with the same indent. For a template, make every input an obvious separate
   definition so the user can change it.
5. One numbered section per check, named with its clause, for example "Bearing
   resistance (Table 3.4)". The source figure or table first, then a lead-in
   sentence, the definitions with results, and a utilisation.
6. A `summary` block at the end.

Express limits as utilisations (`U = E_d / R_d`, pass at ≤ 1) so every check
reads the same way. Use `min(...)` and `max(...)` for "the smaller of" rules
rather than prose. Write symbols in prose as inline math, never as typed
Unicode look-alikes. Keep to [recognised units](authoring.md#units).

## Verify values independently

The toolbox does not evaluate expressions; Prime does, when the worksheet
opens. Before handing back, compute the case independently (a few lines of
plain Python are enough) and compare it with the values in a real Prime
render. If Prime is unavailable, say that the worksheet's displayed values are
unchecked. For a transcribed hand calculation, compare against the source's own
numbers too; a mismatch is either a transcription error or an error in the
source, and the user needs to know which.

## Render, look and report

Build, lint and render as in the main skill, then read every page against the
source. At handoff, list:

- the source pages or regions transcribed;
- every illegible or ambiguous item and the placeholder used;
- every place the worksheet deliberately departs from the source, and why;
- which figures are extracts and whether they may be shared;
- which values were checked independently and which were not.
