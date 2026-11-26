"""Issue #102: a missing ``--output`` parent directory must not mislead.

Reproduced on a clean checkout (685d7db, XLIFF produced by OL): ``orf
apply-xliff normal.docx --xliff normal.xlf --output /tmp/orf_out/out.docx``
with ``/tmp/orf_out`` absent failed deep in the converter with

    Failed to repack DOCX: [Errno 2] No such file or directory:
    '/tmp/orf_out/out.docx'

and all three hints pointed at the XLIFF / the input document. The real
cause — the output parent directory does not exist — never appeared in
the message, and agents retried against the wrong things.

Fix: apply-md / apply-xliff create the output parent (mkdir -p) and
convert-batch creates ``--output-dir`` before anything writes; when the
directory genuinely cannot be created the error names the path and the
reason. The MCP tools (apply_md / apply_xliff / batch_convert) delegate
to these CLI commands via ``_run_cli_command``, so they inherit the fix —
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


class TestApplyXliffMissingParentDir:
    def test_missing_parent_dir_is_created(self, tmp_path):
        """apply-xliff into a non-existent directory must succeed (mkdir -p).

        Before the fix this exited non-zero with "Failed to repack DOCX:
        [Errno 2] No such file or directory" and hints blaming the XLIFF
        and the input document.
        """
        skeleton = tmp_path / "input.skeleton.zip"
        _make_docx_skeleton(skeleton)
        xlf = tmp_path / "normal.xlf"
        xlf.write_text(_MINIMAL_XLIFF, encoding="utf-8")
        output = tmp_path / "orf_out" / "normal_zh.docx"

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

        assert result.exit_code == 0, (
            f"expected rc=0 with auto-created parent dir; got "
            f"rc={result.exit_code}\n{result.output}"
        )
        assert output.exists(), "output file was not written"
        assert output.stat().st_size > 0

    def test_uncreatable_parent_dir_reports_directory_and_path(self, tmp_path):
        """When the parent cannot be created, the error must say so.

        A regular *file* occupies the parent path, so mkdir -p cannot
        succeed. The failure must name "directory" and the path — never
        the misleading "Failed to repack DOCX".
        """
        skeleton = tmp_path / "input.skeleton.zip"
        _make_docx_skeleton(skeleton)
        xlf = tmp_path / "normal.xlf"
        xlf.write_text(_MINIMAL_XLIFF, encoding="utf-8")
        blocker = tmp_path / "blocker"
        blocker.write_text("a regular file, not a directory", encoding="utf-8")
        output = blocker / "out.docx"

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
            f"expected non-zero exit when the output dir is uncreatable\n"
            f"{result.output}"
        )
        assert "directory" in result.output.lower(), result.output
        assert str(blocker) in result.output, result.output
        assert "Failed to repack" not in result.output, (
            f"the misleading repack error must not resurface:\n{result.output}"
        )
        assert not output.exists()


class TestApplyMdMissingParentDir:
    def test_missing_parent_dir_is_created(self, tmp_path):
        """apply-md into a non-existent directory must succeed (mkdir -p)."""
        md = tmp_path / "hello.md"
        md.write_text(_MD_WITH_FRONTMATTER, encoding="utf-8")
        # html target: the md2html channel is pandoc-independent, so this
        # test does not depend on a pandoc install.
        output = tmp_path / "orf_out" / "hello.html"

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

        assert result.exit_code == 0, (
            f"expected rc=0 with auto-created parent dir; got "
            f"rc={result.exit_code}\n{result.output}"
        )
        assert output.exists(), "output file was not written"


class TestConvertBatchMissingOutputDir:
    def test_missing_output_dir_is_created(self, tmp_path):
        """convert-batch --output-dir pointing at a new dir must succeed."""
        input_dir = tmp_path / "md_in"
        input_dir.mkdir()
        (input_dir / "hello.md").write_text(
            _MD_WITH_FRONTMATTER, encoding="utf-8"
        )
        output_dir = tmp_path / "orf_out_batch"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "convert-batch", str(input_dir),
                "--target-format", "html",
                "--output-dir", str(output_dir),
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"expected rc=0 with auto-created output dir; got "
            f"rc={result.exit_code}\n{result.output}"
        )
        assert (output_dir / "hello.html").exists()


class TestMcpApplyXliffMissingParentDir:
    """The MCP apply_xliff tool delegates to the CLI; it must inherit the fix."""

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

    def test_mcp_apply_xliff_creates_missing_parent_dir(self, tmp_path):
        from orf.mcp.tools.apply_xliff import apply_xliff

        skeleton = tmp_path / "input.skeleton.zip"
        _make_docx_skeleton(skeleton)
        xlf = tmp_path / "normal.xlf"
        xlf.write_text(_MINIMAL_XLIFF, encoding="utf-8")
        output = tmp_path / "orf_out_mcp" / "normal_zh.docx"

        result = apply_xliff(
            input_file=str(skeleton),
            xliff_path=str(xlf),
            output_path=str(output),
            format="docx",
        )
        data = json.loads(result)

        assert data.get("success") is True, f"unexpected MCP result: {result}"
        assert output.exists(), "MCP output file was not written"
