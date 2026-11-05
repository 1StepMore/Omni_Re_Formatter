"""P1-A regression: EPUB backfill must be byte-reproducible across runs.

The EPUB channel (``orf/channels/md2epub.py``) handed pandoc the wall
clock: every run embedded a fresh random UUID (``dc:identifier`` /
``dtb:uid``), a current ``dcterms:modified`` / ``dc:date`` and per-member
zip timestamps, so two runs of the same input produced different bytes —
the artifact-fingerprint MD5 comparisons (``scripts/equivalence_checker.py``)
failed for every epub-producing cell.

Contract locked here (same framing as ``test_apply_md_idempotent.py``):

* two runs of the SAME input into DIFFERENT output directories produce
  byte-identical EPUBs — cross-directory on purpose, because apply-md
  stages every conversion through a fresh temp sibling whose mtime changes
  per run, so an mtime-derived SOURCE_DATE_EPOCH pin (the DOCX channel's
  approach) would not suffice; the EPUB pin derives from the input bytes;
* the ``dc:identifier`` is the content-derived uuid5, not a random UUID4.

RED before the fix, GREEN once ``md2epub.convert`` pins the env and the
identifier.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from orf.cli import main

pytestmark = pytest.mark.skipif(
    shutil.which("pandoc") is None,
    reason="apply-md EPUB conversion requires the pandoc binary",
)

_VALID_MD = (
    "---\nsource_lang: en\ntarget_lang: zh\n---\n\n"
    "# User Manual\n\nWelcome to the guide.\n\n## Section\n\nSome body text.\n"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke_apply_md_epub(runner: CliRunner, md: Path, output: Path) -> Any:
    return runner.invoke(
        main,
        [
            "apply-md",
            str(md),
            "--target-format",
            "epub",
            "-o",
            str(output),
            "--no-cache",
        ],
    )


def test_two_runs_in_different_dirs_produce_identical_epub(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same input, fresh staging dirs -> byte-identical EPUB bytes."""
    monkeypatch.setenv("OMNI_CACHE_DIR", str(tmp_path / "cache"))
    md = tmp_path / "doc.md"
    md.write_text(_VALID_MD, encoding="utf-8")

    out_a = tmp_path / "run_a"
    out_b = tmp_path / "run_b"
    out_a.mkdir()
    out_b.mkdir()
    epub_a = out_a / "result.epub"
    epub_b = out_b / "result.epub"

    first = _invoke_apply_md_epub(runner, md, epub_a)
    assert first.exit_code == 0, first.output
    second = _invoke_apply_md_epub(runner, md, epub_b)
    assert second.exit_code == 0, second.output

    assert epub_a.exists() and epub_b.exists()
    assert _sha256(epub_a) == _sha256(epub_b), (
        "two runs of the same input must produce byte-identical EPUBs "
        "(random UUID / wall-clock stamps leaked into the container)"
    )


def test_identifier_is_content_derived_uuid5(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dc:identifier must be the content-derived uuid5, stable per input."""
    monkeypatch.setenv("OMNI_CACHE_DIR", str(tmp_path / "cache"))
    md = tmp_path / "doc.md"
    md.write_text(_VALID_MD, encoding="utf-8")
    epub_path = tmp_path / "result.epub"

    assert _invoke_apply_md_epub(runner, md, epub_path).exit_code == 0
    with zipfile.ZipFile(epub_path) as z:
        # Locate the OPF the spec way — via META-INF/container.xml — instead
        # of guessing its path/name (pandoc versions moved it around; a
        # runner with a different pandoc wrote no *.opf-suffixed entry).
        container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', container)
        assert m, f"container.xml names no rootfile: {container[:200]}"
        opf_text = z.read(m.group(1)).decode("utf-8", "ignore")
    identifiers = re.findall(
        r"<dc:identifier[^>]*>([^<]+)</dc:identifier>", opf_text
    )
    assert identifiers, f"no dc:identifier in {m.group(1)}"
    digest = hashlib.sha256(md.read_bytes()).hexdigest()
    want = str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:orf:epub:{digest}"))
    assert any(want in i for i in identifiers), (
        f"dc:identifier is not the content-derived uuid5: {identifiers} (want {want})"
    )
