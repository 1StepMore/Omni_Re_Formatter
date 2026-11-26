"""Issue #102: a missing ``--output`` parent directory must not mislead.

Reproduced on a clean checkout (685d7db, XLIFF produced by OL): ``orf
apply-xliff normal.docx --xliff normal.xlf --output /tmp/orf_out/out.docx``
with ``/tmp/orf_out`` absent failed deep in the converter with

    Failed to repack DOCX: [Errno 2] No such file or directory:
    '/tmp/orf_out/out.docx'

and all three hints pointed at the XLIFF / the input document. The real
cause — the output parent directory does not exist — never appeared in
the message, and agents retried against the wrong things.

Contract (deliberately unchanged): a missing output directory is a clean
failure, not an implicit side effect — the suite's validation scenario
``tool-orf-json-failure-exit`` depends on it. What changes is the message:
it names the directory and the fix (``mkdir -p <dir>``), never "Failed to
repack DOCX", and in ``--json`` mode the failure is still exactly one JSON
object with ``success: false``.

The MCP tools (apply_md / apply_xliff / batch_convert) delegate to these
CLI commands via ``_run_cli_command``, so they inherit the same message —
one MCP-level test pins that end to end.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from orf.cli import main

_MINIMAL_XLIFF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
    '  <file source-language="en" target-language="zh" datatype="plaintext">\n'
    "    <body>\n"
    '      <trans-unit id="1">\n'
    "        <source>Hello World</source>\n"
    "        <target>你好世界</target>\n"
    "      </trans-unit>\n"
    "    </body>\n"
    "  </file>\n"
    "</xliff>"
)

_DOCX_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r>
        <w:t>Hello World</w:t>
      </w:r>
    </w:p>
  </w:body>
</w:document>
"""

_MD_WITH_FRONTMATTER = (
    "---\n"
    "source_lang: en\n"
    "target_lang: zh\n"
    "---\n"
    "\n"
    "# Hello World\n"
)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Give every invocation its own cache root (order independence)."""
    monkeypatch.setenv("OMNI_CACHE_DIR", str(tmp_path / "omni_cache"))


def _make_docx_skeleton(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _DOCX_DOCUMENT)
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )


def _assert_missing_dir_error(output: str, missing: Path) -> None:
    lowered = output.lower()
    assert "output directory does not exist" in lowered, output
    assert str(missing) in output, output
    assert "mkdir -p" in output, output
    assert "failed to repack" not in lowered, output


class TestApplyXliffMissingParentDir:
    def test_missing_parent_dir_names_the_directory(self, tmp_path):
        """The failure must name the directory, never "Failed to repack DOCX"."""
        skeleton = tmp_path / "input.skeleton.zip"
        _make_docx_skeleton(skeleton)
        xlf = tmp_path / "normal.xlf"
        xlf.write_text(_MINIMAL_XLIFF, encoding="utf-8")
        missing = tmp_path / "orf_out"
        output = missing / "normal_zh.docx"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "apply-xliff", str(skeleton),
                "--xliff", str(xlf),
                "--output", str(output),
                "--format", "docx",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code != 0, (
            f"expected non-zero exit when the output dir is missing\n{result.output}"
        )
        _assert_missing_dir_error(result.output, missing)
        assert not output.exists(), "no artifact may be produced"
        assert not missing.exists(), "the directory must not be created implicitly"


class TestApplyMdMissingParentDir:
    def test_missing_parent_dir_names_the_directory(self, tmp_path):
        md = tmp_path / "hello.md"
        md.write_text(_MD_WITH_FRONTMATTER, encoding="utf-8")
        missing = tmp_path / "orf_out"
        output = missing / "hello.html"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "apply-md", str(md),
                "--target-format", "html",
                "--output", str(output),
            ],
            catch_exceptions=False,
        )

        assert result.exit_code != 0, result.output
        _assert_missing_dir_error(result.output, missing)
        assert not output.exists()

    def test_json_mode_emits_one_object_with_success_false(self, tmp_path):
        """--json must keep stdout a single JSON object on this failure."""
        md = tmp_path / "hello.md"
        md.write_text(_MD_WITH_FRONTMATTER, encoding="utf-8")
        missing = tmp_path / "orf_out_json"
        output = missing / "hello.html"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "apply-md", str(md),
                "--target-format", "html",
                "--output", str(output),
                "--json",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert payload["errors"][0]["code"] == "OUTPUT_DIR_MISSING"
        assert str(missing) in payload["errors"][0]["message"]
        assert not output.exists()


class TestConvertBatchKeepsPerFileContract:
    def test_batch_json_still_reports_fail_count(self, tmp_path):
        """convert-batch keeps its per-file contract: a missing --output-dir
        fails each conversion and the JSON summary reports fail_count.

        (#102 fixes the single-file apply-md / apply-xliff message; the batch
        path intentionally keeps the summary shape the suite validates.)
        """
        input_dir = tmp_path / "md_in"
        input_dir.mkdir()
        (input_dir / "hello.md").write_text(_MD_WITH_FRONTMATTER, encoding="utf-8")
        missing = tmp_path / "orf_out_batch"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "convert-batch", str(input_dir),
                "--target-format", "html",
                "--output-dir", str(missing),
                "--json",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code != 0, f"stdout: {result.stdout!r}"
        payload = json.loads(result.stdout)
        assert payload["fail_count"] == 1
        assert not (missing / "hello.html").exists()


class TestMcpApplyXliffMissingParentDir:
    """The MCP apply_xliff tool delegates to the CLI; it must inherit the message."""

    @pytest.fixture(autouse=True)
    def _allowed_dirs(self, tmp_path, monkeypatch):
        """Pin the MCP allowlist to tmp_path (see test_e2e_78_context_dir)."""
        monkeypatch.delenv("MCP_ALLOWED_DIRECTORIES", raising=False)
        monkeypatch.setenv("ORF_MCP_ALLOWED_DIRS", str(tmp_path))
        import orf.mcp.common as mcp_common

        saved_config = mcp_common._config
        saved_validator = mcp_common._path_validator
        mcp_common.reset_config_and_validator()
        yield
        mcp_common._config = saved_config
        mcp_common._path_validator = saved_validator

    def test_mcp_apply_xliff_reports_missing_directory(self, tmp_path):
        from orf.mcp.tools.apply_xliff import apply_xliff

        skeleton = tmp_path / "input.skeleton.zip"
        _make_docx_skeleton(skeleton)
        xlf = tmp_path / "normal.xlf"
        xlf.write_text(_MINIMAL_XLIFF, encoding="utf-8")
        missing = tmp_path / "orf_out_mcp"
        output = missing / "normal_zh.docx"

        result = apply_xliff(
            input_file=str(skeleton),
            xliff_path=str(xlf),
            output_path=str(output),
            format="docx",
        )
        data = json.loads(result)

        assert data.get("success") is False, f"unexpected MCP result: {result}"
        assert "directory" in json.dumps(data, ensure_ascii=False).lower()
        assert not output.exists()
