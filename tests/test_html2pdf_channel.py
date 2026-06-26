"""Tests for HTML2PDFConverter."""
import pytest
from pathlib import Path


def test_html2pdf_supported_format():
    from orf.channels.html2pdf import HTML2PDFConverter
    c = HTML2PDFConverter()
    assert c.supported_format == "PDF"


def test_html2pdf_validate_input(tmp_path: Path):
    from orf.channels.html2pdf import HTML2PDFConverter
    c = HTML2PDFConverter()
    # Non-existent file
    assert not c.validate_input(tmp_path / "nope.html")
    # Wrong extension
    txt = tmp_path / "t.txt"
    txt.write_text("hello")
    assert not c.validate_input(txt)
    # Valid HTML
    html = tmp_path / "t.html"
    html.write_text("<html><body>test</body></html>")
    assert c.validate_input(html)
    # Valid HTM
    htm = tmp_path / "t.htm"
    htm.write_text("<html><body>test</body></html>")
    assert c.validate_input(htm)


def test_html2pdf_converts_simple_html(tmp_path: Path):
    """Smoke test: simple HTML → PDF (requires weasyprint)."""
    pytest.importorskip("weasyprint")
    from orf.channels.html2pdf import HTML2PDFConverter
    c = HTML2PDFConverter()

    html = tmp_path / "t.html"
    html.write_text(
        "<!DOCTYPE html><html><head><title>Test</title></head>"
        "<body><h1>Hello</h1><p>World</p></body></html>"
    )
    output = tmp_path / "out.pdf"
    result = c.convert(html, output)
    assert result.success, f"Failed: {result.errors}"
    assert output.exists()
    assert output.stat().st_size > 500  # non-empty PDF
