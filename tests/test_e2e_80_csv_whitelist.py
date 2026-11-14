"""E2E-80 regression tests.

The bug: PathValidator.ALLOWED_EXTENSIONS listed only the primary
input formats (md, docx, pptx, xliff, xlf, xml, html, odt, epub,
zip). All of the output formats advertised in the ORF README
(csv, tsv, xlsx, json, ipynb, eml, msg, srt, icml, rtf, pdf) were
rejected by the path validator with
'Extension \".csv\" not in allowed set' even though ORF clearly
supports them. apply-md --target-format csv -o result.csv was
effectively unusable over the MCP path.

The fix: extend ALLOWED_EXTENSIONS with all the output formats the
README claims support for. (Both INPUT and OUTPUT paths flow
through the same validator, so the extension set must include
every format ORF can produce.)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from orf.mcp.security import PathValidator


@pytest.fixture
def validator(tmp_path):
    """PathValidator whose allowlist anchors to the real OS temp root.

    The historical literal ``["/tmp", "/var/tmp"]`` is a POSIX convention:
    on Windows the checked files live under the user's temp dir
    (``C:\\Users\\...\\AppData\\Local\\Temp\\...``), so PathValidator
    rejected them with not-within-allowed before the extension check this
    file exists to exercise could even run. Anchoring the allowlist to the
    same root the checked files live in (pytest's per-test ``tmp_path``)
    keeps the test about extensions on every platform.
    """
    return PathValidator(
        allowed_directories=[str(tmp_path)],
        max_file_size_bytes=100_000_000,
    )


class TestAllowedExtensions:
    """All ORF-supported formats must pass the validator."""

    @pytest.mark.parametrize(
        "ext",
        [
            # Original primary input formats
            ".md",
            ".docx",
            ".pptx",
            ".xliff",
            ".xlf",
            ".xml",
            ".html",
            ".odt",
            ".epub",
            ".zip",
            # E2E-80: output formats newly allowed
            ".csv",
            ".tsv",
            ".xlsx",
            ".json",
            ".ipynb",
            ".eml",
            ".msg",
            ".srt",
            ".icml",
            ".rtf",
            ".pdf",
        ],
    )
    def test_extension_allowed(self, validator, ext, tmp_path):
        path = str(tmp_path / f"test{ext}")
        # allow_missing=True because we only care about the extension
        # check, not whether the file actually exists.
        result = validator.validate_path(path, allow_missing=True)
        assert result.success, (
            f"Extension {ext!r} must be in ALLOWED_EXTENSIONS. "
            f"Error: {result.error}"
        )

    @pytest.mark.parametrize(
        "ext",
        [".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".js"],
    )
    def test_blocked_extension_still_rejected(self, validator, ext, tmp_path):
        """Make sure the BLOCKED list (executables) is unaffected."""
        path = str(tmp_path / f"test{ext}")
        result = validator.validate_path(path, allow_missing=True)
        # The extension check itself is gated by ALLOWED, so blocked
        # extensions also fail with 'not in allowed set'. They may
        # never be tried because the .exe / .sh / .ps1 are explicitly
        # in BLOCKED_EXTENSIONS. Either error message is acceptable.
        assert not result.success
        assert result.error is not None
