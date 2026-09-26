"""Tests for pymcdx.render_compare through ``pymcdx render-diff`` (synthetic page PNGs)."""

import re
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pymcdx import worksheet_cli as cli
from pymcdx.render_compare import PIXEL_TOLERANCE, compare_renders

SIZE = (200, 280)


def _page(path: Path, *, size=SIZE, mark: tuple[int, int, int, int] | None = None, shade=0):
    """A white page with a grey 'watermark' band, text-like bars and an optional mark."""
    image = Image.new("RGBA", size, (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 120, size[0] - 1, 140), fill=(230 - shade, 230 - shade, 230 - shade, 255))
    for row in range(10, 100, 12):
        draw.rectangle((10, row, 150, row + 6), fill=(shade, shade, shade, 255))
    if mark is not None:
        draw.rectangle(mark, fill=(0, 0, 0, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _run(monkeypatch, capsys, *argv: str) -> tuple[int, list[str], str]:
    monkeypatch.setattr(sys, "argv", ["pymcdx", "render-diff", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err


@pytest.fixture
def dirs(tmp_path) -> tuple[Path, Path, Path]:
    return tmp_path / "old", tmp_path / "new", tmp_path / "diff"


def test_identical_renders_exit_zero(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    for directory in (old, new):
        _page(directory / "page-01.png")
        _page(directory / "page-02.png")
    code, lines, _ = _run(monkeypatch, capsys, str(old), str(new), "-o", str(diff))
    assert code == 0
    assert lines == ["page-01.png: unchanged", "page-02.png: unchanged"]
    assert not list(diff.glob("*.png"))


def test_antialias_noise_within_tolerance_is_not_change(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    _page(old / "page-01.png")
    _page(new / "page-01.png", shade=PIXEL_TOLERANCE)  # every glyph and the band shift
    code, lines, _ = _run(monkeypatch, capsys, str(old), str(new), "-o", str(diff))
    assert code == 0
    assert lines == ["page-01.png: unchanged"]


def test_changed_page_reports_fraction_bbox_and_diff_image(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    _page(old / "page-01.png")
    _page(old / "page-02.png")
    _page(new / "page-01.png")
    _page(new / "page-02.png", mark=(160, 200, 169, 209))  # 10 x 10 px
    code, lines, _ = _run(monkeypatch, capsys, str(old), str(new), "-o", str(diff))
    assert code == 1
    assert lines[0] == "page-01.png: unchanged"
    assert lines[1].startswith("page-02.png: changed 0.179% of pixels")
    assert "bbox (160, 200, 170, 210)" in lines[1]
    written = diff / "page-02.png"
    assert f"diff {written}" in lines[1]
    assert not (diff / "page-01.png").exists()
    with Image.open(written) as image:
        rgb = image.convert("RGB")
        assert rgb.getpixel((165, 205)) == (220, 0, 0)  # changed pixel highlighted
        faded = rgb.getpixel((12, 12))  # unchanged text faded, not highlighted
        assert isinstance(faded, tuple)
        assert faded[0] == faded[1] == faded[2] and faded[0] > 150


def test_threshold_ignores_small_change(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    _page(old / "page-01.png")
    _page(new / "page-01.png", mark=(160, 200, 169, 209))
    args = (str(old), str(new), "-o", str(diff))
    assert _run(monkeypatch, capsys, *args, "--threshold", "0.01")[:2] == (
        0,
        ["page-01.png: unchanged"],
    )
    assert _run(monkeypatch, capsys, *args, "--threshold", "0.001")[0] == 1


def test_page_count_changes_are_reported(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    _page(old / "page-01.png")
    _page(old / "page-03.png")
    _page(new / "page-01.png")
    _page(new / "page-02.png")
    code, lines, _ = _run(monkeypatch, capsys, str(old), str(new), "-o", str(diff))
    assert code == 1
    assert lines == [
        "page-01.png: unchanged",
        "page-02.png: added",
        "page-03.png: removed",
    ]


def test_different_page_size_is_changed_not_a_crash(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    _page(old / "page-01.png")
    _page(new / "page-01.png", size=(200, 300), mark=(10, 285, 30, 295))
    code, lines, _ = _run(monkeypatch, capsys, str(old), str(new), "-o", str(diff))
    assert code == 1
    assert "page-01.png: changed" in lines[0]
    assert "size 200x280 -> 200x300" in lines[0]
    assert (diff / "page-01.png").exists()


def test_stale_diff_images_are_removed(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    _page(old / "page-01.png")
    _page(new / "page-01.png")
    _page(diff / "page-07.png")
    assert _run(monkeypatch, capsys, str(old), str(new), "-o", str(diff))[0] == 0
    assert not (diff / "page-07.png").exists()


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("missing", "old directory .* not found"),
        ("empty", "new directory .* has no page-"),
    ],
)
def test_missing_or_empty_directory_fails_clearly(dirs, monkeypatch, capsys, setup, message):
    old, new, diff = dirs
    if setup == "missing":
        _page(new / "page-01.png")
    else:
        _page(old / "page-01.png")
        new.mkdir()
    code, lines, err = _run(monkeypatch, capsys, str(old), str(new), "-o", str(diff))
    assert code == 2
    assert lines == []
    assert re.search(message, err)


def test_bad_threshold_and_diff_dir_collision_fail(dirs, monkeypatch, capsys):
    old, new, diff = dirs
    _page(old / "page-01.png")
    _page(new / "page-01.png")
    code, _, err = _run(
        monkeypatch, capsys, str(old), str(new), "-o", str(diff), "--threshold", "1"
    )
    assert code == 2
    assert "threshold must be in [0, 1)" in err
    code, _, err = _run(monkeypatch, capsys, str(old), str(new), "-o", str(new))
    assert code == 2
    assert "must differ" in err
    assert (new / "page-01.png").exists()


def test_transparent_pages_compare_as_white(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    Image.new("RGBA", SIZE, (0, 0, 0, 0)).save(old / "page-01.png")
    Image.new("RGB", SIZE, (255, 255, 255)).save(new / "page-01.png")
    report = compare_renders(old, new, tmp_path / "diff")
    assert not report.changed
