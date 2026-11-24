"""Tests for context_dir mechanism (improvement #2).

When an agent passes a context_dir parameter, relative paths in the
tool call get resolved against it before path validation.
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_mcp_cwd(tmp_path, monkeypatch):
    """Run each test with cwd and the MCP allowlist pinned to ``tmp_path``.

    The tool writes inline content into ``Path.cwd()`` and validates all
    paths against the allowlist. Pinning both keeps these tests
    independent of the repository root's contents (issue #99) and of
    ambient ``MCP_ALLOWED_DIRECTORIES`` in the developer's shell, which
    takes precedence over ``ORF_MCP_ALLOWED_DIRS``.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_ALLOWED_DIRECTORIES", raising=False)
    monkeypatch.setenv("ORF_MCP_ALLOWED_DIRS", str(tmp_path))

    import orf.mcp.common as mcp_common

    saved_config = mcp_common._config
    saved_validator = mcp_common._path_validator
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_ALLOWED_DIRECTORIES", raising=False)
    monkeypatch.setenv("ORF_MCP_ALLOWED_DIRS", str(tmp_path))
    mcp_common.reset_config_and_validator()
    yield tmp_path
    # Restore the pre-test cached singletons by assignment instead of
    # leaving the cache empty: monkeypatch undoes the env vars only
    # *after* this teardown, so a lazily rebuilt validator would carry a
    # stale identity and break downstream tests that patch the instance
    # that existed before this file ran (e.g. the allow_tmp_paths
    # fixture in test_mcp_images_json_canonical_shape.py).
    mcp_common._config = saved_config
    mcp_common._path_validator = saved_validator


def test_context_dir_resolves_relative_reference_doc(tmp_path):
    """When context_dir is set, a relative reference_doc path is resolved
    against context_dir before the path validator runs."""
    from orf.mcp.tools.apply_md import apply_md
    from orf.mcp import server as srv

    captured = {}
    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        return {"success": True, "output_path": "ctx_out.docx"}

    # Create a context subdir + put a fake reference doc inside
    ctx_dir = tmp_path / "test_context_dir"
    ctx_dir.mkdir()
    ref_name = "fake_ref.docx"
    ref_path = ctx_dir / ref_name
    ref_path.write_bytes(b"PK\x03\x04fake")  # fake docx bytes

    orig = srv._run_cli_command
    srv._run_cli_command = fake_run
    try:
        result = apply_md(
            content="# Hello",
            target_format="docx",
            reference_doc=ref_name,  # relative path
            context_dir=str(tmp_path),
            output_path="ctx_out.docx",
        )
    finally:
        srv._run_cli_command = orig

    data = json.loads(result)
    assert data["success"] is True, f"unexpected result: {data}"

    # The --reference-doc arg should be the absolute path, not the relative
    ref_idx = captured["args"].index("--reference-doc")
    ref_arg = captured["args"][ref_idx + 1]
    assert ref_arg == os.path.join(str(tmp_path), ref_name), (
        f"context_dir did not resolve relative path. got: {ref_arg}"
    )


def test_context_dir_absolute_path_unchanged(tmp_path):
    """Absolute paths win over context_dir (don't get re-prepended)."""
    from orf.mcp.tools.apply_md import apply_md
    from orf.mcp import server as srv

    # Create a real ref file so the path validation passes
    ref_abs = tmp_path / "_test_absolute_ref.docx"
    ref_abs.write_bytes(b"PK\x03\x04fake")

    captured = {}
    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        return {"success": True, "output_path": "ctx_out.docx"}

    orig = srv._run_cli_command
    srv._run_cli_command = fake_run
    try:
        # Absolute reference_doc + valid context_dir (in allowed dirs).
        # The absolute reference_doc should be preserved as-is — context_dir
        # only applies to relative paths.
        result = apply_md(
            content="# Hello",
            target_format="docx",
            reference_doc=str(ref_abs),
            context_dir=str(tmp_path),  # valid (in allowed dirs)
            output_path="ctx_out.docx",
        )
    finally:
        srv._run_cli_command = orig

    data = json.loads(result)
    assert data["success"] is True, f"unexpected result: {data}"
    ref_idx = captured["args"].index("--reference-doc")
    ref_arg = captured["args"][ref_idx + 1]
    assert ref_arg == str(ref_abs), (
        f"absolute path was modified: {ref_arg}"
    )


def test_context_dir_none_uses_path_as_is():
    """When context_dir is None, the path is used as-is (backward compat)."""
    from orf.mcp.tools.apply_md import apply_md
    from orf.mcp import server as srv

    captured = {}
    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        return {"success": True, "output_path": "out.docx"}

    orig = srv._run_cli_command
    srv._run_cli_command = fake_run
    try:
        result = apply_md(
            content="# Hello",
            target_format="docx",
            reference_doc="just_relative.docx",  # no context_dir
            output_path="out.docx",
        )
    finally:
        srv._run_cli_command = orig

    data = json.loads(result)
    # context_dir stayed None, so the relative path must reach the CLI
    # unchanged rather than being re-anchored to a context dir.
    ref_idx = captured["args"].index("--reference-doc")
    ref_arg = captured["args"][ref_idx + 1]
    assert ref_arg == "just_relative.docx", (
        f"no-context_dir path was modified: {ref_arg}"
    )


def test_context_dir_validated_against_allowed_dirs():
    """A context_dir outside the allowed dirs returns PATH_NOT_ALLOWED."""
    from orf.mcp.tools.apply_md import apply_md

    result = apply_md(
        content="# Hello",
        target_format="docx",
        context_dir="/some/path/that/is/not/allowed",
        output_path="out.docx",
    )
    data = json.loads(result)
    assert data["success"] is False
    assert any(
        err.get("code") == "PATH_NOT_ALLOWED" and "context_dir" in err.get("message", "")
        for err in data.get("errors", [])
    )
