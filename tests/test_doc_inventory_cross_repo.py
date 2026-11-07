"""Cross-repo reference resolution in scripts/doc_inventory.py (e2e-test-suite#253).

Locks the two branches the shared checker distinguishes:

- a path with no ``<repo>:`` prefix is **this** repo's file and must exist
  (a missing one is a failure, never a silent skip);
- a prefixed path is only verified when that repo's checkout is present;
  without a checkout the check reports it as skipped so the "passed" line
  cannot claim what it did not verify.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "doc_inventory.py"
OWN_REPO = "orf"
OWN_MARKER = "src/orf"


def _load():
    spec = importlib.util.spec_from_file_location("doc_inventory_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def checker(tmp_path, monkeypatch):
    monkeypatch.delenv("DOC_CROSS_ROOT", raising=False)
    module = _load()
    root = tmp_path / "repo"
    (root / OWN_MARKER).mkdir(parents=True)
    module.ROOT = root
    module.REFERENTIAL_SCOPE = ("AGENTS.md",)
    return module, root


def _doc(root: Path, body: str) -> None:
    (root / "AGENTS.md").write_text(body, encoding="utf-8")


def _run(module):
    failures: list[str] = []
    return failures, module._check_referential_paths(failures)


def test_local_path_must_exist(checker):
    module, root = checker
    _doc(root, "See `src/orf/missing.py`.\n")
    failures, skipped = _run(module)
    assert skipped == {}, "own-repo paths are never counted as skipped"
    assert failures, "a missing own-repo path must fail the check"
    assert "missing.py" in failures[0]


def test_local_path_resolves(checker):
    module, root = checker
    target = root / "src/orf" / "here.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    _doc(root, "See `src/orf/here.py:12`.\n")
    assert _run(module) == ([], {})


def test_cross_repo_without_checkout_is_skipped_not_passed(checker):
    module, root = checker
    _doc(root, "See `suite:docs/dev/validation-director-loop.md`.\n")
    failures, skipped = _run(module)
    assert failures == []
    assert skipped == {"suite": 1}


def test_cross_repo_missing_file_with_checkout_fails(checker):
    module, root = checker
    (root.parent / "e2e-test-suite" / "omni_mcp").mkdir(parents=True)
    _doc(root, "See `suite:docs/dev/does-not-exist.md`.\n")
    failures, skipped = _run(module)
    assert skipped == {}
    assert failures and "does-not-exist.md" in failures[0]


def test_cross_repo_present_file_passes(checker):
    module, root = checker
    suite = root.parent / "e2e-test-suite"
    (suite / "omni_mcp").mkdir(parents=True)
    (suite / "docs" / "dev").mkdir(parents=True)
    (suite / "docs" / "dev" / "validation-director-loop.md").write_text("", encoding="utf-8")
    _doc(root, "See `suite:docs/dev/validation-director-loop.md`.\n")
    assert _run(module) == ([], {})


def test_doc_cross_root_locates_checkout(checker, tmp_path, monkeypatch):
    module, root = checker
    shared = tmp_path / "shared"
    (shared / "omni_mcp").mkdir(parents=True)
    (shared / "docs").mkdir()
    (shared / "docs" / "shared.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("DOC_CROSS_ROOT", str(shared))
    _doc(root, "See `suite:docs/shared.md`.\n")
    assert _run(module) == ([], {})


def test_own_repo_prefix_resolves_locally(checker):
    module, root = checker
    target = root / "src/orf" / "here.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    _doc(root, "See `orf:src/orf/here.py`.\n")
    assert _run(module) == ([], {})


def test_own_repo_prefix_missing_file_still_fails(checker, tmp_path, monkeypatch):
    """A prefixed path naming this repo is verified here, never skipped."""
    module, root = checker
    monkeypatch.setenv("DOC_CROSS_ROOT", str(tmp_path / "nowhere"))
    _doc(root, "See `orf:src/orf/here.py`.\n")
    failures, skipped = _run(module)
    assert skipped == {}
    assert failures and "here.py" in failures[0]
