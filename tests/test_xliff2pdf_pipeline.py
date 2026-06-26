"""Tests for the XLIFF2PDFConverter (XLIFF→HTML→PDF)."""
import pytest
from pathlib import Path


def test_xliff2pdf_requires_skeleton_html(tmp_path: Path):
    """Calling convert without options.skeleton_html should fail clearly."""
    from orf.channels.xliff2pdf import XLIFF2PDFConverter
    from orf.converters.options import ConverterOptions
    c = XLIFF2PDFConverter()
    # Create minimal PDF and XLIFF
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fake")
    xlf = tmp_path / "in.xlf"
    xlf.write_text("<xliff/>")
    output = tmp_path / "out.pdf"

    result = c.convert(pdf, xlf, output)  # No options
    assert not result.success
    # Error should mention skeleton_html
    assert any("skeleton_html" in str(e) for e in (result.errors or []))


def test_xliff2pdf_supported_format():
    from orf.channels.xliff2pdf import XLIFF2PDFConverter
    c = XLIFF2PDFConverter()
    assert c.supported_format == "PDF"


def test_xliff2pdf_validate_input(tmp_path: Path):
    from orf.channels.xliff2pdf import XLIFF2PDFConverter
    c = XLIFF2PDFConverter()
    # Non-existent
    assert not c.validate_input(tmp_path / "nope.pdf")
    # Wrong extension
    txt = tmp_path / "t.txt"
    txt.write_text("hello")
    assert not c.validate_input(txt)
    # Valid PDF (just needs .pdf extension, content is checked later)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert c.validate_input(pdf)
