"""Worksheet layout geometry, lint, and SVG preview.

Coordinates in a `.mcdx` are **WPF device-independent pixels (1/96 inch)** and are
**content-relative** — the origin is the top-left margin, not the paper corner —
and `top` is **continuous across pages**. See
`docs/format/worksheet-layout-and-authoring.md` for the decoded format.

This module gives agents two Mathcad-free ways to check a generated worksheet's
layout: `lint_layout` (compute overlaps / out-of-bounds / overflow from
coordinates) and `render_svg` (draw the page geometry for visual inspection).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import lxml.etree as etree

# --- unit conversion (WPF device-independent px = 1/96 inch) ------------------
PX_PER_MM = 96.0 / 25.4
MM_PER_PX = 25.4 / 96.0

_WS_NS = "http://schemas.mathsoft.com/worksheet50"
_PRES_NS = "http://schemas.ptc.com/mathcad/settings/presentation10"
_DEFAULT_MATH_FONT_SIZE_PX = 11.0 * 96.0 / 72.0
_MATH_ASCENT_RATIO = 9.0 / 11.0

# paper-code -> (width_mm, height_mm) in portrait. Dimensions are NOT stored in
# the file (only the code), so we maintain the table. [CONFIRMED codes: A4/A3/A5]
PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6),
    "Tabloid": (279.4, 431.8),
}

# Named margin presets (left, top, right, bottom) in mm. [UNCONFIRMED] — Prime
# Express can't set Custom margins, so these are best-guess placeholders pending
# calibration on the full Prime install. Only `margin-type="Custom"` stores an
# explicit `page-margin`; for a preset we fall back to this table and flag it.
MARGIN_PRESETS_MM: dict[str, tuple[float, float, float, float]] = {
    "Narrow": (12.7, 12.7, 12.7, 12.7),
    "Normal": (25.4, 25.4, 25.4, 25.4),
    "Wide": (50.8, 25.4, 50.8, 25.4),
}
_DEFAULT_MARGIN_MM = (19.05, 25.4, 19.05, 25.4)


@dataclass(frozen=True)
class Page:
    """Page geometry, all lengths in px unless named `_mm`."""

    paper_code: str
    orientation: str
    width_px: float
    height_px: float
    margin_left: float
    margin_top: float
    margin_right: float
    margin_bottom: float
    margins_confirmed: bool  # False when derived from the unconfirmed preset table
    math_font_size_px: float = _DEFAULT_MATH_FONT_SIZE_PX

    @property
    def content_width(self) -> float:
        return self.width_px - self.margin_left - self.margin_right

    @property
    def content_height(self) -> float:
        """Per-page content height; also the approximate continuous-`top` stride."""
        return self.height_px - self.margin_top - self.margin_bottom


@dataclass(frozen=True)
class Region:
    """A top-level worksheet region box (content-relative px)."""

    region_id: str
    kind: str
    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height


@dataclass(frozen=True)
class LintFinding:
    region_id: str
    check: str
    message: str


# --- loading ------------------------------------------------------------------


def _read_part(path: Path, part: str) -> bytes | None:
    from pymcdx.opc import OpcPackageReader

    with OpcPackageReader(path) as reader:
        try:
            return reader.read_part(part)
        except KeyError:
            return None


def load_page(path: Path) -> Page:
    """Parse page geometry from `settings/presentation.xml`."""
    raw = _read_part(path, "/mathcad/settings/presentation.xml")
    if raw is None:
        raise ValueError(f"{path}: no presentation.xml (not a Mathcad Prime package?)")
    root = etree.fromstring(raw)
    pm = root.find(f".//{{{_PRES_NS}}}pageModel")
    if pm is None:
        raise ValueError(f"{path}: no <pageModel> in presentation.xml")

    paper_code = pm.get("paper-code", "A4")
    orientation = pm.get("orientation", "Portrait")
    w_mm, h_mm = PAPER_SIZES_MM.get(paper_code, PAPER_SIZES_MM["A4"])
    if orientation.lower().startswith("land"):
        w_mm, h_mm = h_mm, w_mm

    page_margin = pm.get("page-margin")
    if page_margin:  # margin-type="Custom" — explicit, in px, order L,T,R,B
        left, top, right, bottom = (float(v) for v in page_margin.split(","))
        confirmed = True
    else:  # named preset — fall back to the unconfirmed table
        preset = pm.get("margin-type", "Normal")
        l_mm, t_mm, r_mm, b_mm = MARGIN_PRESETS_MM.get(preset, _DEFAULT_MARGIN_MM)
        left, top, right, bottom = (v * PX_PER_MM for v in (l_mm, t_mm, r_mm, b_mm))
        confirmed = False

    variable_style = root.find(f".//{{{_PRES_NS}}}mathStyle[@name='Variable']")
    math_font_size = (
        _DEFAULT_MATH_FONT_SIZE_PX
        if variable_style is None
        else float(variable_style.get("font-size", _DEFAULT_MATH_FONT_SIZE_PX))
    )

    return Page(
        paper_code=paper_code,
        orientation=orientation,
        width_px=w_mm * PX_PER_MM,
        height_px=h_mm * PX_PER_MM,
        margin_left=left,
        margin_top=top,
        margin_right=right,
        margin_bottom=bottom,
        margins_confirmed=confirmed,
        math_font_size_px=math_font_size,
    )


def _region_kind(el: etree._Element) -> str:
    for child in el:
        tag = etree.QName(child).localname
        if tag in {"text", "math", "picture", "pageBreak", "Area"}:
            return tag
    return "unknown"


def math_anchor_offset(font_size_px: float) -> float:
    """Prime selection-box ascent above a math region's grid anchor."""

    return font_size_px * _MATH_ASCENT_RATIO


def math_visual_top(anchor_top: float, font_size_px: float) -> float:
    """Convert Prime's math grid anchor to the displayed selection-box top."""

    return anchor_top - math_anchor_offset(font_size_px)


def load_regions(path: Path, *, page: Page | None = None) -> list[Region]:
    """Top-level regions (direct children of the worksheet's <regions>).

    Nested regions (inside an Area or inline in a text region) use parent-relative
    coordinates and are intentionally left to a later pass; top-level regions carry
    the absolute content-relative boxes that layout checks care about.
    """
    raw = _read_part(path, "/mathcad/worksheet.xml")
    if raw is None:
        raise ValueError(f"{path}: no worksheet.xml")
    root = etree.fromstring(raw)
    regions_el = root.find(f"{{{_WS_NS}}}regions")
    if regions_el is None:
        return []

    active_page = page or load_page(path)
    out: list[Region] = []
    for el in regions_el.findall(f"{{{_WS_NS}}}region"):
        left = el.get("left")
        top = el.get("top")
        if left is None or top is None:
            continue  # inline/nested region without an absolute box
        width = el.get("actualWidth") or el.get("width") or "0"
        height = el.get("actualHeight") or el.get("height") or "0"
        kind = _region_kind(el)
        anchor_top = float(top)
        visual_top = (
            math_visual_top(anchor_top, active_page.math_font_size_px)
            if kind == "math"
            else anchor_top
        )
        out.append(
            Region(
                region_id=el.get("region-id", "?"),
                kind=kind,
                left=float(left),
                top=visual_top,
                width=float(width),
                height=float(height),
            )
        )
    return out


# --- lint ---------------------------------------------------------------------


def _overlaps(a: Region, b: Region, tol: float = 1.0) -> bool:
    return (
        a.left < b.right - tol
        and b.left < a.right - tol
        and a.top < b.bottom - tol
        and b.top < a.bottom - tol
    )


def lint_layout(page: Page, regions: list[Region]) -> list[LintFinding]:
    """Compute layout problems from coordinates alone — no Mathcad, no render."""
    findings: list[LintFinding] = []
    if not page.margins_confirmed:
        findings.append(
            LintFinding(
                region_id="-",
                check="margins-unconfirmed",
                message=(
                    "page margins come from the unconfirmed preset table; "
                    "content-bounds checks are approximate (overlaps are exact)"
                ),
            )
        )

    boxed = [r for r in regions if r.kind != "pageBreak"]
    for r in boxed:
        if r.width <= 0 or r.height <= 0:
            findings.append(
                LintFinding(
                    r.region_id, "zero-size", f"non-positive size {r.width:.1f}×{r.height:.1f}"
                )
            )
        if r.left < -0.5 or r.top < -0.5:
            findings.append(
                LintFinding(
                    r.region_id, "out-of-bounds", f"negative origin ({r.left:.1f}, {r.top:.1f})"
                )
            )
        if r.right > page.content_width + 0.5:
            over = r.right - page.content_width
            findings.append(
                LintFinding(
                    r.region_id,
                    "horizontal-overflow",
                    f"extends {over:.1f}px past the {page.content_width:.1f}px content width",
                )
            )

    for i, a in enumerate(boxed):
        for b in boxed[i + 1 :]:
            if _overlaps(a, b):
                findings.append(
                    LintFinding(a.region_id, "overlap", f"overlaps region {b.region_id}")
                )
    return findings


# --- SVG preview --------------------------------------------------------------

_KIND_FILL = {
    "text": "#dbeafe",
    "math": "#dcfce7",
    "picture": "#fef9c3",
    "Area": "#f3e8ff",
    "unknown": "#f1f5f9",
}


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(page: Page, regions: list[Region], *, gap: float = 24.0) -> str:
    """Render the page(s) as a standalone SVG — paper, content box, region boxes.

    Region `top` is continuous across pages; each page is drawn stacked vertically.
    Geometry is truthful; typography is not rendered (boxes carry id + kind labels).
    """
    boxed = [r for r in regions if r.kind != "pageBreak"]
    stride = page.content_height if page.content_height > 0 else page.height_px
    n_pages = 1
    for r in boxed:
        n_pages = max(n_pages, int(r.bottom // stride) + 1)

    total_h = n_pages * page.height_px + (n_pages - 1) * gap
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{page.width_px:.0f}" '
        f'height="{total_h:.0f}" viewBox="0 0 {page.width_px:.0f} {total_h:.0f}" '
        f'font-family="sans-serif" font-size="9">',
        "<style>text{fill:#334155} .lbl{fill:#475569}</style>",
    ]

    for p in range(n_pages):
        page_y = p * (page.height_px + gap)
        parts.append(
            f'<rect x="0" y="{page_y:.1f}" width="{page.width_px:.1f}" '
            f'height="{page.height_px:.1f}" fill="white" stroke="#94a3b8"/>'
        )
        # content rectangle (dashed)
        parts.append(
            f'<rect x="{page.margin_left:.1f}" y="{page_y + page.margin_top:.1f}" '
            f'width="{page.content_width:.1f}" height="{page.content_height:.1f}" '
            f'fill="none" stroke="#cbd5e1" stroke-dasharray="4 3"/>'
        )
        parts.append(
            f'<text x="4" y="{page_y + 12:.1f}" class="lbl">page {p + 1} · '
            f"{_esc(page.paper_code)} {_esc(page.orientation)}</text>"
        )

    for r in boxed:
        p = int(r.top // stride)
        page_y = p * (page.height_px + gap)
        x = page.margin_left + r.left
        y = page_y + page.margin_top + (r.top - p * stride)
        fill = _KIND_FILL.get(r.kind, _KIND_FILL["unknown"])
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{r.width:.1f}" height="{r.height:.1f}" '
            f'fill="{fill}" fill-opacity="0.7" stroke="#64748b" stroke-width="0.6"/>'
        )
        parts.append(
            f'<text x="{x + 2:.1f}" y="{y + 9:.1f}">{_esc(r.region_id)}·{_esc(r.kind)}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def summarize(path: Path) -> dict[str, object]:
    """One-call structured layout summary for agents (JSON-friendly)."""
    page = load_page(path)
    regions = load_regions(path, page=page)
    findings = lint_layout(page, regions)
    return {
        "paper": page.paper_code,
        "orientation": page.orientation,
        "content_width_px": round(page.content_width, 1),
        "content_height_px": round(page.content_height, 1),
        "margins_confirmed": page.margins_confirmed,
        "region_count": len(regions),
        "findings": [
            {"region": f.region_id, "check": f.check, "message": f.message} for f in findings
        ],
    }
