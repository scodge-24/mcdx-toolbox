"""Compare two directories of rendered page PNGs (``pymcdx render`` output).

After a generator or sheet change, this says which pages changed and where, so
only those pages need re-reading. Pages are matched by file name
(``page-NN.png``). A pixel counts as changed when any colour channel moves by
more than :data:`PIXEL_TOLERANCE`, which absorbs anti-aliasing and colour
rounding; a page counts as changed when the fraction of changed pixels exceeds
the caller's threshold. The Express watermark is drawn identically on every
render, so it cancels out. Repeated Prime renders of one sheet are
pixel-identical on this host (2026-09-24), so the tolerance only has to cover
cross-machine rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops, ImageDraw

PIXEL_TOLERANCE = 32
"""Largest per-channel difference (0-255) still treated as the same pixel."""

_PAGE_GLOB = "page-*.png"
_WHITE = (255, 255, 255)
_HIGHLIGHT = (220, 0, 0)
_FADE = 0.8  # how far the new page is blended towards white under the highlight

PageStatus = Literal["unchanged", "changed", "added", "removed"]
Box = tuple[int, int, int, int]


class RenderCompareError(ValueError):
    """Raised when the render directories cannot be compared."""


@dataclass(frozen=True)
class PageDiff:
    name: str
    status: PageStatus
    fraction: float = 0.0
    bbox: Box | None = None
    old_size: tuple[int, int] | None = None
    new_size: tuple[int, int] | None = None
    diff: Path | None = None

    def describe(self) -> str:
        if self.status != "changed":
            return f"{self.name}: {self.status}"
        parts = [f"{self.name}: changed {self.fraction:.3%} of pixels"]
        if self.old_size != self.new_size and self.old_size and self.new_size:
            parts.append(
                f"size {self.old_size[0]}x{self.old_size[1]} -> "
                f"{self.new_size[0]}x{self.new_size[1]}"
            )
        if self.bbox is not None:
            parts.append(f"bbox {self.bbox}")
        if self.diff is not None:
            parts.append(f"diff {self.diff}")
        return ", ".join(parts)


@dataclass(frozen=True)
class CompareReport:
    pages: list[PageDiff]

    @property
    def changed(self) -> bool:
        return any(page.status != "unchanged" for page in self.pages)


def _page_names(directory: Path, label: str) -> set[str]:
    if not directory.is_dir():
        raise RenderCompareError(f"{label} directory {directory} not found")
    names = {page.name for page in directory.glob(_PAGE_GLOB)}
    if not names:
        raise RenderCompareError(
            f"{label} directory {directory} has no {_PAGE_GLOB} files (run pymcdx render first)"
        )
    return names


def _flatten(path: Path) -> Image.Image:
    """Load a page as opaque RGB, compositing any transparency onto white."""
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
    page = Image.new("RGB", rgba.size, _WHITE)
    page.paste(rgba, mask=rgba.getchannel("A"))
    return page


def _on_canvas(page: Image.Image, size: tuple[int, int]) -> Image.Image:
    if page.size == size:
        return page
    canvas = Image.new("RGB", size, _WHITE)
    canvas.paste(page, (0, 0))
    return canvas


def _changed_mask(old: Image.Image, new: Image.Image) -> Image.Image:
    """``L`` mask: 255 where any channel differs by more than the tolerance."""
    red, green, blue = ImageChops.difference(old, new).split()
    largest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return largest.point([255 if value > PIXEL_TOLERANCE else 0 for value in range(256)])


def _write_diff(new: Image.Image, mask: Image.Image, bbox: Box, target: Path) -> None:
    image = Image.blend(new, Image.new("RGB", new.size, _WHITE), _FADE)
    image.paste(Image.new("RGB", new.size, _HIGHLIGHT), mask=mask)
    ImageDraw.Draw(image).rectangle(
        (bbox[0] - 2, bbox[1] - 2, bbox[2] + 1, bbox[3] + 1), outline=_HIGHLIGHT, width=1
    )
    image.save(target)


def _compare_page(old_path: Path, new_path: Path, diff_dir: Path, threshold: float) -> PageDiff:
    old, new = _flatten(old_path), _flatten(new_path)
    size = (max(old.width, new.width), max(old.height, new.height))
    mask = _changed_mask(_on_canvas(old, size), _on_canvas(new, size))
    changed = mask.histogram()[255]
    fraction = changed / (size[0] * size[1])
    resized = old.size != new.size
    if fraction <= threshold and not resized:
        return PageDiff(new_path.name, "unchanged", fraction, old_size=old.size, new_size=new.size)
    bbox = mask.getbbox()
    diff = None
    if bbox is not None:
        diff_dir.mkdir(parents=True, exist_ok=True)
        diff = diff_dir / new_path.name
        _write_diff(_on_canvas(new, size), mask, bbox, diff)
    return PageDiff(new_path.name, "changed", fraction, bbox, old.size, new.size, diff)


def compare_renders(
    old_dir: Path, new_dir: Path, diff_dir: Path, *, threshold: float = 0.0
) -> CompareReport:
    """Compare ``old_dir`` and ``new_dir`` page by page, writing diffs to ``diff_dir``.

    ``threshold`` is the fraction of a page's pixels (0 <= T < 1) that may change
    before the page is reported as changed. A page present on one side only is
    ``added`` or ``removed``; pages of different pixel size are ``changed``.
    """
    if not 0.0 <= threshold < 1.0:
        raise RenderCompareError(f"threshold must be in [0, 1), got {threshold:g}")
    old_names = _page_names(old_dir, "old")
    new_names = _page_names(new_dir, "new")
    if diff_dir.resolve() in (old_dir.resolve(), new_dir.resolve()):
        raise RenderCompareError("the diff directory must differ from both render directories")
    if diff_dir.is_dir():
        for stale in diff_dir.glob(_PAGE_GLOB):
            stale.unlink()
    pages: list[PageDiff] = []
    for name in sorted(old_names | new_names):
        if name not in new_names:
            pages.append(PageDiff(name, "removed"))
        elif name not in old_names:
            pages.append(PageDiff(name, "added"))
        else:
            pages.append(_compare_page(old_dir / name, new_dir / name, diff_dir, threshold))
    return CompareReport(pages)
