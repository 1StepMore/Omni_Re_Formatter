"""XLIFF to HTML channel tests."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from orf.channels.xliff2html import XLIFF2HTMLConverter
from orf.converters.options import ConverterOptions
from orf.converters.base import ConversionResult


@pytest.fixture
def sample_html_template(tmp_path: Path) -> Path:
    # The OPP contract: each translatable node carries
    # data-trans-unit-id="<id>" matching the <trans-unit> id in the XLIFF.
    # Literal ``[<unit_id>]`` substring placeholders are NOT part of the
    # contract and are left in place if they happen to appear in source text.
    content = """<!DOCTYPE html>
<html>
<head><title>Test Document</title></head>
<body>
<h1 data-trans-unit-id="heading1">Original Heading</h1>
<p data-trans-unit-id="para1">Original paragraph content.</p>
<p data-trans-unit-id="trans-unit-1">placeholder one</p>
<p data-trans-unit-id="trans-unit-2">placeholder two</p>
</body>
</html>"""
    html_file = tmp_path / "template.html"
    html_file.write_text(content, encoding="utf-8")
    return html_file


@pytest.fixture
def sample_xliff(tmp_path: Path) -> Path:
    content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
<file original="template.html" source-language="en" target-language="zh-CN">
<body>
<trans-unit id="heading1">
<source>Original Heading</source>
<target>翻译标题</target>
</trans-unit>
<trans-unit id="para1">
<source>Original paragraph content.</source>
<target>翻译的段落内容。</target>
</trans-unit>
<trans-unit id="trans-unit-1">
<source>placeholder 1</source>
<target>占位符1</target>
</trans-unit>
<trans-unit id="trans-unit-2">
<source>placeholder 2</source>
<target>占位符2</target>
</trans-unit>
</body>
</file>
</xliff>"""
    xliff_file = tmp_path / "translation.xlf"
    xliff_file.write_text(content, encoding="utf-8")
    return xliff_file


class TestXLIFF2HTMLConverter:
    def test_supported_format(self):
        converter = XLIFF2HTMLConverter()
        assert converter.supported_format == "HTML"

    def test_validate_input_valid(self, sample_html_template: Path):
        converter = XLIFF2HTMLConverter()
        assert converter.validate_input(sample_html_template) is True

    def test_validate_input_htm_extension(self, tmp_path: Path):
        htm_file = tmp_path / "test.htm"
        htm_file.touch()
        converter = XLIFF2HTMLConverter()
        assert converter.validate_input(htm_file) is True

    def test_validate_input_invalid_extension(self, tmp_path: Path):
        txt_file = tmp_path / "test.txt"
        txt_file.touch()
        converter = XLIFF2HTMLConverter()
        assert converter.validate_input(txt_file) is False

    def test_validate_input_not_exists(self):
        converter = XLIFF2HTMLConverter()
        assert converter.validate_input("/nonexistent/file.html") is False

    def test_convert_missing_html_template(self, sample_xliff: Path, tmp_path: Path):
        output = tmp_path / "output.html"
        missing_html = tmp_path / "missing.html"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(missing_html, sample_xliff, output)
        assert result.success is False
        assert "HTML template not found" in result.errors[0].message

    def test_convert_missing_xliff(self, sample_html_template: Path, tmp_path: Path):
        output = tmp_path / "output.html"
        missing_xliff = tmp_path / "missing.xlf"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(sample_html_template, missing_xliff, output)
        assert result.success is False
        assert "XLIFF file not found" in result.errors[0].message

    def test_convert_success(self, sample_html_template: Path, sample_xliff: Path, tmp_path: Path):
        output = tmp_path / "output.html"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(sample_html_template, sample_xliff, output)
        assert isinstance(result, ConversionResult)
        assert result.success is True
        assert result.output_path == output
        assert output.exists()
        content = output.read_text(encoding="utf-8")
        assert "翻译标题" in content
        assert "翻译的段落内容" in content

    def test_convert_with_placeholder_replacement(self, sample_html_template: Path, sample_xliff: Path, tmp_path: Path):
        output = tmp_path / "output.html"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(sample_html_template, sample_xliff, output)
        assert result.success is True
        content = output.read_text(encoding="utf-8")
        assert "占位符1" in content
        assert "占位符2" in content

    def test_convert_without_inline_formatting(self, sample_html_template: Path, sample_xliff: Path, tmp_path: Path):
        output = tmp_path / "output.html"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(sample_html_template, sample_xliff, output, options=ConverterOptions(preserve_inline=False))
        assert result.success is True
        assert output.exists()

    def test_convert_read_error_html(self, sample_xliff: Path, tmp_path: Path):
        output = tmp_path / "output.html"
        html_file = tmp_path / "unreadable.html"
        html_file.write_text("test", encoding="utf-8")
        html_file.chmod(0o000)
        converter = XLIFF2HTMLConverter()
        try:
            result = converter.convert(html_file, sample_xliff, output)
            assert result.success is False
            assert "Failed to read HTML template" in result.errors[0].message
        finally:
            html_file.chmod(0o644)

    def test_convert_write_error(self, sample_html_template: Path, sample_xliff: Path, tmp_path: Path):
        output = tmp_path / "subdir" / "output.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("", encoding="utf-8")
        output.chmod(0o444)
        converter = XLIFF2HTMLConverter()
        try:
            result = converter.convert(sample_html_template, sample_xliff, output)
            assert result.success is False
            assert "Failed to write output HTML" in result.errors[0].message
        finally:
            output.chmod(0o644)

    def test_parse_xliff_extracts_translations(self):
        converter = XLIFF2HTMLConverter()
        xliff_content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
<file>
<body>
<trans-unit id="unit1">
<source>Source text</source>
<target>Target text</target>
</trans-unit>
<trans-unit id="unit2">
<source>Another source</source>
<target>Another target</target>
</trans-unit>
</body>
</file>
</xliff>"""
        translations = converter._parse_xliff(xliff_content)
        assert translations["unit1"] == "Target text"
        assert translations["unit2"] == "Another target"

    def test_parse_xliff_falls_back_to_source(self):
        converter = XLIFF2HTMLConverter()
        xliff_content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
<file>
<body>
<trans-unit id="unit1">
<source>Source only</source>
</trans-unit>
</body>
</file>
</xliff>"""
        translations = converter._parse_xliff(xliff_content)
        assert translations["unit1"] == "Source only"

    def test_parse_xliff_strips_inline_tags(self):
        converter = XLIFF2HTMLConverter()
        xliff_content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2">
<file>
<body>
<trans-unit id="unit1">
<source>Text with <bx id="bold"/>inline<ex id="bold"/> tags</source>
<target>Text with <bx id="italic"/>inline<ex id="italic"/> formatting</target>
</trans-unit>
</body>
</file>
</xliff>"""
        translations = converter._parse_xliff(xliff_content)
        assert "<bx" not in translations["unit1"]
        assert "<ex" not in translations["unit1"]
        assert "inline formatting" in translations["unit1"]

    def test_strip_xliff_inline_tags(self):
        converter = XLIFF2HTMLConverter()
        text = 'Hello <bx id="bold"/>world<ex id="bold"/> and <ex id="italic"/>more'
        result = converter._strip_xliff_inline_tags(text)
        assert result == "Hello world and more"
        assert "<bx" not in result
        assert "<ex" not in result

    def test_convert_with_inline_formatting_applier(self, sample_html_template: Path, sample_xliff: Path, tmp_path: Path):
        """Test that inline formatting is applied via EPUBHTMLInlineApplier."""
        output = tmp_path / "output.html"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(sample_html_template, sample_xliff, output, options=ConverterOptions(preserve_inline=True))
        assert result.success is True
        assert result.metadata["source_format"] == "XLIFF"
        assert result.metadata["target_format"] == "HTML"
        assert "template" in result.metadata
        assert "xliff" in result.metadata

    def test_apply_translations_and_formatting_without_preserve(self, sample_html_template: Path, sample_xliff: Path, tmp_path: Path):
        """Test apply translations with preserve_inline=False."""
        output = tmp_path / "output.html"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(sample_html_template, sample_xliff, output, options=ConverterOptions(preserve_inline=False))
        assert result.success is True
        content = output.read_text(encoding="utf-8")
        assert "翻译标题" in content

    def test_result_metadata(self, sample_html_template: Path, sample_xliff: Path, tmp_path: Path):
        """Test that convert returns proper metadata."""
        output = tmp_path / "output.html"
        converter = XLIFF2HTMLConverter()
        result = converter.convert(sample_html_template, sample_xliff, output)
        assert result.success is True
        assert result.metadata["source_format"] == "XLIFF"
        assert result.metadata["target_format"] == "HTML"

    def test_preserves_img_child_during_translation(self, tmp_path: Path):
        """<p data-trans-unit-id="x"> has <img> child — must survive translation."""
        html = """<!DOCTYPE html>
<html><body>
<p data-trans-unit-id="para-0">Welcome <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==" alt="logo"/> solution</p>
</body></html>"""
        xliff = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2"><file source-language="en" target-language="zh" datatype="plaintext" original="test" date="2026-01-01T00:00:00Z"><body>
<trans-unit id="para-0" resname="para_index_0">
<source>Welcome  solution</source>
<target>欢迎使用解决方案</target>
</trans-unit>
</body></file></xliff>"""
        tmpl = tmp_path / "t.html"
        tmpl.write_text(html, encoding="utf-8")
        xlf = tmp_path / "t.xlf"
        xlf.write_text(xliff, encoding="utf-8")
        output = tmp_path / "out.html"
        conv = XLIFF2HTMLConverter()
        result = conv.convert(tmpl, xlf, output, options=ConverterOptions(preserve_inline=False))
        assert result.success, f"Conversion failed: {result.errors}"
        content = output.read_text(encoding="utf-8")
        assert "欢迎使用解决方案" in content
        assert "<img" in content
        assert "iVBORw0KGgo" in content

    def test_preserves_strong_img_a_children(self, tmp_path: Path):
        """Multiple child types all survive translation."""
        html = """<!DOCTYPE html>
<html><body>
<p data-trans-unit-id="x"><strong>Note:</strong> <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="/> see <a href="http://example.com">docs</a></p>
</body></html>"""
        xliff = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2"><file source-language="en" target-language="zh" datatype="plaintext" original="test" date="2026-01-01T00:00:00Z"><body>
<trans-unit id="x" resname="para_index_0">
<source>Note: see docs</source>
<target>注意：参见文档</target>
</trans-unit>
</body></file></xliff>"""
        tmpl = tmp_path / "t.html"
        tmpl.write_text(html, encoding="utf-8")
        xlf = tmp_path / "t.xlf"
        xlf.write_text(xliff, encoding="utf-8")
        output = tmp_path / "out.html"
        conv = XLIFF2HTMLConverter()
        result = conv.convert(tmpl, xlf, output, options=ConverterOptions(preserve_inline=False))
        assert result.success, f"Conversion failed: {result.errors}"
        content = output.read_text(encoding="utf-8")
        assert "注意：" in content
        assert "<strong>" in content
        assert "<img" in content
        assert "<a href=" in content

    def test_preserves_br_in_heading(self, tmp_path: Path):
        """<br> inside a translated heading must survive."""
        html = """<!DOCTYPE html>
<html><body>
<h1 data-trans-unit-id="h-1">Line 1<br/>Line 2</h1>
</body></html>"""
        xliff = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2"><file source-language="en" target-language="zh" datatype="plaintext" original="test" date="2026-01-01T00:00:00Z"><body>
<trans-unit id="h-1" resname="para_index_0">
<source>Line 1Line 2</source>
<target>第一行第二行</target>
</trans-unit>
</body></file></xliff>"""
        tmpl = tmp_path / "t.html"
        tmpl.write_text(html, encoding="utf-8")
        xlf = tmp_path / "t.xlf"
        xlf.write_text(xliff, encoding="utf-8")
        output = tmp_path / "out.html"
        conv = XLIFF2HTMLConverter()
        result = conv.convert(tmpl, xlf, output, options=ConverterOptions(preserve_inline=False))
        assert result.success, f"Conversion failed: {result.errors}"
        content = output.read_text(encoding="utf-8")
        assert "第一行" in content
        assert "<br" in content or "<br/>" in content

    def test_inline_formatting_preserves_img(self, tmp_path: Path):
        """When preserve_inline=True and XLIFF has bx/ex markers, img children survive."""
        html = """<!DOCTYPE html>
<html><body>
<p data-trans-unit-id="x"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="/> <strong>bold text</strong> end</p>
</body></html>"""
        xliff = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2"><file source-language="en" target-language="zh" datatype="plaintext" original="test" date="2026-01-01T00:00:00Z"><body>
<trans-unit id="x" resname="para_index_0">
<source> <bx id="b1" type="bold"/>bold text<ex id="b1"/> end</source>
<target> <bx id="b1" type="bold"/>粗体文本<ex id="b1"/> 结尾</target>
</trans-unit>
</body></file></xliff>"""
        tmpl = tmp_path / "t.html"
        tmpl.write_text(html, encoding="utf-8")
        xlf = tmp_path / "t.xlf"
        xlf.write_text(xliff, encoding="utf-8")
        output = tmp_path / "out.html"
        conv = XLIFF2HTMLConverter()
        result = conv.convert(tmpl, xlf, output, options=ConverterOptions(preserve_inline=True))
        assert result.success, f"Conversion failed: {result.errors}"
        content = output.read_text(encoding="utf-8")
        assert "<img" in content
        assert "iVBORw0KGgo" in content