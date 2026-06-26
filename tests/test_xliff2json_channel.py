"""ORF#13: Tests for xliff2json channel.

Verifies that XLIFF files can be converted to JSON output with
unit_id, source, and target fields preserved.
"""

from __future__ import annotations

import json

import pytest
from pathlib import Path

# Import the function we're about to create (RED phase — will fail until GREEN)
from orf.channels.xliff2json import apply_xliff_to_json


@pytest.fixture
def sample_xliff(tmp_path: Path) -> Path:
    """Create a sample XLIFF file for testing."""
    xliff_content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="test.docx" source-language="en" target-language="zh-CN">
    <body>
      <trans-unit id="1">
        <source>Hello World</source>
        <target>你好世界</target>
      </trans-unit>
      <trans-unit id="2">
        <source>Goodbye</source>
        <target>再见</target>
      </trans-unit>
    </body>
  </file>
</xliff>"""
    xliff_file = tmp_path / "test.xlf"
    xliff_file.write_text(xliff_content, encoding="utf-8")
    return xliff_file


@pytest.fixture
def empty_xliff(tmp_path: Path) -> Path:
    """Create an XLIFF file with no trans-units."""
    xliff_content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="test.docx" source-language="en" target-language="zh-CN">
    <body></body>
  </file>
</xliff>"""
    xliff_file = tmp_path / "empty.xlf"
    xliff_file.write_text(xliff_content, encoding="utf-8")
    return xliff_file


class TestApplyXliffToJson:
    """Unit tests for apply_xliff_to_json."""

    def test_basic_conversion(self, sample_xliff: Path, tmp_path: Path):
        """Convert a 2-unit XLIFF to JSON and verify structure."""
        output = tmp_path / "output.json"
        result = apply_xliff_to_json(sample_xliff, output)

        assert result["success"] is True
        assert result["unit_count"] == 2
        assert output.exists()

        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["unit_count"] == 2
        assert len(data["xliff_units"]) == 2

    def test_unit_fields_preserved(self, sample_xliff: Path, tmp_path: Path):
        """Verify unit_id, source, target are correctly extracted."""
        output = tmp_path / "output.json"
        apply_xliff_to_json(sample_xliff, output)

        data = json.loads(output.read_text(encoding="utf-8"))
        unit1 = data["xliff_units"][0]
        assert unit1["unit_id"] == "1"
        assert unit1["source"] == "Hello World"
        assert unit1["target"] == "你好世界"

        unit2 = data["xliff_units"][1]
        assert unit2["unit_id"] == "2"
        assert unit2["source"] == "Goodbye"
        assert unit2["target"] == "再见"

    def test_empty_xliff(self, empty_xliff: Path, tmp_path: Path):
        """XLIFF with no trans-units produces empty list."""
        output = tmp_path / "output.json"
        result = apply_xliff_to_json(empty_xliff, output)

        assert result["success"] is True
        assert result["unit_count"] == 0

        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["unit_count"] == 0
        assert data["xliff_units"] == []

    def test_output_is_valid_json(self, sample_xliff: Path, tmp_path: Path):
        """Output file is valid JSON with proper encoding."""
        output = tmp_path / "output.json"
        apply_xliff_to_json(sample_xliff, output)

        raw = output.read_text(encoding="utf-8")
        data = json.loads(raw)  # Will raise if invalid
        assert "xliff_units" in data
        assert "unit_count" in data

    def test_unicode_preserved(self, tmp_path: Path):
        """CJK characters in source/target are preserved in JSON output."""
        xliff_content = """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="test.docx" source-language="zh" target-language="en">
    <body>
      <trans-unit id="42">
        <source>中文源文本</source>
        <target>Chinese source text</target>
      </trans-unit>
    </body>
  </file>
</xliff>"""
        xliff_file = tmp_path / "cjk.xlf"
        xliff_file.write_text(xliff_content, encoding="utf-8")
        output = tmp_path / "output.json"

        apply_xliff_to_json(xliff_file, output)

        raw = output.read_text(encoding="utf-8")
        assert "中文源文本" in raw  # ensure_ascii=False
        data = json.loads(raw)
        assert data["xliff_units"][0]["source"] == "中文源文本"

    def test_returns_output_path_string(self, sample_xliff: Path, tmp_path: Path):
        """Return dict contains string path, not Path object."""
        output = tmp_path / "output.json"
        result = apply_xliff_to_json(sample_xliff, output)
        assert isinstance(result["output_path"], str)

    def test_json_indentation(self, sample_xliff: Path, tmp_path: Path):
        """Output JSON is pretty-printed with indent=2."""
        output = tmp_path / "output.json"
        apply_xliff_to_json(sample_xliff, output)

        raw = output.read_text(encoding="utf-8")
        # Pretty-printed: should contain newlines and spaces
        assert "\n" in raw
        assert "  " in raw
