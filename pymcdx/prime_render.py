"""Render a worksheet through a real Mathcad Prime install to page images.

This is the ground-truth visual check for generated or edited worksheets:
``layout-preview`` approximates geometry, this shows what Prime actually draws.
It needs Windows with Mathcad Prime, reached natively or over WSL interop.
Prime Express blocks PDF export, so pages go Prime → XPS → PNG, and
:func:`write_pdf` assembles those PNGs into a raster PDF for sharing.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lxml import etree
from PIL import Image

POWERSHELL = "powershell.exe"
_SCRIPT = Path(__file__).parent / "prime" / "render.ps1"
_XPS_NAME = "worksheet.xps"
_CORE_PART = "docProps/core.xml"
_DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"


class PrimeRenderError(RuntimeError):
    """Raised when a worksheet cannot be rendered through Mathcad Prime."""


@dataclass(frozen=True)
class RenderResult:
    xps: Path
    pages: list[Path]
    resaved: Path | None = None


def _to_windows(path: Path) -> str:
    """Return ``path`` as PowerShell sees it (translated under WSL)."""
    wslpath = shutil.which("wslpath")
    if wslpath is None:
        return str(path)
    return subprocess.run(  # noqa: S603
        [wslpath, "-w", str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()


def _windows_temp() -> Path:
    """A staging root Mathcad can open files from (not a \\\\wsl$ share)."""
    wslpath = shutil.which("wslpath")
    cmd = shutil.which("cmd.exe")
    if wslpath is None or cmd is None:
        return Path(tempfile.gettempdir())
    temp = subprocess.run(  # noqa: S603
        [cmd, "/c", "echo %TEMP%"], check=True, capture_output=True, text=True, cwd="/mnt/c"
    ).stdout.strip()
    return Path(
        subprocess.run(  # noqa: S603
            [wslpath, "-u", temp], check=True, capture_output=True, text=True
        ).stdout.strip()
    )


def render_pages(
    mcdx: Path,
    out_dir: Path,
    *,
    dpi: int = 110,
    timeout: float = 300,
    resave: Path | None = None,
) -> RenderResult:
    """Render every printed page of ``mcdx`` to ``out_dir/page-NN.png``."""
    if not mcdx.is_file():
        raise PrimeRenderError(f"{mcdx} not found")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 36 <= dpi <= 600:
        raise PrimeRenderError("dpi must be an integer from 36 to 600")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise PrimeRenderError("timeout must be finite and in (0, 3600] seconds")
    if resave is not None and (
        resave.resolve() == mcdx.resolve() or (resave.exists() and resave.samefile(mcdx))
    ):
        raise PrimeRenderError("resave output must differ from input")
    if out_dir.exists() and not out_dir.is_dir():
        raise PrimeRenderError("render output must be a directory")
    powershell = shutil.which(POWERSHELL)
    if powershell is None:
        raise PrimeRenderError(
            f"{POWERSHELL} not found; rendering needs Windows with Mathcad Prime (native or WSL)"
        )

    stage = Path(tempfile.mkdtemp(prefix="pymcdx-render-", dir=_windows_temp()))
    preserve_stage = False
    try:
        staged = stage / "worksheet.mcdx"
        script = stage / _SCRIPT.name
        shutil.copyfile(mcdx, staged)
        shutil.copyfile(_SCRIPT, script)
        command = [
            powershell,
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            _to_windows(script),
            "-Mcdx",
            _to_windows(staged),
            "-OutDir",
            _to_windows(stage),
            "-Dpi",
            str(dpi),
        ]
        if resave is not None:
            command.append("-Resave")
        try:
            proc = subprocess.run(  # noqa: S603
                command, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            preserve_stage = True
            raise PrimeRenderError(
                f"Mathcad Prime did not finish within {timeout:g}s "
                f"; Prime was left untouched. Check its prompts; recovery files: {stage}"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise PrimeRenderError(f"Mathcad Prime render failed: {detail}")

        pages = sorted(stage.glob("page-*.png"))
        if not pages:
            raise PrimeRenderError("Mathcad Prime exported no pages")
        out_dir.mkdir(parents=True, exist_ok=True)
        for stale in out_dir.glob("page-*.png"):
            stale.unlink()
        xps = out_dir / _XPS_NAME
        shutil.copyfile(stage / _XPS_NAME, xps)
        written = []
        for page in pages:
            target = out_dir / page.name
            shutil.copyfile(page, target)
            written.append(target)
        if resave is not None:
            resave.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stage / "resaved.mcdx", resave)
        return RenderResult(xps=xps, pages=written, resaved=resave)
    finally:
        if not preserve_stage:
            shutil.rmtree(stage, ignore_errors=True)


def worksheet_title(mcdx: Path) -> str:
    """The package's ``dc:title``, or the file stem when it has none."""
    try:
        with zipfile.ZipFile(mcdx) as package:
            core = etree.fromstring(package.read(_CORE_PART))
    except (OSError, KeyError, zipfile.BadZipFile, etree.XMLSyntaxError):
        return mcdx.stem
    title = core.findtext(_DC_TITLE)
    return title.strip() if title and title.strip() else mcdx.stem


def _pdf_page(png: Path, dpi: float) -> tuple[Image.Image, tuple[float, float]]:
    """Load ``png`` as opaque RGB with the DPI stored in it (else ``dpi``)."""
    with Image.open(png) as image:
        stored = image.info.get("dpi")
        rgba = image.convert("RGBA")
    page = Image.new("RGB", rgba.size, (255, 255, 255))
    page.paste(rgba, mask=rgba.getchannel("A"))
    if isinstance(stored, tuple) and len(stored) == 2 and stored[0] > 0 and stored[1] > 0:
        return page, (float(stored[0]), float(stored[1]))
    return page, (dpi, dpi)


def write_pdf(pages: Sequence[Path], out: Path, *, title: str, dpi: float = 110) -> Path:
    """Assemble page PNGs, in order, into a raster PDF at ``out``.

    Each PDF page takes its PNG's physical size from the DPI stored in the PNG
    (``dpi`` when it has none), so an A4 sheet page is an A4 PDF page. The PDF is
    written to a temporary file and moved into place, so a failure leaves no
    partial PDF behind.
    """
    if out.is_dir():
        raise PrimeRenderError(f"PDF output {out} is a directory")
    if not pages:
        raise PrimeRenderError("no rendered pages to write to the PDF")
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, scratch = tempfile.mkstemp(suffix=".pdf", dir=out.parent)
    os.close(handle)
    temp = Path(scratch)
    try:
        for index, png in enumerate(pages):
            image, page_dpi = _pdf_page(png, dpi)
            image.save(temp, "PDF", dpi=page_dpi, title=title, append=index > 0)
        temp.replace(out)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return out
