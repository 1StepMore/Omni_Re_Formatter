"""A6 cache tests for ORF CLI (PR11 of slim-pipeline-hardening plan).

Tests the ORF CLI's content-addressed cache at ~/.omni_cache/orf/<sha256>.<ext>.
The cache root is overridden in tests via OMNI_CACHE_DIR env var (set by
``fake_cache_dir`` fixture) so tests do not touch the real user cache.

TDD discipline: these tests are written FIRST (before the production code).
They exercise the cache plumbing without depending on the real
``MD2DOCXConverter.convert()`` (we mock it to a deterministic counter +
known-output writer so we can assert "cache hit means no convert work" and
"cache miss calls the converter").

Cache key for ORF ``apply-md`` is:
    sha256(input_md_bytes + target_format + manifest_bytes_if_any
           + template_bytes_if_any + images_json_bytes_if_any)

The cache stores the post-conversion output (e.g., a ``.docx`` file).
On a hit, the cached file is copied to the user-supplied ``--output`` path
and the converter is not invoked. On a miss, the converter runs normally
and the produced output is copied into the cache for next time.

The four required tests are:
  1. test_orf_cache_hit_returns_cached_output
  2. test_orf_cache_miss_on_input_change
  3. test_orf_cache_invalidation_on_config_change   (manifest change for apply-md)
  4. test_orf_cache_directory_created_with_correct_permissions

The tests focus on ``apply-md`` because that command has the full
"input + config (manifest) + output" trio. ``apply-xliff`` is wired in
the same pattern (and the helpers are shared), so a single command
exercised here is sufficient to prove the convention is uniform.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from orf.cli import main

runner = CliRunner()


# ========== Fixtures ==========


@pytest.fixture
def fake_cache_dir(tmp_path, monkeypatch):
    """Override OMNI_CACHE_DIR to a tmp path for testing.

    The production cache helpers must read this env var at call-time
    (not at module import) so the fixture sets it before each test and
    the ORF module resolves the cache root to ``tmp_path / "omni_cache"``.

    Also clears ``OMNI_TEST_FAKE_PANDOC``: these tests exercise the cache
    through the CLI entry point, and since issue #429 the fake-pandoc seam
    deliberately disables caching (a stub's output must never be cached).
    CI exports that env var job-wide, which would otherwise disable the very
    behavior under test. These cells mock the converter, so no real pandoc
    runs either way.
    """
    cache_root = tmp_path / "omni_cache"
    monkeypatch.setenv("OMNI_CACHE_DIR", str(cache_root))
    monkeypatch.delenv("OMNI_TEST_FAKE_PANDOC", raising=False)
    yield cache_root


@pytest.fixture
def sample_md(tmp_path):
    """Create a minimal MD input file for ORF apply-md (with manifest sidecar)."""
    md = tmp_path / "input.md"
    md.write_text(
        "---\nsource_lang: en\ntarget_lang: zh\n---\n\n# Hello\n\nWorld.\n",
        encoding="utf-8",
    )
    # Manifest sidecar: find_manifest looks for <stem>_manifest.json in the same dir.
    manifest = tmp_path / "input_manifest.json"
    manifest.write_text(
        '{"manifest_version": "1.0", "source": {"format": "DOCX", "file_path": "x.docx"}}',
        encoding="utf-8",
    )
    return md


def _make_fake_md_convert_side_effect(call_counter, output_bytes: bytes):
    """Side effect for the MD converter that writes a known output file and counts calls.

    Real ``MD2DOCXConverter.convert()`` does real pandoc work. For cache tests we
    only care that the ORF CLI calls the converter on a cache MISS and does NOT
    call it on a cache HIT. This fake gives us that signal without invoking the
    real pandoc/format-detection code paths.
    """
    def side_effect(input_path, output_path, *args, **kwargs):
        call_counter["n"] += 1
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(output_bytes)
        result = MagicMock()
        result.success = True
        result.output_path = out
        result.errors = []
        result.warnings = []
        result.metadata = {}
        return result
    return side_effect


# ========== Tests ==========


def test_orf_cache_hit_returns_cached_output(fake_cache_dir, sample_md, tmp_path):
    """Run ``apply-md`` twice on same input; second run is a cache hit.

    The converter (``MD2DOCXConverter.convert``) is called once on the cache
    MISS and NOT called on the cache HIT. Outputs across runs are byte
    identical.
    """
    counter = {"n": 0}
    fake_output = b"FAKE ORF DOCX OUTPUT v1"

    # First run: cache miss → converter is called
    out1 = tmp_path / "out1.docx"
    with patch("orf.cli.MD2DOCXConverter") as mock_class:
        mock_instance = MagicMock()
        mock_instance.convert.side_effect = _make_fake_md_convert_side_effect(
            counter, fake_output,
        )
        mock_class.return_value = mock_instance
        rc1 = runner.invoke(
            main,
            [
                "apply-md", str(sample_md),
                "--target-format", "docx",
                "-o", str(out1),
            ],
        )
    assert rc1.exit_code == 0, (
        f"first run failed (rc={rc1.exit_code}), output={rc1.output!r}, "
        f"exception={rc1.exception!r}"
    )
    assert counter["n"] == 1, f"expected 1 converter call, got {counter['n']}"
    assert out1.exists(), f"expected {out1} to exist"
    assert out1.read_bytes() == fake_output

    # Second run: cache hit → converter MUST NOT be called again
    counter["n"] = 0
    out2 = tmp_path / "out2.docx"
    with patch("orf.cli.MD2DOCXConverter") as mock_class:
        mock_instance = MagicMock()
        mock_instance.convert.side_effect = _make_fake_md_convert_side_effect(
            counter, fake_output,
        )
        mock_class.return_value = mock_instance
        rc2 = runner.invoke(
            main,
            [
                "apply-md", str(sample_md),
                "--target-format", "docx",
                "-o", str(out2),
            ],
        )
    assert rc2.exit_code == 0, (
        f"second run failed (rc={rc2.exit_code}), output={rc2.output!r}, "
        f"exception={rc2.exception!r}"
    )
    assert counter["n"] == 0, (
        f"cache hit expected: converter should not be called, "
        f"got {counter['n']} calls"
    )
    assert out2.exists(), f"expected {out2} to exist after cache hit"
    # Identical output across runs (cache hit must reproduce the cached bytes)
    assert out2.read_bytes() == out1.read_bytes()


def test_orf_cache_miss_on_input_change(fake_cache_dir, tmp_path):
    """Modify input MD; assert cache miss (converter called twice across runs)."""
    counter = {"n": 0}
    fake_output = b"FAKE ORF DOCX OUTPUT"
    fake_side_effect = _make_fake_md_convert_side_effect(counter, fake_output)

    # First run with input1 (with its manifest sidecar)
    input1 = tmp_path / "in1.md"
    input1.write_text("# Original\n\ncontent one.\n", encoding="utf-8")
    (tmp_path / "in1_manifest.json").write_text(
        '{"manifest_version": "1.0", "source": {"format": "DOCX"}}',
        encoding="utf-8",
    )
    out1 = tmp_path / "out1.docx"
    with patch("orf.cli.MD2DOCXConverter") as mock_class:
        mock_instance = MagicMock()
        mock_instance.convert.side_effect = fake_side_effect
        mock_class.return_value = mock_instance
        runner.invoke(
            main,
            ["apply-md", str(input1), "--target-format", "docx", "-o", str(out1)],
        )
    assert counter["n"] == 1, f"first run expected 1 call, got {counter['n']}"

    # Second run with a different input (same args, different bytes → different cache key)
    input2 = tmp_path / "in2.md"
    input2.write_text("# Modified\n\ndifferent content.\n", encoding="utf-8")
    (tmp_path / "in2_manifest.json").write_text(
        '{"manifest_version": "1.0", "source": {"format": "DOCX"}}',
        encoding="utf-8",
    )
    out2 = tmp_path / "out2.docx"
    with patch("orf.cli.MD2DOCXConverter") as mock_class:
        mock_instance = MagicMock()
        mock_instance.convert.side_effect = fake_side_effect
        mock_class.return_value = mock_instance
        runner.invoke(
            main,
            ["apply-md", str(input2), "--target-format", "docx", "-o", str(out2)],
        )
    assert counter["n"] == 2, (
        f"cache miss expected on input change: expected 2 total calls, "
        f"got {counter['n']}"
    )


def test_orf_cache_invalidation_on_config_change(fake_cache_dir, sample_md, tmp_path):
    """Change manifest content; assert cache miss (converter called twice across runs).

    For ``apply-md`` the relevant "config" is the OPP ``<stem>_manifest.json``
    sidecar (it influences the converter via the parsed ``Manifest`` object).
    The cache key includes the raw manifest bytes, so editing the manifest
    invalidates the cache.
    """
    counter = {"n": 0}
    fake_output = b"FAKE ORF DOCX OUTPUT"
    fake_side_effect = _make_fake_md_convert_side_effect(counter, fake_output)

    # First run with manifest1 (the one created by the sample_md fixture)
    out1 = tmp_path / "out1.docx"
    with patch("orf.cli.MD2DOCXConverter") as mock_class:
        mock_instance = MagicMock()
        mock_instance.convert.side_effect = fake_side_effect
        mock_class.return_value = mock_instance
        rc1 = runner.invoke(
            main,
            [
                "apply-md", str(sample_md),
                "--target-format", "docx",
                "-o", str(out1),
            ],
        )
    assert rc1.exit_code == 0, (
        f"first run failed (rc={rc1.exit_code}), output={rc1.output!r}, "
        f"exception={rc1.exception!r}"
    )
    assert counter["n"] == 1, f"first run expected 1 call, got {counter['n']}"

    # Modify the manifest: same path, different content → different cache key
    manifest = tmp_path / "input_manifest.json"
    manifest.write_text(
        '{"manifest_version": "1.0", "source": {"format": "DOCX", "note": "changed"}}',
        encoding="utf-8",
    )
    out2 = tmp_path / "out2.docx"
    with patch("orf.cli.MD2DOCXConverter") as mock_class:
        mock_instance = MagicMock()
        mock_instance.convert.side_effect = fake_side_effect
        mock_class.return_value = mock_instance
        rc2 = runner.invoke(
            main,
            [
                "apply-md", str(sample_md),
                "--target-format", "docx",
                "-o", str(out2),
            ],
        )
    assert rc2.exit_code == 0, (
        f"second run failed (rc={rc2.exit_code}), output={rc2.output!r}, "
        f"exception={rc2.exception!r}"
    )
    assert counter["n"] == 2, (
        f"cache miss expected on manifest change: expected 2 total calls, "
        f"got {counter['n']}"
    )


def test_orf_cache_directory_created_with_correct_permissions(
    fake_cache_dir, sample_md, tmp_path,
):
    """Cache dir exists and is mode 0o700 (protects any sensitive cached content)."""
    counter = {"n": 0}
    fake_output = b"FAKE ORF DOCX OUTPUT"
    out1 = tmp_path / "out1.docx"
    with patch("orf.cli.MD2DOCXConverter") as mock_class:
        mock_instance = MagicMock()
        mock_instance.convert.side_effect = _make_fake_md_convert_side_effect(
            counter, fake_output,
        )
        mock_class.return_value = mock_instance
        rc = runner.invoke(
            main,
            [
                "apply-md", str(sample_md),
                "--target-format", "docx",
                "-o", str(out1),
            ],
        )
    assert rc.exit_code == 0, (
        f"run failed (rc={rc.exit_code}), output={rc.output!r}, "
        f"exception={rc.exception!r}"
    )

    cache_root = Path(os.environ["OMNI_CACHE_DIR"])
    orf_cache = cache_root / "orf"
    assert orf_cache.exists(), f"expected cache dir at {orf_cache}"
    assert orf_cache.is_dir()
    if sys.platform == "win32":
        # POSIX mode bits do not exist on Windows: NTFS synthesizes
        # st_mode as 0o777 for every directory (measured: mkdir(mode=0o700)
        # reports 0o777), so the 0o700 contract cannot be observed there.
        # Assert what the platform does express — the cache dir is usable
        # (readable/writable) by the owning process.
        assert os.access(orf_cache, os.R_OK), f"cache dir not readable: {orf_cache}"
        assert os.access(orf_cache, os.W_OK), f"cache dir not writable: {orf_cache}"
    else:
        mode = orf_cache.stat().st_mode & 0o777
        assert mode == 0o700, f"expected mode 0o700, got {oct(mode)}"
