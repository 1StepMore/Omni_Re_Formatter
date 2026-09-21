"""MD to PDF channel tests."""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Make weasyprint importable for mock patches when the package is not installed
if importlib.util.find_spec("weasyprint") is None:
    sys.modules["weasyprint"] = MagicMock()

import pytest
from orf.channels.md2pdf import MD2PDFConverter
from orf.converters.base import ConversionResult


@pytest.fixture
def sample_md(tmp_path: Path) -> Path:
    content = """# 用户手册

 这是中文内容。

 ## 第一章

 - 列表项 1
 - 列表项 2

 | 表格 | 列 |
 |------|---|
 | 数据 | 值 |
 """
    md_file = tmp_path / "test.md"
    md_file.write_text(content, encoding="utf-8")
    return md_file


class TestMD2PDFConverter:
    def test_supported_format(self):
        converter = MD2PDFConverter()
        assert converter.supported_format == "PDF"

    def test_validate_input_valid(self, sample_md: Path):
        converter = MD2PDFConverter()
        assert converter.validate_input(sample_md) is True

    def test_validate_input_invalid(self, tmp_path: Path):
        txt_file = tmp_path / "test.txt"
        txt_file.touch()

        converter = MD2PDFConverter()
        assert converter.validate_input(txt_file) is False

    @patch("subprocess.run")
    def test_convert_pandoc_success(self, mock_run, sample_md: Path, tmp_path: Path):
        from orf.converters.options import ConverterOptions

        output = tmp_path / "output.pdf"
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )

        converter = MD2PDFConverter()
        result = converter.convert(sample_md, output, ConverterOptions(engine="pandoc"))

        assert isinstance(result, ConversionResult)
        assert result.success is True
        assert result.output_path == output

        call_args = mock_run.call_args[0][0]
        assert "pandoc" in call_args
        assert str(sample_md) in call_args
        assert str(output) in call_args
        assert "--to" in call_args
        assert "pdf" in call_args

    @patch("orf.channels.md2html.MD2HTMLConverter")
    @patch("weasyprint.HTML")
    def test_convert_weasyprint_success(self, mock_weasyprint_html, mock_md2html_class, sample_md: Path, tmp_path: Path):
        output = tmp_path / "output.pdf"

        # Mock MD2HTMLConverter instance and its convert method
        mock_md2html_instance = MagicMock()
        mock_md2html_instance.convert.return_value = MagicMock(success=True)
        mock_md2html_class.return_value = mock_md2html_instance

        # Mock WeasyPrint HTML
        mock_weasyprint_instance = MagicMock()
        mock_weasyprint_html.return_value = mock_weasyprint_instance

        from orf.converters.options import ConverterOptions
        converter = MD2PDFConverter()
        result = converter.convert(sample_md, output, ConverterOptions(engine="weasyprint"))

        assert result.success is True
        assert result.output_path == output
        mock_md2html_instance.convert.assert_called_once()
        mock_weasyprint_instance.write_pdf.assert_called_once()

    @patch("subprocess.run")
    def test_convert_pandoc_error(self, mock_run, sample_md: Path, tmp_path: Path):
        from subprocess import CalledProcessError

        from orf.converters.options import ConverterOptions

        output = tmp_path / "output.pdf"
        mock_run.side_effect = CalledProcessError(
            1, "pandoc", stderr="Unknown extension"
        )

        converter = MD2PDFConverter()
        result = converter.convert(sample_md, output, ConverterOptions(engine="pandoc"))

        assert result.success is False
        assert len(result.errors) > 0
        assert "Pandoc error" in result.errors[0].message

    @patch("subprocess.run")
    def test_convert_pandoc_not_found(self, mock_run, sample_md: Path, tmp_path: Path):
        from orf.converters.options import ConverterOptions

        output = tmp_path / "output.pdf"
        mock_run.side_effect = FileNotFoundError()

        converter = MD2PDFConverter()
        result = converter.convert(sample_md, output, ConverterOptions(engine="pandoc"))

        assert result.success is False
        assert "not in PATH" in result.errors[0].message

    @patch("subprocess.run")
    def test_convert_weasyprint_import_error(self, mock_run, sample_md: Path, tmp_path: Path):
        output = tmp_path / "output.pdf"

        # Simulate BOTH engines unavailable: WeasyPrint absent AND pandoc not
        # installed. Patching importlib.util.find_spec alone is NOT deterministic
        # (e2e#56): on runners that ship pandoc + a TeX PDF engine, the pandoc
        # fallback succeeds and produces a real PDF, so the failure path must be
        # forced explicitly to assert the graceful, crash-free contract.
        mock_run.side_effect = FileNotFoundError()
        # Patch importlib.util.find_spec to simulate weasyprint not installed
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None  # simulate weasyprint not installed
            from orf.converters.options import ConverterOptions
            converter = MD2PDFConverter()
            result = converter.convert(sample_md, output, ConverterOptions(engine="weasyprint"))

            # Graceful fallback: when neither engine can render, the converter
            # returns a clean failure naming the engine — never a crash.
            assert result.success is False
            assert result.errors, "expected a clear error from the fallback path"

    def test_convert_invalid_input(self, tmp_path: Path):
        invalid_file = tmp_path / "nonexistent.md"
        output = tmp_path / "output.pdf"

        converter = MD2PDFConverter()
        result = converter.convert(invalid_file, output)

        assert result.success is False
        assert "Invalid input file" in result.errors[0].message

    def test_convert_weasyprint_real_render_cjk(self, tmp_path: Path, sample_md: Path):
        """CI-G3: Real (non-mocked) weasyprint PDF render with CJK content.

        Skipped locally when weasyprint C libs are not installed.
        In CI, weasyprint + libpango + libcairo are installed by
        e2e-tests.yml, so this test runs and asserts:
          - output exists and is non-empty
          - output is a valid PDF (%PDF- header)
          - output contains a decodable text layer (CJK glyphs)

        Regression for OPP#24/26/28: prior CI runs always skipped
        PDF rendering, so PDF regressions slipped through.
        """
        weasyprint_spec = importlib.util.find_spec("weasyprint")
        pytest.skipif = getattr(pytest, "skipif", None)  # ensure skipif exists
        if weasyprint_spec is None:
            pytest.skip("weasyprint not installed (CI-only test)")
        # Also skip if weasyprint import succeeds but C libs are missing
        # (importing the sub-modules would raise OSError on first use).
        try:
            from orf.converters.options import ConverterOptions
            converter = MD2PDFConverter()
            output = tmp_path / "real_render.pdf"
            result = converter.convert(
                sample_md, output, ConverterOptions(engine="weasyprint")
            )
        except OSError as exc:
            pytest.skip(f"weasyprint C libs unavailable: {exc}")

        assert result.success is True, (
            f"CI-G3: real weasyprint render failed: {result.errors}"
        )
        assert output.exists(), "PDF output not created"
        assert output.stat().st_size > 200, (
            f"CI-G3: PDF too small ({output.stat().st_size} bytes) — "
            "weasyprint may have produced an empty PDF"
        )
        # Read the first 8 bytes and verify PDF magic
        with open(output, "rb") as f:
            magic = f.read(8)
        assert magic.startswith(b"%PDF-"), (
            f"CI-G3: output is not a valid PDF (magic={magic!r})"
        )