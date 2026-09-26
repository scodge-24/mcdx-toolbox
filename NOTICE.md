# Notices and provenance

Original Python/PowerShell code, project-authored format schemas, XML templates,
documentation and synthetic examples: Copyright 2026 Scott Hodge, MIT. Schemas
are project approximations, not official PTC validation schemas. No vendor
manuals, worksheets, standards tables, font binaries or proprietary images are
bundled.

`pymcdx/data/fonts/layout-advance-widths.json` contains layout measurements made
from Liberation Sans 2.1.5. Source font digests are recorded in that file. It is
not a font and does not substitute for Prime's typography. Associated digitized
font data copyright 2010 Google Corporation and 2012 Red Hat, Inc.; see
`pymcdx/data/fonts/OFL-1.1.txt` for attribution and SIL Open Font License 1.1.
These data are not relicensed as MIT. No font binaries are shipped.

Runtime dependencies are installed separately: lxml, MCP Python SDK, Pillow,
Pydantic, pypdfium2 and PyYAML, plus their transitive dependencies. They remain
under their own licences (MIT, BSD, Apache, PSF, MIT-CMU, and MPL-2.0 for
certifi's certificate data). Preserve their notices when redistributing an
environment, including the native library notices shipped with lxml, Pillow,
cryptography and pypdfium2. pypdfium2 supplies PDFium binaries through its own
distribution with its own third-party notices. The lockfile pins the resolved
distributions but is not a substitute for their licence files.

Mathcad and Mathcad Prime are PTC product names. The integration requires the
user's separately installed and licensed product. No PTC software is distributed
or sublicensed by this project; no affiliation or endorsement is implied.
