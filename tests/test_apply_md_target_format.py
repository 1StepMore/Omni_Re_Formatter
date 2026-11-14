"""Round 13+: apply-md --target-format Choice coverage.

Round 10 excluded pptx (md2pptx binary not installed by default).
Round 13 re-added pptx after MartinPacker/md2pptx was installed.

These tests lock in the current Choice composition so future
additions are explicit and tested.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_OL_ROOT = Path(__file__).resolve().parents[1]
_OL_SRC = _OL_ROOT / "src"


def _cli_python() -> Path:
    """Interpreter for the ORF CLI subprocess, resolved platform-aware.

    See tests/test_apply_xliff_format_validation.py::_cli_python — the
    suite-level ``.venv_ol/bin/python`` is a Linux ELF that cannot spawn on
    a Windows-native checkout (WinError 1920); POSIX keeps using it, Windows
    uses the interpreter running pytest, and a missing interpreter fails
    loudly instead of being silently substituted.
    """
    if sys.platform == "win32":
        return Path(sys.executable)
    venv_python = _OL_ROOT.parent / ".venv_ol" / "bin" / "python"
    if not venv_python.is_file():
        pytest.fail(
            f".venv_ol interpreter not found at {venv_python} — the POSIX "
            f"branch of this suite expects the suite-level venv (CI creates "
            f"it via symlink); refusing to silently substitute an interpreter",
            pytrace=False,
        )
    return venv_python


def _orf_subprocess_env() -> dict:
    """Env for ORF CLI subprocesses, per platform.

    POSIX keeps the historical minimal env exactly (PYTHONPATH + venv-first
    PATH with os.pathsep, which is ":" there — the old hardcoded ":").
    Windows additionally inherits os.environ: the minimal dict omits
    SystemRoot/SystemDrive, which Windows child processes need for basic
    runtime initialization.
    """
    env = {
        "PYTHONPATH": str(_OL_SRC),
        "ORF_MCP_ALLOWED_DIRS": os.environ.get("ORF_MCP_ALLOWED_DIRS", "/tmp"),
    }
    if sys.platform == "win32":
        return {**os.environ, **env}
    env["PATH"] = str(_cli_python().parent) + os.pathsep + os.environ.get("PATH", "")
    return env


def _run_apply_md_help() -> str:
    """Capture `orf apply-md --help` output."""
    result = subprocess.run(
        [str(_cli_python()), "-m", "orf", "apply-md", "--help"],
        capture_output=True, text=True, env=_orf_subprocess_env(),
        timeout=15,
    )
    return result.stdout + result.stderr


class TestApplyMdTargetFormatChoice:
    """Lock in the apply-md --target-format Choice composition."""

    def test_help_shows_target_format_choices(self):
        """--target-format must list the 16 supported formats (round 10)."""
        out = _run_apply_md_help()
        m = re.search(r"--target-format \[([^\]]+)\]", out)
        assert m, f"--target-format Choice not found in help:\n{out}"
        choices = m.group(1).split("|")
        # 17 = auto + 16 formats (pptx re-added in round 13)
        assert len(choices) == 17, (
            f"Expected 17 target-format choices, got {len(choices)}: {choices}"
        )

    def test_pptx_included_in_choices(self):
        """PPTX must now be in --target-format Choice (round 13 re-added
        pptx after md2pptx binary was installed)."""
        out = _run_apply_md_help()
        m = re.search(r"--target-format \[([^\]]+)\]", out)
        assert m
        choices = m.group(1).split("|")
        assert "pptx" in choices, (
            f"pptx should be in choices now; md2pptx is installed. Got: {choices}"
        )

    def test_all_standard_data_formats_present(self):
        """docx, html, pdf, xlsx, csv, json, xml, ipynb, eml, epub
        must all be in the Choice (data format coverage)."""
        out = _run_apply_md_help()
        m = re.search(r"--target-format \[([^\]]+)\]", out)
        choices = set(m.group(1).split("|"))
        for fmt in ("docx", "html", "pdf", "xlsx", "csv", "json",
                    "xml", "ipynb", "eml", "epub"):
            assert fmt in choices, (
                f"{fmt} missing from apply-md --target-format Choice"
            )

    def test_invalid_format_rejected(self):
        """A bogus target format must be rejected (click BadParameter)."""
        result = subprocess.run(
            [str(_cli_python()), "-m", "orf", "apply-md",
             "test_fixtures/zh/haier.epub",
             "--target-format", "bogus_format",
             "--output", "/tmp/bogus.out"],
            capture_output=True, text=True, env=_orf_subprocess_env(),
            timeout=15,
        )
        assert result.returncode != 0
        assert "bogus_format" in (result.stdout + result.stderr)