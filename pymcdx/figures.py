"""Prepare image-block PNGs from PDF pages and other image formats.

See ``contract-py2m-figure-cli``. A PDF page is rendered at a known DPI and
tagged with it, so the figure places at its printed size; an image keeps its
pixels. Both are flattened onto white, cropped, and trimmed tight, because
white margins inside a PNG count as part of the placed picture.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import yaml  # type: ignore[reportMissingTypeStubs]
from PIL import Image, ImageChops, ImageDraw, UnidentifiedImageError

from pymcdx.authoring_images import PngInfo, image_display_size

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"})
PDF_DPI = 192.0
MIN_TRUSTED_DPI = 96.0
"""Recorded image DPI below this (JPEG's common 72) is a placeholder, not a scale."""

_MM_PER_INCH = 25.4
_WHITE_THRESHOLD = 245

Crop = tuple[float, float, float, float]


class FigureError(ValueError):
    """The figure source cannot be read, or the request does not fit it."""


@dataclass(frozen=True, slots=True)
class FigureResult:
    """The written PNG and the size an image block will place it at."""

    path: Path
    width_px: int
    height_px: int
    dpi: float | None
    display_width_px: float
    display_height_px: float


def _render_pdf_page(source: Path, page: int, dpi: float) -> Image.Image:
    try:
        document = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as error:
        raise FigureError(f"cannot read PDF {str(source)!r}: {error}") from error
    try:
        if not 1 <= page <= len(document):
            raise FigureError(f"page {page} is out of range: {source.name} has {len(document)}")
        # pypdfium2 annotates scale as int, but it takes (and needs) a float.
        page_image = document[page - 1].render(scale=dpi / 72.0)  # pyright: ignore[reportArgumentType]
        return page_image.to_pil()
    finally:
        document.close()


def _open_image(source: Path) -> tuple[Image.Image, float | None]:
    try:
        image = Image.open(source)
        image.load()
    except (OSError, UnidentifiedImageError) as error:
        raise FigureError(f"cannot read image {str(source)!r}: {error}") from error
    recorded = image.info.get("dpi")
    dpi = float(recorded[0]) if isinstance(recorded, tuple) and recorded else None
    return image, dpi if dpi is not None and dpi >= MIN_TRUSTED_DPI else None


def _flatten(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, rgba).convert("RGB")


def _trim(image: Image.Image, pad: int) -> Image.Image:
    ink = ImageChops.invert(image.convert("L")).point(
        [255 if value > 255 - _WHITE_THRESHOLD else 0 for value in range(256)]
    )
    box = ink.getbbox()
    if box is None:
        raise FigureError("the figure is blank (nothing darker than near-white)")
    content = image.crop(box)
    padded = Image.new("RGB", (content.width + 2 * pad, content.height + 2 * pad), "white")
    padded.paste(content, (pad, pad))
    return padded


def _grid_overlay(
    image: Image.Image, scale: float, unit: str, origin: tuple[float, float]
) -> Image.Image:
    """Draw a labelled grid in source crop units (10 mm or 50 px) for choosing --crop.

    ``origin`` is the crop's top-left in those units, so labels stay in
    page coordinates when the preview is itself cropped.
    """

    step = 10.0 if unit == "mm" else 50.0
    grid = image.convert("RGB")
    draw = ImageDraw.Draw(grid)
    for axis_length, start, vertical in (
        (grid.width, origin[0], True),
        (grid.height, origin[1], False),
    ):
        first = math.ceil(start / step)
        last = math.floor((start + axis_length / scale) / step)
        for index in range(first, last + 1):
            at = round((index * step - start) * scale)
            major = index % 2 == 0
            colour = (220, 40, 40) if major else (240, 150, 150)
            line = (at, 0, at, grid.height) if vertical else (0, at, grid.width, at)
            draw.line(line, fill=colour, width=1)
            if major:
                label = f"{index * step:g}"
                draw.text((at + 2, 2) if vertical else (2, at + 2), label, fill=(200, 0, 0))
    return grid


def prepare_figure(
    source: Path,
    output: Path,
    *,
    page: int = 1,
    crop: Crop | None = None,
    dpi: float = PDF_DPI,
    trim: bool = True,
    pad: int = 8,
    grid: bool = False,
) -> FigureResult:
    """Write a tight PNG for an image block from a PDF page or an image file.

    ``grid`` instead writes the (cropped) source overlaid with a labelled grid
    in crop units, untrimmed, for reading off --crop coordinates.
    """

    suffix = source.suffix.lower()
    if suffix == ".pdf":
        image = _render_pdf_page(source, page, dpi)
        out_dpi: float | None = dpi
        scale = dpi / _MM_PER_INCH  # crop is in millimetres
    elif suffix in IMAGE_SUFFIXES:
        image, out_dpi = _open_image(source)
        scale = 1.0  # crop is in pixels
    else:
        raise FigureError(f"unsupported figure source {source.suffix or source.name!r}")
    origin = (0.0, 0.0)
    if crop is not None:
        origin = (crop[0], crop[1])
        x0, y0, x1, y1 = (round(value * scale) for value in crop)
        if not (0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height):
            unit = "mm" if suffix == ".pdf" else "px"
            raise FigureError(
                f"crop {crop} {unit} lies outside the source "
                f"({image.width / scale:.1f} x {image.height / scale:.1f} {unit})"
            )
        image = image.crop((x0, y0, x1, y1))
    image = _flatten(image)
    if grid:
        image = _grid_overlay(image, scale, "mm" if suffix == ".pdf" else "px", origin)
    elif trim:
        image = _trim(image, pad)
    output.parent.mkdir(parents=True, exist_ok=True)
    if out_dpi is None:
        image.save(output, format="PNG", optimize=True)
    else:
        image.save(output, format="PNG", optimize=True, dpi=(out_dpi, out_dpi))
    width, height = image_display_size(
        PngInfo(image.width, image.height, out_dpi),
        width_mm=None,
        span_px=float("inf"),
        max_height_px=float("inf"),
    )
    return FigureResult(output, image.width, image.height, out_dpi, width, height)


MANIFEST_SUFFIX = ".figures.yaml"
_MANIFEST_KEYS = frozenset({"output", "source", "page", "crop", "dpi", "trim", "pad"})


class ManifestError(ValueError):
    """A figures manifest cannot be read or one of its entries is invalid."""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One validated figure: where it comes from and how to cut it."""

    index: int
    output: Path
    source: Path
    page: int
    crop: Crop | None
    dpi: float
    trim: bool
    pad: int


@dataclass(frozen=True, slots=True)
class ManifestOutcome:
    """One manifest entry's written figure, or the error that stopped it."""

    entry: ManifestEntry
    result: FigureResult | None
    error: str | None


def _read_manifest_entries(path: Path) -> list[object]:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"cannot read manifest {str(path)!r}: {error.strerror}") from error
    except yaml.YAMLError as error:
        raise ManifestError(f"manifest {str(path)!r} is not valid YAML: {error}") from error
    if not isinstance(loaded, Mapping) or set(loaded) != {"figures"}:
        raise ManifestError(f"manifest {str(path)!r} must be a mapping with one key, 'figures'")
    figures: object = loaded["figures"]
    if not isinstance(figures, list) or not figures:
        raise ManifestError(f"manifest {str(path)!r}: 'figures' must be a non-empty list")
    return list(figures)


def _number(raw: object) -> float | None:
    """A finite number from YAML (PyYAML reads ``1e-3`` as a string)."""

    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _validate_entry(index: int, raw: object, base: Path) -> ManifestEntry:
    """Check one entry; every problem names the entry's index."""

    def fail(message: str) -> ManifestError:
        return ManifestError(f"entry {index}: {message}")

    if not isinstance(raw, Mapping):
        raise fail("must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in _MANIFEST_KEYS)
    if unknown:
        raise fail(f"unknown key(s) {', '.join(unknown)}")
    output, source = raw.get("output"), raw.get("source")
    if not isinstance(output, str) or not output:
        raise fail("'output' must be a PNG path")
    if not isinstance(source, str) or not source:
        raise fail("'source' must be a file path")
    source_path = Path(os.path.expandvars(source)).expanduser()
    if not source_path.is_absolute():
        source_path = base / source_path
    if not source_path.is_file():
        raise fail(f"source {str(source_path)!r} does not exist")
    page = raw.get("page", 1)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise fail("'page' must be a positive integer")
    crop: Crop | None = None
    if "crop" in raw:
        values: object = raw["crop"]
        numbers = [_number(value) for value in values] if isinstance(values, list) else []
        checked = [number for number in numbers if number is not None]
        if len(checked) != 4 or len(numbers) != 4:
            raise fail("'crop' must be four numbers X0 Y0 X1 Y1")
        crop = (checked[0], checked[1], checked[2], checked[3])
    dpi = _number(raw.get("dpi", PDF_DPI))
    if dpi is None or dpi <= 0:
        raise fail("'dpi' must be a positive number")
    trim, pad = raw.get("trim", True), raw.get("pad", 8)
    if not isinstance(trim, bool):
        raise fail("'trim' must be true or false")
    if isinstance(pad, bool) or not isinstance(pad, int) or pad < 0:
        raise fail("'pad' must be a non-negative integer")
    return ManifestEntry(index, base / output, source_path, page, crop, dpi, trim, pad)


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Validate a figures manifest; outputs resolve against the manifest's folder.

    Sources expand ``~`` and environment variables. Every invalid entry is
    reported by index before any figure is written.
    """

    entries: list[ManifestEntry] = []
    problems: list[str] = []
    for index, raw in enumerate(_read_manifest_entries(path)):
        try:
            entries.append(_validate_entry(index, raw, path.parent))
        except ManifestError as error:
            problems.append(str(error))
    if problems:
        raise ManifestError(f"manifest {str(path)!r} is invalid: " + "; ".join(problems))
    return entries


def run_manifest(path: Path) -> list[ManifestOutcome]:
    """Regenerate every figure a manifest lists (``contract-py2m-figure-manifest``).

    A failing entry is recorded and the rest still run; the caller turns any
    failure into a non-zero exit.
    """

    outcomes: list[ManifestOutcome] = []
    for entry in load_manifest(path):
        try:
            result = prepare_figure(
                entry.source,
                entry.output,
                page=entry.page,
                crop=entry.crop,
                dpi=entry.dpi,
                trim=entry.trim,
                pad=entry.pad,
            )
        except FigureError as error:
            outcomes.append(ManifestOutcome(entry, None, str(error)))
        else:
            outcomes.append(ManifestOutcome(entry, result, None))
    return outcomes


def manifest_for_output(image: Path, directory: Path) -> Path | None:
    """Return the ``*.figures.yaml`` in ``directory`` that lists ``image`` as an output.

    Lets a build whose picture is missing name the command that regenerates
    it. Unreadable manifests are skipped here; running them reports why.
    """

    target = image.resolve()
    for manifest in sorted(directory.glob(f"*{MANIFEST_SUFFIX}")):
        try:
            figures = _read_manifest_entries(manifest)
        except ManifestError:
            continue
        for raw in figures:
            output = raw.get("output") if isinstance(raw, Mapping) else None
            if isinstance(output, str) and (manifest.parent / output).resolve() == target:
                return manifest
    return None


__all__ = [
    "MANIFEST_SUFFIX",
    "FigureError",
    "FigureResult",
    "ManifestEntry",
    "ManifestError",
    "ManifestOutcome",
    "load_manifest",
    "manifest_for_output",
    "prepare_figure",
    "run_manifest",
]
