"""E2E-76 regression tests.

The bug: ``ORF MCP apply_md`` declared its input as ``input_md: str``
(documenting it as "Path to input markdown file.") and the function
body ran every value through ``PathValidator.validate_path`` — a path
allowlist. Agent flows that receive inline markdown content (e.g. the
output of OL's ``translate_md_text``) had no way to pass it through
MCP without writing a temp file themselves; the function would return
``{"success": false, "errors": [{"code": "PATH_NOT_ALLOWED", ...}]}``
for any inline string.

The fix adds a ``content: str`` parameter to ``apply_md``. When
``content`` is non-empty, the function writes it to a tempfile inside
the allowed directory, runs the CLI on that file, and cleans up after.
``input_md`` and ``content`` are mutually exclusive; supplying neither
is a clear error.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orf.mcp import server


# Use a fake-subprocess pattern so the regression tests don't shell out
# to a real pandoc/CLI subprocess.
_FAKE_CLI_SUCCESS = {
    "success": True,
    "output_path": "/tmp/regression_result.html",
    "errors": [],
    "warnings": [],
    "metadata": {},
}


def _fake_subprocess_run(capture_path_to=None):
    """Patch ``subprocess.run`` with a fake success. When
    ``capture_path_to`` is a list, the markdown path the CLI receives
    is appended to it so the test can read the file content BEFORE
    apply_md's finally block cleans it up.
    """
    if capture_path_to is None:
        return patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=0,
                stdout=json.dumps(_FAKE_CLI_SUCCESS),
                stderr="",
            ),
        )

    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if "apply-md" in cmd:
            idx = cmd.index("apply-md")
            if idx + 1 < len(cmd):
                capture_path_to.append(cmd[idx + 1])
        return MagicMock(
            returncode=0,
            stdout=json.dumps(_FAKE_CLI_SUCCESS),
            stderr="",
        )

    return patch("subprocess.run", side_effect=side_effect)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Run each test with cwd and the MCP allowlist pinned to ``tmp_path``.

    ``apply_md`` materialises inline content into ``Path.cwd()``. Running
    these tests from the repository root used to make the cleanup
    assertions trip over *other* runs' leftover ``orf_mcp_inline_*``
    files (issue #99). Pinning the cwd also keeps the allowlist
    independent of ambient ``MCP_ALLOWED_DIRECTORIES`` in the developer's
    shell, which takes precedence over ``ORF_MCP_ALLOWED_DIRS``.
    """
    import orf.mcp.common as mcp_common

    saved_config = mcp_common._config
    saved_validator = mcp_common._path_validator
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MCP_ALLOWED_DIRECTORIES", raising=False)
    monkeypatch.setenv("ORF_MCP_ALLOWED_DIRS", str(tmp_path))
    mcp_common.reset_config_and_validator()
    yield
    # Restore the pre-test cached singletons by assignment instead of
    # leaving the cache empty: monkeypatch undoes the env vars only
    # *after* this teardown, so a lazily rebuilt validator would carry a
    # stale identity and break downstream tests that patch the instance
    # that existed before this file ran (e.g. the allow_tmp_paths
    # fixture in test_mcp_images_json_canonical_shape.py).
    mcp_common._config = saved_config
    mcp_common._path_validator = saved_validator


@pytest.fixture
def cwd_md_file(tmp_path):
    """Create a real .md file in the isolated cwd and clean it up."""
    p = tmp_path / "_test_e2e_76_input.md"
    p.write_text("# existing path-based call", encoding="utf-8")
    try:
        yield p
    finally:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture
def cwd_output_path(tmp_path):
    p = tmp_path / "_test_e2e_76_output.html"
    try:
        yield str(p)
    finally:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def _cleanup_inline_tempfiles(tmp_path):
    """Best-effort cleanup of any tempfiles the test session may have
    leaked into the isolated cwd (the apply_md function should clean
    these up automatically, but a failure in cleanup should not cascade
    to other tests)."""
    yield
    for p in tmp_path.glob("_test_e2e_76_*"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    for p in tmp_path.glob("orf_mcp_inline_*.md"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


class TestApplyMdAcceptsInlineContent:
    """``content`` must work the same as a path-based ``input_md``."""

    def test_content_param_exists_in_signature(self):
        params = inspect.signature(server.apply_md).parameters
        assert "content" in params, (
            "apply_md must accept an inline ``content`` parameter so "
            "text-in/text-out agent flows can chain OL translate_md_text "
            "→ ORF apply_md without writing a temp file themselves."
        )

    def test_content_param_is_optional(self):
        content_param = inspect.signature(server.apply_md).parameters["content"]
        assert content_param.default is None, (
            "content must default to None so existing path-based callers "
            "keep working unchanged."
        )

    def test_content_passes_a_written_file_to_cli(self, cwd_output_path):
        """When ``content`` is supplied, the CLI must receive a path to
        a tempfile that contains the inline markdown. We capture the
        path during the mock call (BEFORE apply_md's finally block
        cleans it up), then verify the cleanup happened.
        """
        captured: list[str] = []
        with _fake_subprocess_run(capture_path_to=captured):
            result_str = server.apply_md(
                content="# Hello\nThis is inline.",
                target_format="html",
                output_path=cwd_output_path,
            )
        parsed = json.loads(result_str)
        assert parsed["success"] is True, (
            f"Inline content via content= must succeed. Got: {parsed}"
        )
        assert captured, "subprocess.run was not called with a path to capture"
        md_path = captured[0]
        # The mock snapshot was taken during the CLI call, so the file
        # must have existed at that point. After apply_md returns,
        # the finally block has deleted it.
        assert Path(md_path).name.startswith("orf_mcp_inline_"), (
            f"Captured path should be a tempfile named orf_mcp_inline_*.md. "
            f"Got: {md_path}"
        )
        assert md_path.endswith(".md"), f"Expected .md suffix, got: {md_path}"
        assert not Path(md_path).exists(), (
            f"Tempfile must be deleted after the call. Still exists: {md_path}"
        )

    def test_tempfile_cleaned_up_after_call(self, cwd_output_path, tmp_path):
        """The tempfile holding the inline content must be deleted once
        the CLI call returns, so the agent's working directory doesn't
        accumulate junk files."""
        with _fake_subprocess_run():
            server.apply_md(
                content="# cleanup me",
                target_format="html",
                output_path=cwd_output_path,
            )
        leftovers = list(tmp_path.glob("orf_mcp_inline_*.md"))
        assert leftovers == [], (
            f"Tempfile cleanup failed. Leftover files: {leftovers}"
        )


class TestApplyMdMutualExclusivity:
    """Exactly one of ``input_md`` or ``content`` must be supplied."""

    def test_both_input_md_and_content_rejected(self, cwd_md_file, cwd_output_path):
        result = server.apply_md(
            input_md=str(cwd_md_file),
            content="# hello",
            target_format="html",
            output_path=cwd_output_path,
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["errors"][0]["code"] == "MUTUALLY_EXCLUSIVE"

    def test_neither_input_md_nor_content_rejected(self, cwd_output_path):
        result = server.apply_md(
            target_format="html",
            output_path=cwd_output_path,
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["errors"][0]["code"] == "MISSING_INPUT"


class TestApplyMdBackwardCompat:
    """Existing path-based callers must keep working unchanged."""

    def test_input_md_path_still_works(self, cwd_md_file, cwd_output_path):
        with _fake_subprocess_run() as mock_run:
            result = server.apply_md(
                input_md=str(cwd_md_file),
                target_format="html",
                output_path=cwd_output_path,
            )
        parsed = json.loads(result)
        assert parsed["success"] is True
        # The CLI must have received the original input_md path
        # (NOT a tempfile we created).
        args = mock_run.call_args[0][0]
        apply_md_idx = args.index("apply-md")
        assert args[apply_md_idx + 1] == str(cwd_md_file), (
            f"Path-based call must pass the original input_md path to the "
            f"CLI. Got: {args[apply_md_idx + 1]!r}"
        )


class TestMcpSchemaAdvertisesContent:
    """The MCP tool schema must tell clients about the ``content`` param."""

    def test_schema_lists_content_param(self):
        from orf.mcp.server import _list_tools
        import asyncio

        tools = asyncio.run(_list_tools())
        apply_md_tool = next(t for t in tools if t.name == "apply_md")
        props = apply_md_tool.inputSchema["properties"]
        assert "content" in props, (
            f"MCP schema must advertise the new ``content`` parameter. "
            f"Current properties: {list(props.keys())}"
        )
        assert props["content"]["type"] == "string"

    def test_schema_allows_either_input_md_or_content(self):
        from orf.mcp.server import _list_tools
        import asyncio

        tools = asyncio.run(_list_tools())
        apply_md_tool = next(t for t in tools if t.name == "apply_md")
        # The schema must allow callers to supply content instead of
        # input_md. We use anyOf to express the disjunction.
        any_of = apply_md_tool.inputSchema.get("anyOf", [])
        assert any_of, (
            f"Schema must use anyOf to allow (input_md, target_format) or "
            f"(content, target_format). Got: {apply_md_tool.inputSchema}"
        )
        content_variants = [
            v for v in any_of if "content" in v.get("required", [])
        ]
        assert content_variants, (
            f"anyOf must include a variant requiring content. Got: {any_of}"
        )
