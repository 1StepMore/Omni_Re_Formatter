"""Security regression tests for ORF MCP server (C4, C5, C13).

These tests target attack vectors documented in AUDIT_FINDINGS_VERIFIED.md
specific to the Omni-Re-Formatter MCP surface.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


_REPO_ROOT = Path(__file__).parent.parent
_ORF_SRC = _REPO_ROOT / "src"
if str(_ORF_SRC) not in sys.path:
    sys.path.insert(0, str(_ORF_SRC))

# Round 13: set ORF_MCP_ALLOWED_DIRS so the MCP PathValidator singleton
# (created on first import of orf.mcp.server) allows tmp_path fixtures.
# 2026-09-17 (ADR 0007): use ``os.pathsep`` + the real temp dir instead of the
# POSIX literal "/tmp" separator — on Windows the old form became a single
# bogus path. POSIX produces the identical string.
if "ORF_MCP_ALLOWED_DIRS" not in os.environ:
    os.environ["ORF_MCP_ALLOWED_DIRS"] = os.pathsep.join(
        [str(_REPO_ROOT), tempfile.gettempdir()]
    )


# 1x1 transparent PNG, base64.
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwGhQGI/UOEQAAAASUVORK5CYII="
)


# ---------------------------------------------------------------------------
# C4: ORF MCP apply_xliff must reject images[].file_path
# ---------------------------------------------------------------------------
#
# Audit: xliff2docx.py:903 (and 3 sibling channels) do
#   img_bytes = Path(img_dict["file_path"]).read_bytes()
# with no path validation. Attacker submits
#   images: [{"file_path": "/etc/passwd", "mime_type": "image/png"}]
# and ORF embeds the file as base64.
#
# Fix: at the MCP layer, reject `file_path` in image placements and require
# `data_base64`. Channels also validate defensively.
# ---------------------------------------------------------------------------


class TestC4RejectsImageFilePathArbitraryRead:
    """C4: passing a file_path in images must not read arbitrary files."""

    def test_apply_xliff_rejects_image_with_file_path(self, tmp_path):
        from orf.mcp import server as orf_mcp

        # Set up input artifacts
        input_file = tmp_path / "input.docx"
        input_file.write_bytes(b"fake-docx")
        xliff_path = tmp_path / "trans.xlf"
        xliff_path.write_text(
            '<?xml version="1.0"?><xliff version="1.2"></xliff>'
        )

        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET-CONTENT-CANARY-42")

        images = [
            {
                "file_path": str(secret),
                "mime_type": "image/png",
            }
        ]

        # Mock _run_cli_command to detect whether the request was sent to the CLI
        with patch.object(orf_mcp, "_run_cli_command") as mock_cli:
            mock_cli.return_value = {"success": True, "output_path": "out.docx"}
            result_str = orf_mcp.apply_xliff(
                input_file=str(input_file),
                xliff_path=str(xliff_path),
                output_path=str(tmp_path / "out.docx"),
                format="docx",
                images=images,
            )
            result = json.loads(result_str)

        # C4 fix: the request must be rejected OR the file_path image
        # dropped silently. We check both paths:
        # (a) If rejected with error code containing FILE_PATH_NOT_ALLOWED,
        #     the secret must remain untouched and CLI must NOT have been
        #     called with --images-json (which would contain the read data).
        # (b) If accepted, the CLI was called but the file_path image
        #     should have been dropped (so secret is not in CLI args).
        secret_intact = secret.read_text() == "SECRET-CONTENT-CANARY-42"

        if result.get("success") is False:
            # Rejection path: ensure CLI was NOT called with secret content
            err_codes = [e.get("code", "") for e in result.get("errors", [])]
            assert any("FILE_PATH" in c or "PATH_NOT_ALLOWED" in c for c in err_codes), (
                f"C4: rejection should include FILE_PATH error code, got: {result}"
            )
        else:
            # Accepted path: ensure secret content is NOT in the CLI args
            # (i.e. file_path was dropped, not read)
            for call in mock_cli.call_args_list:
                args, _ = call
                # args[0] is the argv list passed to _run_cli_command
                if args and isinstance(args[0], list):
                    cmd_str = " ".join(str(a) for a in args[0])
                    assert "SECRET-CONTENT-CANARY-42" not in cmd_str, (
                        "C4 BUG: secret content was passed to CLI subprocess"
                    )

        assert secret_intact, "C4 BUG: secret file was modified or read"

    def test_apply_xliff_rejects_path_traversal_in_file_path(self, tmp_path):
        """C4: file_path with `..` must be rejected or sanitized."""
        from orf.mcp import server as orf_mcp

        input_file = tmp_path / "input.docx"
        input_file.write_bytes(b"fake-docx")
        xliff_path = tmp_path / "trans.xlf"
        xliff_path.write_text(
            '<?xml version="1.0"?><xliff version="1.2"></xliff>'
        )

        images = [{"file_path": "../../../../../../etc/passwd"}]

        with patch.object(orf_mcp, "_run_cli_command") as mock_cli:
            mock_cli.return_value = {"success": True, "output_path": "out.docx"}
            result_str = orf_mcp.apply_xliff(
                input_file=str(input_file),
                xliff_path=str(xliff_path),
                output_path=str(tmp_path / "out.docx"),
                format="docx",
                images=images,
            )
            result = json.loads(result_str)

        # Either rejected or sanitized (file_path image dropped)
        if result.get("success") is False:
            err_codes = [e.get("code", "") for e in result.get("errors", [])]
            assert any("PATH" in c for c in err_codes), (
                f"C4 BUG: should reject traversal path, got: {result}"
            )
        else:
            # If accepted, verify /etc/passwd content was NOT passed to CLI
            for call in mock_cli.call_args_list:
                args, _ = call
                if args and isinstance(args[0], list):
                    cmd_str = " ".join(str(a) for a in args[0])
                    # 2026-09-17: 这里原先是「取到 cmd_str 后什么都不做」的空循环
                    # （只有注释 + pass），F841 就此长期存在。补上注释所声称的那条
                    # 断言：被拒/被清洗时，原始遍历路径不得逐字传给 CLI。
                    assert "/etc/passwd" not in cmd_str, (
                        f"C4 BUG: raw traversal path passed to CLI: {cmd_str}"
                    )

    def test_apply_xliff_accepts_data_base64_only(self, tmp_path):
        """C4: valid data_base64 images must still work."""
        import os
        from orf.mcp.common import reset_config_and_validator
        os.environ["ORF_MCP_ALLOWED_DIRS"] = str(tmp_path)
        reset_config_and_validator()
        from orf.mcp import server as orf_mcp

        input_file = tmp_path / "input.docx"
        input_file.write_bytes(b"fake-docx")
        xliff_path = tmp_path / "trans.xlf"
        xliff_path.write_text(
            '<?xml version="1.0"?><xliff version="1.2"></xliff>'
        )

        images = [
            {
                "data_base64": TINY_PNG_BASE64,
                "mime_type": "image/png",
                "paragraph_index": 0,
            }
        ]

        with patch.object(orf_mcp, "_run_cli_command") as mock_cli:
            mock_cli.return_value = {"success": True, "output_path": "out.docx"}
            result_str = orf_mcp.apply_xliff(
                input_file=str(input_file),
                xliff_path=str(xliff_path),
                output_path=str(tmp_path / "out.docx"),
                format="docx",
                images=images,
            )
            result = json.loads(result_str)
        # Pre-fix this returns success=True. The C4 fix should still allow
        # data_base64 images to work.
        assert result.get("success") is True, (
            f"data_base64 image should still work, got: {result}"
        )


# ---------------------------------------------------------------------------
# C5: ORF MCP apply_xliff output_path must be validated
# ---------------------------------------------------------------------------
#
# Audit: server.py:182-187 builds `args = [..., "--output", output_path, ...]`
# then runs the CLI subprocess. The output_path is unvalidated — attacker
# can overwrite ~/.ssh/authorized_keys or /etc/cron.d/*.
# ---------------------------------------------------------------------------


class TestC5OutputPathValidation:
    """C5: output_path must be validated against the allowlist."""

    def test_apply_xliff_rejects_output_path_with_traversal(self, tmp_path):
        from orf.mcp import server as orf_mcp

        input_file = tmp_path / "input.docx"
        input_file.write_bytes(b"fake-docx")
        xliff_path = tmp_path / "trans.xlf"
        xliff_path.write_text(
            '<?xml version="1.0"?><xliff version="1.2"></xliff>'
        )

        # Try to write to ~/.ssh/authorized_keys (or similar sensitive path)
        dangerous_output = "/tmp/../tmp/../etc/cron.d/malicious"

        with patch.object(orf_mcp, "_run_cli_command") as mock_cli:
            mock_cli.return_value = {"success": True}
            result_str = orf_mcp.apply_xliff(
                input_file=str(input_file),
                xliff_path=str(xliff_path),
                output_path=dangerous_output,
                format="docx",
            )
            result = json.loads(result_str)

        # The fix must reject this. Either error or it normalizes to safe.
        assert result.get("success") is False, (
            f"C5 BUG: apply_xliff accepted dangerous output_path: {result}"
        )
        err_codes = [e.get("code", "") for e in result.get("errors", [])]
        assert any("PATH" in c for c in err_codes), (
            f"C5 BUG: should reject dangerous output_path, got errors: {err_codes}"
        )

    def test_apply_xliff_accepts_safe_output_path(self, tmp_path):
        """C5: a safe output path inside the input dir must still work."""
        import os
        from orf.mcp.common import reset_config_and_validator
        os.environ["ORF_MCP_ALLOWED_DIRS"] = str(tmp_path)
        reset_config_and_validator()
        from orf.mcp import server as orf_mcp

        input_file = tmp_path / "input.docx"
        input_file.write_bytes(b"fake-docx")
        xliff_path = tmp_path / "trans.xlf"
        xliff_path.write_text(
            '<?xml version="1.0"?><xliff version="1.2"></xliff>'
        )

        safe_output = str(tmp_path / "out.docx")

        with patch.object(orf_mcp, "_run_cli_command") as mock_cli:
            mock_cli.return_value = {"success": True, "output_path": safe_output}
            result_str = orf_mcp.apply_xliff(
                input_file=str(input_file),
                xliff_path=str(xliff_path),
                output_path=safe_output,
                format="docx",
            )
            result = json.loads(result_str)
        # Pre-fix, this returns success. C5 fix should not break the happy path.
        assert result.get("success") is True, (
            f"safe output_path should work, got: {result}"
        )


# ---------------------------------------------------------------------------
# C13: xliff2docx floating image XML injection via relativeFrom
# ---------------------------------------------------------------------------
#
# Audit: xliff2docx.py:855-897 builds XML with f-strings and unescaped
# relativeFrom values. Attacker injects XML via MCP input.
# ---------------------------------------------------------------------------


class TestC13XMLInjectionViaRelativeFrom:
    """C13: relativeFrom values must be validated against allowlist."""

    def test_relative_h_with_injection_chars_rejected(self, tmp_path):
        """Attacker value containing </wp:positionH> must be rejected."""
        from orf.channels.xliff2docx import XLIFF2DOCXConverter
        from orf.mcp.schemas import ImagePlacement

        converter = XLIFF2DOCXConverter()
        img = ImagePlacement(
            data_base64=TINY_PNG_BASE64,
            mime_type="image/png",
            paragraph_index=None,
            is_floating=True,
            wp_anchor_h=1_000_000,
            wp_anchor_v=2_000_000,
            wp_anchor_relative_h='</wp:positionH><script>alert(1)</script>',
            wp_anchor_relative_v="page",
        )

        # C13 fix: must raise ValueError (allowlist rejects injection chars).
        with pytest.raises(ValueError) as exc_info:
            converter._create_floating_anchor_xml(
                rId="rId1", cx=200, cy=200, pos_h=1_000_000, pos_v=2_000_000,
                relative_h=img.wp_anchor_relative_h,
                relative_v=img.wp_anchor_relative_v,
            )
        # The error message must mention the allowlist
        assert "allowlist" in str(exc_info.value).lower()

    def test_relative_h_valid_values_accepted(self):
        """Sanity: legitimate relativeFrom values produce valid output."""
        from orf.channels.xliff2docx import XLIFF2DOCXConverter

        converter = XLIFF2DOCXConverter()
        for val in ("page", "column", "margin", "paragraph", "line", "character"):
            xml_str = converter._create_floating_anchor_xml(
                rId="rId1", cx=200, cy=200, pos_h=0, pos_v=0,
                relative_h=val, relative_v="page",
            )
            assert f'relativeFrom="{val}"' in xml_str

    def test_relative_h_unknown_value_rejected(self):
        """An unknown but otherwise safe-looking value must be rejected."""
        from orf.channels.xliff2docx import XLIFF2DOCXConverter

        converter = XLIFF2DOCXConverter()
        with pytest.raises((ValueError, Exception)):
            converter._create_floating_anchor_xml(
                rId="rId1", cx=200, cy=200, pos_h=0, pos_v=0,
                relative_h="not_a_real_relativeFrom",  # not in allowlist
                relative_v="page",
            )
