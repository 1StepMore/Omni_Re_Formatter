"""Integration tests for ORF MCP Server."""

import os
import pytest
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock
import json
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from orf.mcp.server import (
    get_server,
    _run_cli_command,
    _call_tool,
    apply_md,
    apply_xliff,
    batch_convert,
    detect_format,
    info,
    ping,
)
from orf.mcp.security import PathValidator


class TestPathValidator:
    """Tests for path security validation."""
    
    def test_valid_path(self):
        """Valid paths should pass."""
        valid, error = PathValidator.validate("test.md")
        assert valid is True
        assert error == ""
    
    def test_path_traversal_blocked(self):
        """Path traversal attempts should be blocked."""
        valid, error = PathValidator.validate("../../etc/passwd")
        assert valid is False
        assert "traversal" in error.lower()
    
    def test_absolute_path_valid(self):
        """Absolute paths should be validated."""
        valid, error = PathValidator.validate("/tmp/test.md")
        assert valid is True


class TestRunCLICommand:
    """Tests for CLI command runner."""
    
    @patch("subprocess.run")
    def test_successful_command(self, mock_run):
        """Successful CLI commands should return parsed JSON."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"success": True, "output_path": "/tmp/out.docx"})
        )
        
        result = _run_cli_command(["apply-md", "input.md", "--target-format", "docx"])
        
        assert result["success"] is True
        assert result["output_path"] == "/tmp/out.docx"
    
    @patch("subprocess.run")
    def test_failed_command(self, mock_run):
        """Failed CLI commands should return error JSON."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout=json.dumps({
                "success": False,
                "errors": [{"code": "ERROR", "message": "Test error", "recovery_strategy": None}]
            }),
            stderr="Error message"
        )

        result = _run_cli_command(["apply-md", "bad.md", "--target-format", "docx"])

        assert result["success"] is False
        assert len(result["errors"]) > 0

    @patch("subprocess.run")
    @patch.dict("os.environ", {"OMNI_TEST_FAKE_PANDOC": "1"}, clear=False)
    def test_fake_pandoc_env_not_leaked_to_subprocess(self, mock_run):
        """OMNI_TEST_FAKE_PANDOC must NOT be inherited by the CLI subprocess.

        ULTRAREADY-VERIFY (2026-06-07): the MCP server used to invoke the
        CLI without an ``env=`` argument, so a test harness that started
        the MCP server with ``OMNI_TEST_FAKE_PANDOC=1`` would silently
        route every MCP conversion to a stub-DOCX. This test pins the
        contract that the seam is scrubbed before subprocess invocation.
        """
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"success": True, "output_path": "/tmp/out.docx"})
        )

        result = _run_cli_command(["apply-md", "input.md", "--target-format", "docx"])

        assert mock_run.called, "subprocess.run was not invoked"
        # Inspect the env kwarg that was passed to subprocess.run.
        kwargs = mock_run.call_args.kwargs
        assert "env" in kwargs, (
            "subprocess.run was called without env=; "
            "the FAKE_PANDOC seam will leak across the MCP boundary."
        )
        assert "OMNI_TEST_FAKE_PANDOC" not in kwargs["env"], (
            f"OMNI_TEST_FAKE_PANDOC leaked into subprocess env: {kwargs['env']!r}"
        )
        # Sanity: real env vars (PATH) are still present.
        assert "PATH" in kwargs["env"]
        # And the call still produced a real result.
        assert result["success"] is True


class TestApplyMdTool:
    """Tests for apply_md MCP tool (FastMCP 3.x: in-process function aliases)."""

    @patch("subprocess.run")
    def test_apply_md_success(self, mock_run, tmp_path):
        """apply_md blocks path traversal; valid path returns tool result."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "success": True,
                "output_path": "/tmp/result.docx",
                "errors": [],
                "warnings": [],
                "metadata": {},
            }),
        )

        result_str = apply_md(input_md="../../etc/passwd", target_format="docx")
        result_data = json.loads(result_str)
        assert result_data["success"] is False
        assert "PATH_NOT_ALLOWED" in str(result_data.get("errors", []))
        assert "error" in result_data
        assert result_data["error"]["code"] == "PATH_NOT_ALLOWED"


class TestApplyXLIFFTool:
    """Tests for apply_xliff MCP tool."""

    def test_apply_xliff_path_validation(self):
        """apply_xliff should validate paths."""
        result_str = apply_xliff(
            input_file="../../etc/passwd",
            xliff_path="test.xlf",
            output_path="out.docx",
            format="docx",
        )
        result_data = json.loads(result_str)
        assert result_data["success"] is False
        assert "error" in result_data
        assert isinstance(result_data["error"], dict)
        assert "code" in result_data["error"]


class TestBatchConvertTool:
    """Tests for batch_convert MCP tool."""

    def test_batch_convert_invalid_path(self):
        """batch_convert should handle invalid paths."""
        result_str = batch_convert(
            input_dir="../../dangerous",
            target_format="docx",
            pattern="*.md",
        )
        result_data = json.loads(result_str)
        assert result_data.get("success") is False
        content = result_data.get("content", {})
        assert content.get("success_count", 0) == 0
        assert "error" in result_data
        assert isinstance(result_data["error"], dict)
        assert result_data["error"]["code"] == "PATH_NOT_ALLOWED"
        assert "message" in result_data["error"]


class TestDetectFormatTool:
    """Tests for detect_format MCP tool."""

    def test_detect_format_invalid_path(self):
        """detect_format should handle invalid paths."""
        result_str = detect_format(file_path="../../etc/passwd")
        result_data = json.loads(result_str)
        assert result_data.get("success") is False
        content = result_data.get("content", {})
        assert content.get("format") == "UNKNOWN"
        assert content.get("confidence") == 0.0
        assert "error" in result_data
        assert isinstance(result_data["error"], dict)
        assert "code" in result_data["error"]


class TestInfoTool:
    """Tests for info MCP tool."""

    def test_info_invalid_path(self):
        """info should handle invalid paths."""
        result_str = info(file_path="../../etc/passwd")
        result_data = json.loads(result_str)
        assert result_data.get("success") is False
        content = result_data.get("content", {})
        assert content.get("manifest_status") == "error"
        assert "error" in result_data
        assert isinstance(result_data["error"], dict)
        assert "code" in result_data["error"]


class TestMCPIntegration:
    """Full integration tests."""

    def test_server_starts(self):
        """MCP server should start without error."""
        server = get_server()
        assert server is not None

    def test_ping_returns_content_wrapper(self):
        """ping must return {success: True, content: {module, version}}."""
        result_data = json.loads(ping())
        assert result_data.get("success") is True
        assert "content" in result_data
        assert isinstance(result_data["content"], dict)
        assert "module" in result_data["content"]
        assert "version" in result_data["content"]
        assert result_data["content"]["module"] == "orf"
        for key in result_data:
            assert key in {"success", "content", "metadata"}, (
                f"Unexpected top-level key: {key}"
            )

    @patch("subprocess.run")
    def test_full_apply_md_flow(self, mock_run, tmp_path, monkeypatch):
        """Test complete apply_md flow with mocked CLI subprocess."""
        import orf.mcp.server as server_module
        monkeypatch.setattr(
            server_module._path_validator,
            "allowed_directories",
            [tmp_path.resolve()],
        )
        input_path = tmp_path / "input.md"
        input_path.write_text("# test\n", encoding="utf-8")

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "success": True,
                "output_path": "/tmp/result.docx",
                "errors": [],
                "warnings": [],
                "metadata": {"format": "docx"},
            }),
        )

        result_blocks = asyncio.run(
            _call_tool("apply_md", {
                "input_md": str(input_path),
                "target_format": "docx",
            })
        )
        result_data = json.loads(result_blocks[0].text)
        assert result_data["success"] is True
        assert "content" in result_data
        assert isinstance(result_data["content"], dict)