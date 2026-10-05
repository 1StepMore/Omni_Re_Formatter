"""FIX-#8: apply-xliff must fail early on a mismatched skeleton.

Regression test for the round 5 fix that adds a format-preservation
check in ORF's `apply-xliff` CLI command. Before the fix, passing
a DOCX skeleton with `--format=odt` would crash deep in
translate-toolkit with an abstract error. After the fix, it raises
a clear click.BadParameter pointing to the format mismatch.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

_ORF_SRC = Path(__file__).resolve().parents[1] / "src"
_VENV_PYTHON = Path(__file__).resolve().parents[2] / ".venv_ol" / "bin" / "python"


def _run_orf_cli(*args: str) -> subprocess.CompletedProcess:
    """Run ORF CLI as a subprocess (matches production invocation).

    ORF keeps a content-addressed output cache under ``OMNI_CACHE_DIR``
    (default ``~/.omni_cache``). The cache check runs before the conversion, so
    a warm entry short-circuits it and prints ``Created ... (cached)`` instead of
    the real result -- these CLI-contract tests would then depend on whatever ran
    before them in the same job. Give every invocation its own cache root to keep
    them order-independent.
    """
    cmd = [str(_VENV_PYTHON), "-m", "orf", *args]
    with tempfile.TemporaryDirectory(prefix="orf-cli-cache-") as cache_dir:
        env = {
            **os.environ,
            "PYTHONPATH": str(_ORF_SRC),
            "OMNI_CACHE_DIR": cache_dir,
        }
        return subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=30
        )


def test_apply_xliff_rejects_docx_skeleton_with_odt_format(tmp_path):
    """docx skeleton + --format=odt must fail fast with a clear message.

    Before FIX-#8, the call would proceed and crash in translate-toolkit
    with an abstract 'no content.xml' error. After the fix, it raises
    click.BadParameter before any I/O.
    """
    # Create fake skeleton + xlf + output paths (contents don't matter;
    # the format check fires before any file content is read).
    fake_skeleton = tmp_path / "input.docx"
    fake_skeleton.write_bytes(b"PK\x03\x04")  # zip magic (would be valid DOCX)
    fake_xlf = tmp_path / "translation.xlf"
    fake_xlf.write_text('<?xml version="1.0"?><xliff/>')
    output = tmp_path / "out.odt"

    result = _run_orf_cli(
        "apply-xliff", str(fake_skeleton),
        "--xliff", str(fake_xlf),
        "--output", str(output),
        "--format", "odt",
    )
    # Should fail with non-zero exit
    assert result.returncode != 0, (
        f"Expected non-zero exit on format mismatch; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Error message should mention the format mismatch
    combined = result.stdout + result.stderr
    assert "odt" in combined.lower(), (
        f"Error message should mention 'odt'; got: {combined}"
    )
    assert "format" in combined.lower() or "skeleton" in combined.lower(), (
        f"Error message should mention format or skeleton; got: {combined}"
    )


def test_apply_xliff_accepts_xlf_input_with_format(tmp_path):
    """`.xlf` or `.xliff` input is skeleton-agnostic — the format check skips.

    The check is permissive when the input is a generic XLIFF file
    (skeleton inferred from the --format value). The XLIFF dispatch
    in ORF handles this correctly via the ODF converter's separate
    skeleton discovery.
    """
    fake_xlf = tmp_path / "translation.xlf"
    fake_xlf.write_text('<?xml version="1.0"?><xliff/>')
    output = tmp_path / "out.docx"

    result = _run_orf_cli(
        "apply-xliff", str(fake_xlf),
        "--xliff", str(fake_xlf),
        "--output", str(output),
        "--format", "docx",
    )
    # We don't care about success/failure here — we just want to confirm
    # the early format check did NOT fire (no "format" / "skeleton" error).
    combined = result.stdout + result.stderr
    assert "does not match" not in combined, (
        f"Generic .xlf should bypass format check; got: {combined}"
    )


class TestZipSkeletonAccepted:
    """FIX-#8 round 9: OPP packages skeleton as .zip; ORF must accept.

    Without this fix, OPP's `skeleton.zip` is rejected by ORF's
    format-preservation check (`.zip != .docx` for --format=docx).
    e2e_runner.py Tier 2 run caught this on xliff_cli path.
    """

    def test_zip_skeleton_with_format_docx_accepted(self, tmp_path):
        """`.zip` skeleton + --format=docx must be accepted.

        OPP produces `skeleton.zip` from DOCX inputs; this is the
        canonical skeleton source. ORF's skeleton loader handles it.
        The round 5 FIX-#8 guard was too strict; round 9 extends it.
        """
        fake_skeleton = tmp_path / "input.skeleton.zip"
        fake_skeleton.write_bytes(b"PK\x03\x04")  # zip magic
        fake_xlf = tmp_path / "translation.xlf"
        fake_xlf.write_text('<?xml version="1.0"?><xliff/>')
        output = tmp_path / "out.docx"

        result = _run_orf_cli(
            "apply-xliff", str(fake_skeleton),
            "--xliff", str(fake_xlf),
            "--output", str(output),
            "--format", "docx",
        )
        # The guard must NOT fire — expect rc=0 OR rc != 2 (rc=2 is the
        # BadParameter exit code). We accept any other outcome (real
        # ORF execution may fail for unrelated reasons in tests).
        assert "does not match" not in (result.stdout + result.stderr), (
            f".zip skeleton rejected by round 5 guard; round 9 should "
            f"have extended it:\n{result.stdout}\n{result.stderr}"
        )

    def test_pptx_skeleton_with_format_docx_still_rejected(self, tmp_path):
        """Cross-format (pptx + docx) must still be rejected."""
        fake_skeleton = tmp_path / "input.pptx"
        fake_skeleton.write_bytes(b"PK\x03\x04")
        fake_xlf = tmp_path / "translation.xlf"
        fake_xlf.write_text('<?xml version="1.0"?><xliff/>')
        output = tmp_path / "out.docx"

        result = _run_orf_cli(
            "apply-xliff", str(fake_skeleton),
            "--xliff", str(fake_xlf),
            "--output", str(output),
            "--format", "docx",
        )
        combined = result.stdout + result.stderr
        assert "does not match" in combined, (
            f"Cross-format pptx→docx must be rejected; got:\n{combined}"
        )

    def test_html_skeleton_with_format_docx_still_rejected(self, tmp_path):
        """html + docx cross-format: OPP doesn't produce .zip for HTML,
        so HTML skeleton with --format=docx must be rejected."""
        fake_skeleton = tmp_path / "input.html"
        fake_skeleton.write_text("<html></html>")
        fake_xlf = tmp_path / "translation.xlf"
        fake_xlf.write_text('<?xml version="1.0"?><xliff/>')
        output = tmp_path / "out.docx"

        result = _run_orf_cli(
            "apply-xliff", str(fake_skeleton),
            "--xliff", str(fake_xlf),
            "--output", str(output),
            "--format", "docx",
        )
        combined = result.stdout + result.stderr
        assert "does not match" in combined, (
            f"HTML→DOCX cross-format must be rejected:\n{combined}"
        )


class TestForceFlag:
    """e2e-test-suite#64: ``--force`` is accepted but inert.

    It used to downgrade both skeleton-format guards to a ``FORCE MODE``
    warning. It never converted anything: the backfill only rewrote the
    declared content type, so a DOCX skeleton came out as a DOCX-shaped
    ``cross.pptx`` / ``cross.epub`` -- rejected by python-pptx and by every
    EPUB reader while the CLI exited 0. The bypass is removed; the flag stays
    accepted so a caller gets the real cross-format error, not "no such option".
    """

    def test_force_does_not_bypass_extension_mismatch(self, tmp_path):
        """--force must fail exactly like the unforced run, with no artifact."""
        skeleton = tmp_path / "input.pptx"
        skeleton.write_bytes(b"PK\x03\x04")
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.docx"

        unforced = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "docx",
        )
        forced = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "docx",
            "--force",
        )

        for label, result in (("unforced", unforced), ("forced", forced)):
            combined = result.stdout + result.stderr
            assert result.returncode != 0, (
                f"{label}: cross-format must exit non-zero; got {result.returncode}\n"
                f"{combined}"
            )
            assert "does not match --format 'docx'" in combined, (
                f"{label}: expected the extension-mismatch rejection; got:\n{combined}"
            )
            assert "not implemented" in combined, (
                f"{label}: message must say cross-format is not implemented; "
                f"got:\n{combined}"
            )
            assert "orf apply-md" in combined, (
                f"{label}: message must point at the MD path; got:\n{combined}"
            )
            assert not output.exists(), (
                f"{label}: rejected cross-format request wrote {output}"
            )

    def test_force_does_not_bypass_zip_content_mismatch(self, tmp_path):
        """--force must not downgrade the content-level guard either."""
        skeleton = tmp_path / "input.skeleton.zip"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.pptx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "pptx",
            "--force",
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"--force must not bypass the content-level guard; "
            f"got rc={result.returncode}\n{combined}"
        )
        assert "Skeleton ZIP contains 'DOCX' format content" in combined, (
            f"expected the content-level rejection wording; got:\n{combined}"
        )
        assert "FORCE MODE" not in combined, (
            f"the FORCE MODE warning path must be gone; got:\n{combined}"
        )
        assert not output.exists(), f"--force wrote {output}"

    def test_force_is_harmless_on_matching_format(self, tmp_path):
        """The control: same-format backfill still succeeds with --force."""
        skeleton = tmp_path / "input.skeleton.zip"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf, source="Hello World", target="Hello World")
        output = tmp_path / "out.docx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "docx",
            "--force",
        )
        assert result.returncode == 0, (
            f"--force must stay accepted and harmless on a matching format; "
            f"got rc={result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert output.exists(), f"same-format backfill wrote no output\n{result.stderr}"
        assert "No such option" not in result.stdout + result.stderr

    def test_force_flag_help_says_it_does_not_enable_cross_format(self, tmp_path):
        """`--help` must not advertise a bypass ORF cannot honour."""
        result = _run_orf_cli("apply-xliff", "--help")
        assert result.returncode == 0, f"--help failed: {result.stderr}"
        # click hard-wraps option help, so compare on whitespace-collapsed text.
        help_text = " ".join(result.stdout.split())
        assert "--force" in help_text, "the --force option disappeared from --help"
        assert "no longer enables cross-format" in help_text, (
            f"--force help text still advertises the cross-format bypass:\n{help_text}"
        )
        assert "orf apply-md" in help_text, (
            f"--force help text must name the MD path:\n{help_text}"
        )


class TestXlsxSkeletonRegistration:
    """e2e-test-suite#92: ``xlsx`` joins the XLIFF backfill formats.

    OPP packages its XLSX skeleton as ``<stem>.skeleton.zip`` — the workbook
    plus an ``xliff_map.json`` sidecar — so registering the format means two
    things at once: a ``.zip`` skeleton must be accepted, and the content-level
    detection branch must stay active for it so a skeleton of another format
    renamed ``.zip`` is still rejected (the e2e-test-suite#64 guard).
    """

    def test_xlsx_is_a_zip_backfill_format(self):
        from orf.commands.apply_xliff import _FORMAT_EXT, _ZIP_FORMATS

        assert "xlsx" in _ZIP_FORMATS, (
            "xlsx is missing from _ZIP_FORMATS, so OPP's .skeleton.zip would be "
            "rejected on its extension"
        )
        assert _FORMAT_EXT["xlsx"] == ".xlsx"

    def test_xlsx_is_an_accepted_format_choice(self):
        result = _run_orf_cli("apply-xliff", "--help")
        assert result.returncode == 0, result.stderr
        import re

        match = re.search(r"--format \[([^\]]+)\]", result.stdout)
        assert match, result.stdout
        assert "xlsx" in match.group(1).split("|"), match.group(1)

    def test_docx_skeleton_with_format_xlsx_is_still_rejected(self, tmp_path):
        """The #64 guard must survive xlsx's arrival in _ZIP_FORMATS."""
        skeleton = tmp_path / "input.skeleton.zip"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.xlsx"

        for extra in ([], ["--force"]):
            result = _run_orf_cli(
                "apply-xliff", str(skeleton),
                "--xliff", str(xlf),
                "--output", str(output),
                "--format", "xlsx",
                *extra,
            )
            combined = result.stdout + result.stderr
            label = "--force" if extra else "plain"
            assert result.returncode != 0, (
                f"{label}: DOCX skeleton + --format xlsx must be rejected; "
                f"got rc={result.returncode}\n{combined}"
            )
            assert "Skeleton ZIP contains 'DOCX' format content" in combined, (
                f"{label}: expected the content-level rejection; got:\n{combined}"
            )
            assert "not implemented" in combined
            assert not output.exists(), f"{label} wrote {output}"

    def test_xlsx_skeleton_backfill_succeeds_end_to_end(self, tmp_path):
        """The positive control: a real XLSX skeleton + --format xlsx."""
        import json

        import openpyxl

        book = tmp_path / "book.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws["A1"] = "Name"
        ws["B1"] = 42
        wb.save(book)

        payload = {
            "version": 1,
            "sheets": {"Sheet1": [{"id": 1, "row": 1, "cells": [
                {"ref": "A1", "translatable": True},
                {"ref": "B1", "translatable": False},
            ]}]},
        }
        import zipfile

        skeleton = tmp_path / "book.skeleton.zip"
        with zipfile.ZipFile(skeleton, "w", zipfile.ZIP_DEFLATED) as zf:
            with zipfile.ZipFile(book) as source:
                for info in source.infolist():
                    zf.writestr(info, source.read(info))
            zf.writestr("xliff_map.json", json.dumps(payload))

        xlf = tmp_path / "translation.xlf"
        xlf.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">'
            '<file><body>'
            '<trans-unit id="1"><source>Name | 42</source>'
            '<target>名称 | 42</target></trans-unit>'
            "</body></file></xliff>",
            encoding="utf-8",
        )
        output = tmp_path / "out.xlsx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "xlsx",
        )
        assert result.returncode == 0, (
            f"xlsx backfill failed: rc={result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert output.exists(), result.stdout
        out = openpyxl.load_workbook(output)
        assert out["Sheet1"]["A1"].value == "名称"
        assert out["Sheet1"]["B1"].value == 42, "numeric cell was overwritten"


# ── Helpers for skeleton content-level validation tests ────────────────

_DOCX_SKELETON_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
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


def _create_docx_skeleton_zip(path: Path) -> None:
    """Create a minimal DOCX skeleton ZIP for testing."""
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _DOCX_SKELETON_DOCUMENT)
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="xml" ContentType="application/xml"/>\n'
            '</Types>',
        )


def _create_pptx_skeleton_zip(path: Path) -> None:
    """Create a minimal PPTX skeleton ZIP for testing."""
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )


def _create_xliff(path: Path, source: str = "Hello World", target: str = "Hello World") -> None:
    """Create a minimal XLIFF file for testing."""
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        f'  <file source-language="en" target-language="zh" datatype="plaintext">\n'
        f'    <body>\n'
        f'      <trans-unit id="1">\n'
        f'        <source>{source}</source>\n'
        f'        <target>{target}</target>\n'
        f'      </trans-unit>\n'
        f'    </body>\n'
        f'  </file>\n'
        f'</xliff>'
    )


class TestSkeletonContentValidation:
    """Content-level validation for ZIP skeletons.

    Peek inside .skeleton.zip files via FormatDetector.detect_from_skeleton()
    to verify the actual format matches --format, not just the file extension.
    """

    def test_docx_skeleton_rejects_pptx_format_without_force(self, tmp_path):
        """DOCX skeleton .zip with --format pptx must fail with skeleton error."""
        skeleton = tmp_path / "input.skeleton.zip"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.pptx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "pptx",
        )
        assert result.returncode != 0, (
            f"Expected non-zero exit on format mismatch; got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "Skeleton" in combined, (
            f"Error message should contain 'Skeleton'; got:\n{combined}"
        )

    def test_docx_skeleton_accepts_docx_format(self, tmp_path):
        """DOCX skeleton .zip with --format docx must succeed."""
        skeleton = tmp_path / "input.skeleton.zip"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf, source="Hello World", target="Hello World")
        output = tmp_path / "out.docx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "docx",
        )
        assert result.returncode == 0, (
            f"Expected exit code 0 for matching format; got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_docx_skeleton_rejects_pptx_format_with_force(self, tmp_path):
        """DOCX skeleton .zip with --format pptx --force must still be rejected.

        e2e-test-suite#64: this used to warn and exit 0, emitting the DOCX zip
        under a .pptx extension.
        """
        skeleton = tmp_path / "input.skeleton.zip"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf, source="Hello World", target="Hello World")
        output = tmp_path / "out.pptx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "pptx",
            "--force",
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"--force must not bypass the content-level guard; "
            f"got rc={result.returncode}\n{combined}"
        )
        assert "Invalid value" in combined, (
            f"expected the click BadParameter rejection; got:\n{combined}"
        )
        assert not output.exists(), f"--force wrote {output}\n{combined}"

    def test_ppt_skeleton_rejects_docx_format(self, tmp_path):
        """PPTX skeleton .zip with --format docx must fail with skeleton error."""
        skeleton = tmp_path / "input.skeleton.zip"
        _create_pptx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.docx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "docx",
        )
        assert result.returncode != 0, (
            f"Expected non-zero exit on format mismatch; got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "Skeleton" in combined, (
            f"Error message should contain 'Skeleton'; got:\n{combined}"
        )


class TestForceRejectedByFinalConverterGate:
    """e2e-test-suite#64: the final ``converter.validate_input`` gate no
    longer honours ``--force``.

    e2e-test-suite#86 had relaxed this gate to
    ``if not force and not converter.validate_input(...)`` so a raw
    cross-format skeleton could proceed after the FORCE MODE warnings. With
    the bypass removed the exemption had to go too, otherwise ``--force``
    remained a working bypass for an extensionless skeleton -- the one shape
    that slips past both CLI-level checks.
    """

    def test_force_raw_docx_input_pptx_rejected_by_final_gate(self, tmp_path):
        """Raw .docx input + --format pptx --force must not reach the converter."""
        skeleton = tmp_path / "input.docx"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf, source="Hello World", target="Hello World")
        output = tmp_path / "out.pptx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "pptx",
            "--force",
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"--force must not reach the converter for a cross-format request; "
            f"rc={result.returncode}\n{combined}"
        )
        assert "does not match --format 'pptx'" in combined, (
            f"expected the extension-check rejection; got:\n{combined}"
        )
        assert not output.exists(), f"--force wrote {output}\n{combined}"

    def test_force_extensionless_skeleton_rejected_by_final_gate(self, tmp_path):
        """Extensionless input skips both CLI-level checks — the gate must not."""
        skeleton = tmp_path / "skeleton"  # no suffix → earlier checks skip
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.pptx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "pptx",
            "--force",
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"--force must not bypass the final gate; rc={result.returncode}\n{combined}"
        )
        assert "is not valid for pptx format" in combined, (
            f"expected the final validate_input gate to fire; got:\n{combined}"
        )
        assert not output.exists(), f"--force wrote {output}\n{combined}"

    def test_no_force_raw_docx_pptx_still_rejected(self, tmp_path):
        """Without --force, raw .docx + --format pptx must still be rejected.

        Guards the fix against over-weakening: the non-force rejection
        (extension-check BadParameter) is preserved.
        """
        skeleton = tmp_path / "input.docx"
        _create_docx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.pptx"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "pptx",
        )
        assert result.returncode != 0, (
            f"Expected non-zero exit without --force; got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "does not match" in combined, (
            f"Cross-format .docx→pptx without --force must be rejected by the "
            f"extension check; got:\n{combined}"
        )


# e2e-test-suite#86 relaxed the last skeleton gate to
# `if not force and not converter.validate_input(...)`. The non-force branch
# must keep rejecting for every supported target format; `json` returns before
# the gate and is intentionally excluded.

_GATE_FORMATS = ["docx", "pptx", "epub", "html", "odt", "pdf", "xlsx"]

# docx already owns .docx, so its foreign extension is .pptx; .docx is foreign
# to every other format.
_FOREIGN_SKELETON_EXT = {
    "docx": ".pptx",
    "pptx": ".docx",
    "epub": ".docx",
    "html": ".docx",
    "odt": ".docx",
    "pdf": ".docx",
    "xlsx": ".docx",
}


class TestNonForceTypeRejectionAcrossFormats:
    """Non-force extension (type) rejection for every target format."""

    @pytest.mark.parametrize("fmt", _GATE_FORMATS)
    def test_foreign_extension_rejected_without_force(self, tmp_path, fmt):
        skeleton = tmp_path / f"input{_FOREIGN_SKELETON_EXT[fmt]}"
        skeleton.write_bytes(b"PK\x03\x04")
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / f"out.{fmt}"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", fmt,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"{fmt}: foreign skeleton must be rejected without --force; got "
            f"rc={result.returncode}\n{combined}"
        )
        assert "does not match" in combined, (
            f"{fmt}: expected extension-mismatch rejection; got:\n{combined}"
        )


class TestFinalGateNonForceRejectionAcrossFormats:
    """The exact guard changed by e2e-test-suite#86 must still reject
    non-force inputs.

    An extensionless skeleton slips past both CLI-level checks — the
    extension check short-circuits on an empty suffix and the ZIP check
    requires a ``.zip`` suffix — so the final ``validate_input`` gate is
    the only thing that can reject it. These tests therefore exercise the
    changed guard's non-force branch directly, for every format.
    """

    @pytest.mark.parametrize("fmt", _GATE_FORMATS)
    def test_extensionless_skeleton_rejected_by_final_gate(self, tmp_path, fmt):
        skeleton = tmp_path / "skeleton"  # no suffix → earlier checks skip
        skeleton.write_bytes(b"PK\x03\x04")
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / f"out.{fmt}"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", fmt,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"{fmt}: extensionless skeleton must be rejected by the final "
            f"gate without --force; got rc={result.returncode}\n{combined}"
        )
        assert f"is not valid for {fmt} format" in combined, (
            f"{fmt}: expected the final validate_input gate to fire; "
            f"got:\n{combined}"
        )


class TestNonForceContentRejectionAcrossZipFormats:
    """Non-force ZIP content-level rejection for the EPUB target.

    The docx↔pptx directions are already pinned by
    ``TestSkeletonContentValidation``; these complete the trio of
    ZIP-backed formats so content-level rejection is proven for each.
    """

    @pytest.mark.parametrize("builder", ["docx", "pptx"])
    def test_zip_content_mismatch_rejected_without_force(self, tmp_path, builder):
        skeleton = tmp_path / "input.skeleton.zip"
        if builder == "docx":
            _create_docx_skeleton_zip(skeleton)
        else:
            _create_pptx_skeleton_zip(skeleton)
        xlf = tmp_path / "translation.xlf"
        _create_xliff(xlf)
        output = tmp_path / "out.epub"

        result = _run_orf_cli(
            "apply-xliff", str(skeleton),
            "--xliff", str(xlf),
            "--output", str(output),
            "--format", "epub",
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            f"{builder}-content zip with --format epub must be rejected "
            f"without --force; got rc={result.returncode}\n{combined}"
        )
        assert "Skeleton ZIP contains" in combined, (
            f"{builder}-content zip vs epub: expected content-level "
            f"rejection; got:\n{combined}"
        )
