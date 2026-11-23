"""Issue #429: fake-pandoc runs must never read or write the shared ORF cache.

``OMNI_TEST_FAKE_PANDOC=1`` swaps pandoc for a fixed-template stub whose
output carries no relation to the input. Caching that output would poison
real runs hitting the same content key. These tests pin the isolation
behavior (cache read skip, cache write skip) plus the real-mode baseline
(caching still works, atomic write leaves no ``.tmp`` residue).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orf.cli import _check_cache, _write_cache

FAKE_KEY = "deadbeef" * 8  # 64-char sha256-shaped cache key


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point OMNI_CACHE_DIR at a temp dir (never the real ~/.omni_cache)."""
    root = tmp_path / "omni_cache"
    root.mkdir()
    monkeypatch.setenv("OMNI_CACHE_DIR", str(root))
    return root


def _cached_file(cache_dir: Path, ext: str) -> Path:
    return cache_dir / "orf" / f"{FAKE_KEY}{ext}"


def test_fake_pandoc_disables_cache_read(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under OMNI_TEST_FAKE_PANDOC=1 a pre-seeded cache entry is ignored."""
    monkeypatch.setenv("OMNI_TEST_FAKE_PANDOC", "1")
    cached = _cached_file(cache_dir, ".docx")
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"poisoned fake-pandoc output")

    output_path = tmp_path / "out.docx"
    hit = _check_cache(FAKE_KEY, output_path, ".docx")

    assert hit is False
    assert not output_path.exists()


def test_fake_pandoc_disables_cache_write(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under OMNI_TEST_FAKE_PANDOC=1 nothing is written into the cache."""
    monkeypatch.setenv("OMNI_TEST_FAKE_PANDOC", "1")
    output_path = tmp_path / "out.docx"
    output_path.write_bytes(b"fake template content")

    _write_cache(FAKE_KEY, output_path, ".docx")

    orf_dir = cache_dir / "orf"
    assert not orf_dir.exists() or not any(orf_dir.iterdir())


def test_real_mode_still_caches(cache_dir: Path, tmp_path: Path) -> None:
    """Without the fake-pandoc env, cache write stores the file for reuse."""
    output_path = tmp_path / "out.docx"
    output_path.write_bytes(b"real converted content")

    _write_cache(FAKE_KEY, output_path, ".docx")
    cached = _cached_file(cache_dir, ".docx")
    assert cached.exists()
    assert cached.read_bytes() == b"real converted content"

    # And the written entry is a hit on the next run.
    output_path2 = tmp_path / "out2.docx"
    assert _check_cache(FAKE_KEY, output_path2, ".docx") is True
    assert output_path2.read_bytes() == b"real converted content"


def test_cache_write_is_atomic(cache_dir: Path, tmp_path: Path) -> None:
    """Real-mode cache write leaves no .tmp residue behind."""
    output_path = tmp_path / "out.docx"
    output_path.write_bytes(b"real converted content")

    _write_cache(FAKE_KEY, output_path, ".docx")

    orf_dir = _cached_file(cache_dir, ".docx").parent
    residue = [p.name for p in orf_dir.iterdir() if p.name.endswith(".tmp")]
    assert residue == []
    assert _cached_file(cache_dir, ".docx").exists()
