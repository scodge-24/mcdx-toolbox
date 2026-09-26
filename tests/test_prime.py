"""Opt-in acceptance using only the public synthetic example."""

import os
from pathlib import Path

import pytest
from PIL import Image

from pymcdx.authoring import build_authoring_document
from pymcdx.prime_render import render_pages, write_pdf
from pymcdx.validation import validate_mcdx_file


@pytest.mark.prime
@pytest.mark.skipif(os.environ.get("PYMCDX_TEST_PRIME") != "1", reason="Prime is opt-in")
def test_synthetic_prime_render_resave_pdf(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "examples/basic.yaml"
    worksheet = tmp_path / "source.mcdx"
    build_authoring_document(source, worksheet)
    before = worksheet.read_bytes()
    resaved = tmp_path / "resaved.mcdx"
    result = render_pages(worksheet, tmp_path / "pages", resave=resaved)
    assert worksheet.read_bytes() == before
    assert result.pages and resaved.is_file()
    validate_mcdx_file(resaved)
    for page in result.pages:
        with Image.open(page) as image:
            image.verify()
    pdf = tmp_path / "worksheet.pdf"
    write_pdf(result.pages, pdf, title="Synthetic rectangle")
    assert pdf.read_bytes().startswith(b"%PDF-")
