"""Image blocks: read a PNG's size and density and fit it to the page.

See ``contract-py2m-image-block``. Prime draws the PNG bytes of a picture
region unchanged, so nothing here decodes or re-encodes the image.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_IMAGE_DPI = 120.0
"""Density assumed when a PNG records none: Prime shows a pasted screenshot at 0.8 scale."""

_PX_PER_MM = 96.0 / 25.4
_METRES_PER_INCH = 0.0254


class ImageError(ValueError):
    """An image block's file is missing, unreadable, or not a PNG."""


@dataclass(frozen=True, slots=True)
class PngInfo:
    """Pixel size and recorded density of a PNG file."""

    width: int
    height: int
    dpi: float | None


def read_png_info(data: bytes) -> PngInfo:
    """Parse the IHDR size and optional pHYs density of PNG bytes."""

    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ImageError("only PNG images are supported")
    width, height = struct.unpack(">II", data[16:24])
    if width == 0 or height == 0:
        raise ImageError("PNG has zero width or height")
    dpi: float | None = None
    offset = 8
    while offset + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[offset : offset + 8])
        if kind == b"pHYs" and length == 9:
            per_x, _per_y, unit = struct.unpack(">IIB", data[offset + 8 : offset + 17])
            if unit == 1 and per_x > 0:  # pixels per metre
                dpi = per_x * _METRES_PER_INCH
            break
        if kind == b"IDAT":
            break
        offset += 12 + length
    return PngInfo(width, height, dpi)


def load_png(path: Path) -> tuple[bytes, PngInfo]:
    """Read a PNG file and its size, with a readable error on failure."""

    try:
        data = path.read_bytes()
    except OSError as error:
        raise ImageError(f"cannot read image {str(path)!r}: {error.strerror}") from error
    try:
        return data, read_png_info(data)
    except ImageError as error:
        raise ImageError(f"{path}: {error}") from error


def image_display_size(
    info: PngInfo,
    *,
    width_mm: float | None,
    span_px: float,
    max_height_px: float,
) -> tuple[float, float]:
    """Display size in WPF pixels: natural or requested width, fitted to span and page."""

    natural = info.width * 96.0 / (info.dpi or DEFAULT_IMAGE_DPI)
    width = width_mm * _PX_PER_MM if width_mm is not None else natural
    aspect = info.height / info.width
    width = min(width, span_px, max_height_px / aspect)
    return width, width * aspect


__all__ = [
    "DEFAULT_IMAGE_DPI",
    "ImageError",
    "PngInfo",
    "image_display_size",
    "load_png",
    "read_png_info",
]
