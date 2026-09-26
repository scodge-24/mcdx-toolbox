# Notices and provenance

Original Python/PowerShell code, project-authored format schemas, XML templates,
documentation and synthetic examples: Copyright 2026 Scott Hodge, MIT. The owner
confirmed authorship; prior moves between their repositories do not introduce a
third-party code dependency. Schemas are project approximations, not official PTC
validation schemas. No vendor manuals, worksheets, standards tables, font binaries
or proprietary images are bundled.

`pymcdx/data/fonts/layout-advance-widths.json` contains layout measurements made
from Liberation Sans 2.1.5. Source font digests are recorded in that file. It is
not a font and does not substitute for Prime's typography. Associated digitized
font data copyright 2010 Google Corporation and 2012 Red Hat, Inc.; see
`pymcdx/data/fonts/OFL-1.1.txt` for attribution and SIL Open Font License 1.1.
These data are not relicensed as MIT. No font binaries are shipped.

Runtime dependencies are installed separately: lxml, MCP Python SDK, Pillow,
Pydantic, pypdfium2 and PyYAML, plus their transitive dependencies. Their original
licences remain applicable. pypdfium2 supplies PDFium binaries through its own
distribution and includes its own third-party notices; do not strip them when
redistributing an environment. The lockfile pins the resolved distributions,
but is not a substitute for their licence files.

The candidate dependency metadata was reviewed on 2026-09-26. Runtime packages
use MIT, BSD, Apache, PSF, MIT-CMU or MPL-2.0 terms (certifi's certificate data).
This does not relicense them: preserve package notices, including the native
library notices shipped with lxml, Pillow, cryptography and pypdfium2. The core
code's MIT licence need not change to Apache merely because dependencies use it.
Re-review dependencies whenever the lockfile changes.

Mathcad and Mathcad Prime are PTC product names. The integration requires the
user's separately installed and licensed product. No PTC software is distributed
or sublicensed by this project; no affiliation or endorsement is implied.
