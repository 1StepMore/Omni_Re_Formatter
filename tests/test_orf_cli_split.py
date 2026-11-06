"""Regression tests for ORF CLI split into commands/* modules.

Verifies that:
1. All CLI commands are reachable from the main group
2. Help output for each command is correct
3. The module structure is sound (no import errors)
"""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from orf.cli import main


@pytest.fixture(autouse=True)
def _fake_pandoc(monkeypatch: pytest.MonkeyPatch):
    """Use the fake-pandoc seam without leaking into later test modules.

    A module-level ``os.environ.setdefault`` used to pollute every test
    collected afterwards, and the CLI seam patches ``subprocess.run`` without
    ever undoing it — scope both to this module's tests (ORF#81).
    """
    real_run = subprocess.run
    monkeypatch.setenv("OMNI_TEST_FAKE_PANDOC", "1")
    yield
    subprocess.run = real_run


@pytest.fixture
def runner():
    """Create a Click CLI test runner."""
    return CliRunner()


class TestOrfCliSplit:
    """Test that the CLI module split preserved all command registrations."""

    def test_help_output(self, runner):
        """`orf --help` shows all 4 commands."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "apply-md" in result.output
        assert "apply-xliff" in result.output
        assert "convert-batch" in result.output
        assert "info" in result.output

    def test_apply_md_help(self, runner):
        """`orf apply-md --help` shows apply-md options."""
        result = runner.invoke(main, ["apply-md", "--help"])
        assert result.exit_code == 0
        assert "INPUT_MD" in result.output
        assert "--target-format" in result.output
        assert "--output" in result.output

    def test_apply_xliff_help(self, runner):
        """`orf apply-xliff --help` shows apply-xliff options."""
        result = runner.invoke(main, ["apply-xliff", "--help"])
        assert result.exit_code == 0
        assert "INPUT_FILE" in result.output
        assert "--xliff" in result.output
        assert "--output" in result.output
        assert "--format" in result.output

    def test_convert_batch_help(self, runner):
        """`orf convert-batch --help` shows convert-batch options."""
        result = runner.invoke(main, ["convert-batch", "--help"])
        assert result.exit_code == 0
        assert "INPUT_DIR" in result.output
        assert "--target-format" in result.output
        assert "--pattern" in result.output

    def test_info_help(self, runner):
        """`orf info --help` shows info options."""
        result = runner.invoke(main, ["info", "--help"])
        assert result.exit_code == 0
        assert "INPUT_FILE" in result.output

    def test_commands_module_importable(self):
        """All command modules can be imported without errors."""
        from orf.commands import apply_md
        from orf.commands import apply_xliff
        from orf.commands import convert_batch
        from orf.commands import info

        # Verify all command functions exist
        assert callable(apply_md.apply_md)
        assert callable(apply_xliff.apply_xliff)
        assert callable(convert_batch.convert_batch)
        assert callable(info.info)

    def test_xliff2html_converter_importable(self):
        """XLIFF2HTMLConverter can be imported from the new package path."""
        from orf.channels.xliff2html import XLIFF2HTMLConverter

        converter = XLIFF2HTMLConverter()
        assert converter.supported_format == "HTML"

    def test_xliff2html_submodules_importable(self):
        """All xliff2html sub-modules are importable."""
        from orf.channels.xliff2html.parser import parse_xliff, strip_xliff_inline_tags
        from orf.channels.xliff2html.writer import parse_html_fragment
        from orf.channels.xliff2html.images import get_image_bytes, create_data_uri

        assert callable(parse_xliff)
        assert callable(strip_xliff_inline_tags)
        assert callable(parse_html_fragment)
        assert callable(get_image_bytes)
        assert callable(create_data_uri)
