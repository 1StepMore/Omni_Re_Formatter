"""MD to PPTX channel tests."""

import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from orf.channels.md2pptx import MD2PPTXConverter
from orf.converters.base import ConversionResult


# E2E-79: convert() does a shutil.which() pre-flight before subprocess.run,
# so @patch("subprocess.run") alone doesn't bypass it. Skip when missing.
_md2pptx_missing = shutil.which("md2pptx") is None
_md2pptx_skip = pytest.mark.skipif(
    _md2pptx_missing, reason="md2pptx binary not on PATH (see E2E-79 install hint)"
)


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


class TestMD2PPTXConverter:
    def test_supported_format(self):
        converter = MD2PPTXConverter()
        assert converter.supported_format == "PPTX"

    def test_validate_input_valid(self, sample_md: Path):
        converter = MD2PPTXConverter()
        assert converter.validate_input(sample_md) is True

    def test_validate_input_invalid_extension(self, tmp_path: Path):
        txt_file = tmp_path / "test.txt"
        txt_file.touch()

        converter = MD2PPTXConverter()
        assert converter.validate_input(txt_file) is False

    def test_validate_input_not_exists(self):
        converter = MD2PPTXConverter()
        assert converter.validate_input("/nonexistent/file.md") is False

    @_md2pptx_skip
    @patch("subprocess.run")
    def test_convert_success(self, mock_run, sample_md: Path, tmp_path: Path):
        output = tmp_path / "output.pptx"
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )

        converter = MD2PPTXConverter()
        result = converter.convert(sample_md, output)

        assert isinstance(result, ConversionResult)
        assert result.success is True
        assert result.output_path == output

        call_args = mock_run.call_args[0][0]
        assert "md2pptx" in call_args
        assert str(sample_md) in call_args
        # md2pptx uses positional args: md2pptx <input> <output>  (no -o flag)
        assert call_args[-1] == str(output)

    @_md2pptx_skip
    @patch("subprocess.run")
    def test_convert_md2pptx_error(self, mock_run, sample_md: Path, tmp_path: Path):
        from subprocess import CalledProcessError

        output = tmp_path / "output.pptx"
        mock_run.side_effect = CalledProcessError(
            1, "md2pptx", stderr="Unknown extension"
        )

        converter = MD2PPTXConverter()
        result = converter.convert(sample_md, output)

        assert result.success is False
        assert len(result.errors) > 0
        assert "md2pptx error" in result.errors[0].message

    @_md2pptx_skip
    @patch("subprocess.run")
    def test_convert_md2pptx_not_found(self, mock_run, sample_md: Path, tmp_path: Path):
        output = tmp_path / "output.pptx"
        mock_run.side_effect = FileNotFoundError()

        converter = MD2PPTXConverter()
        result = converter.convert(sample_md, output)

        assert result.success is False
        assert "not in PATH" in result.errors[0].message

    def test_convert_invalid_input(self, tmp_path: Path):
        invalid_file = tmp_path / "nonexistent.md"
        output = tmp_path / "output.pptx"

        converter = MD2PPTXConverter()
        result = converter.convert(invalid_file, output)

        assert result.success is False
        assert "Invalid input file" in result.errors[0].message

    def test_convert_falls_back_to_pandoc_when_md2pptx_missing(
        self, sample_md: Path, tmp_path: Path
    ):
        """ORF#7: when md2pptx is missing but pandoc is available,
        the converter should fall back to pandoc and return success."""
        from subprocess import CompletedProcess
        from orf.channels import md2pptx as md2pptx_mod

        output = tmp_path / "output.pptx"

        def fake_which(name: str) -> str | None:
            if name == "md2pptx":
                return None
            if name == "pandoc":
                return "/usr/bin/pandoc"
            return None

        def fake_run(cmd, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"fake pptx")
            return CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch_ctx = patch.object(md2pptx_mod.shutil, "which", side_effect=fake_which)
        mock_run_ctx = patch.object(md2pptx_mod.subprocess, "run", side_effect=fake_run)

        with monkeypatch_ctx, mock_run_ctx:
            converter = MD2PPTXConverter()
            result = converter.convert(sample_md, output)

        assert result.success is True
        assert result.metadata.get("tool") == "pandoc"
        assert result.metadata.get("fallback_from") == "md2pptx"

    def test_convert_fails_when_both_md2pptx_and_pandoc_missing(
        self, sample_md: Path, tmp_path: Path
    ):
        """ORF#7: when both md2pptx AND pandoc are missing,
        the converter should return the install hint error."""
        from orf.channels import md2pptx as md2pptx_mod

        output = tmp_path / "output.pptx"

        with patch.object(md2pptx_mod.shutil, "which", return_value=None):
            converter = MD2PPTXConverter()
            result = converter.convert(sample_md, output)

        assert result.success is False
        err_str = getattr(result.errors[0], "message", None) or str(result.errors[0])
        assert "install" in err_str.lower() or "md2pptx" in err_str

    def test_convert_still_uses_md2pptx_when_available(
        self, sample_md: Path, tmp_path: Path
    ):
        """ORF#7: when md2pptx IS available, the converter should
        still use md2pptx (primary path unchanged)."""
        from subprocess import CompletedProcess
        from orf.channels import md2pptx as md2pptx_mod

        output = tmp_path / "output.pptx"

        def fake_which(name: str) -> str | None:
            if name == "md2pptx":
                return "/usr/bin/md2pptx"
            return None

        def fake_run(cmd, **kwargs):
            Path(str(output)).write_bytes(b"fake pptx")
            return CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(md2pptx_mod.shutil, "which", side_effect=fake_which), \
             patch.object(md2pptx_mod.subprocess, "run", side_effect=fake_run):
            converter = MD2PPTXConverter()
            result = converter.convert(sample_md, output)

        assert result.success is True
        assert result.metadata.get("tool") == "md2pptx"