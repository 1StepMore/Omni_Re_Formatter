"""T-01 regression: ``--json`` failure branches must exit non-zero.

Plan gap T-01 (``.omo/plans/agent-oriented-gap-register.md`` §4.1,
"ORF ``--json`` exits 0 on conversion failure"): ORF's ``--json`` failure
branches emitted a ``success: false`` JSON object on stdout but returned
normally, so the process exited 0. An agent that branches on the exit code
(``#exit-codes``) saw a misleading success while the machine-readable body
said failure — the two signals disagreed.

Contract locked here, per ``scenarios/STANDARDS.md``:

* ``#exit-codes`` — a failing command exits non-zero.
* ``#json-parseable`` — the failure body on stdout is exactly one parseable
  JSON object carrying ``success: false``.

The ``*_characterization`` tests pin the JSON error contract that already
held before the fix (stdout parses, ``success`` is false). The
``test_*_exits_nonzero`` tests are the failing-first assertions: they are
RED on the unfixed code (exit 0) and GREEN once every ``--json`` failure
branch exits non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from orf.cli import main
from orf.converters.base import ConversionResult, ErrorDetail


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _apply_md_failure(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    """Invoke a real, deterministic apply-md failure with --json.

    Issue #102: the output parent is now auto-created (mkdir -p), so a
    missing directory no longer fails the conversion — the failure is
    injected by stubbing the HTML converter instead (the same mechanism
    ``test_apply_xliff_generic_failure_exits_nonzero`` below uses). The
    asserted ``--json`` failure contract is unchanged: stdout carries
    ``success: false`` and the process exits non-zero.
    """
    md = tmp_path / "in.md"
    md.write_text("# Hello\n\nworld\n", encoding="utf-8")
    output = tmp_path / "missing_dir" / "out.html"

    def _fake_convert(self, input_path, output_path, options=None):
        return ConversionResult(
            output_path=Path(output_path),
            success=False,
            errors=[
                ErrorDetail(
                    code="CONVERSION_ERROR",
                    message="synthetic conversion failure",
                )
            ],
        )

    from orf.cli import MD2HTMLConverter

    monkeypatch.setattr(MD2HTMLConverter, "convert", _fake_convert)
    return runner.invoke(
        main,
        [
            "apply-md",
            str(md),
            "--target-format",
            "html",
            "-o",
            str(output),
            "--json",
        ],
    )


def _stdout_json(result: Any) -> dict[str, Any]:
    """Parse stdout as exactly one JSON object (raises on trailing data)."""
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict), f"stdout JSON is not an object: {type(parsed)}"
    return parsed


# ---------------------------------------------------------------------------
# apply-md
# ---------------------------------------------------------------------------


def test_apply_md_json_failure_characterization(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline (pre-fix) JSON contract: stdout parses, success=false.

    Passes on the unfixed code and stays green after the fix — the failure
    branch must keep emitting the machine-readable error object.
    """
    result = _apply_md_failure(runner, tmp_path, monkeypatch)

    obj = _stdout_json(result)
    assert obj["success"] is False
    assert obj["errors"], "failure JSON must carry at least one error entry"


def test_apply_md_json_failure_exits_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed contract: conversion failure + --json exits non-zero.

    RED on the unfixed code (exit 0), GREEN after T-01.
    """
    result = _apply_md_failure(runner, tmp_path, monkeypatch)

    assert result.exit_code != 0, (
        "apply-md --json on a failed conversion must exit non-zero "
        f"(got {result.exit_code}); stdout={result.stdout!r}"
    )
    obj = _stdout_json(result)
    assert obj["success"] is False


def test_apply_md_json_failure_stdout_is_single_json_object(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure body is exactly one JSON object — no trailers on stdout."""
    result = _apply_md_failure(runner, tmp_path, monkeypatch)

    # json.loads on the whole stdout rejects any trailing non-whitespace.
    obj = _stdout_json(result)
    assert obj["success"] is False
    assert "Traceback (most recent call last)" not in result.stderr


# ---------------------------------------------------------------------------
# apply-xliff — JSON channel failure branch
# ---------------------------------------------------------------------------


def test_apply_xliff_json_channel_failure_exits_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply-xliff --format json failure + --json must exit non-zero.

    RED on the unfixed code (exit 0), GREEN after T-01.
    """
    skeleton = tmp_path / "skeleton.json"
    skeleton.write_text("{}", encoding="utf-8")
    xliff = tmp_path / "translated.xlf"
    xliff.write_text("<xliff/>", encoding="utf-8")
    output = tmp_path / "out.json"

    def _fake_apply_xliff_to_json(**_kwargs: Any) -> dict[str, Any]:
        return {"success": False, "error": "synthetic backfill failure"}

    monkeypatch.setattr(
        "orf.channels.xliff2json.apply_xliff_to_json",
        _fake_apply_xliff_to_json,
    )

    result = runner.invoke(
        main,
        [
            "apply-xliff",
            str(skeleton),
            "--xliff",
            str(xliff),
            "--output",
            str(output),
            "--format",
            "json",
            "--json",
        ],
    )

    assert result.exit_code != 0, (
        "apply-xliff --format json --json on failure must exit non-zero "
        f"(got {result.exit_code}); stdout={result.stdout!r}"
    )
    obj = _stdout_json(result)
    assert obj["success"] is False


# ---------------------------------------------------------------------------
# apply-xliff — generic converter failure branch
# ---------------------------------------------------------------------------


def test_apply_xliff_generic_failure_exits_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply-xliff (docx) converter failure + --json must exit non-zero.

    RED on the unfixed code (exit 0), GREEN after T-01.
    """
    skeleton = tmp_path / "skeleton.docx"
    skeleton.write_bytes(b"not-a-real-docx")
    xliff = tmp_path / "translated.xlf"
    xliff.write_text("<xliff/>", encoding="utf-8")
    output = tmp_path / "out.docx"

    def _fake_convert(
        self: Any,
        input_path: Any,
        xliff_path: Any,
        output_path: Any,
        options: Any = None,
    ) -> ConversionResult:
        return ConversionResult(
            output_path=Path(output_path),
            success=False,
            errors=[ErrorDetail(code="CONVERSION_ERROR", message="synthetic failure")],
        )

    monkeypatch.setattr(
        "orf.channels.xliff2docx.XLIFF2DOCXConverter.convert",
        _fake_convert,
    )

    result = runner.invoke(
        main,
        [
            "apply-xliff",
            str(skeleton),
            "--xliff",
            str(xliff),
            "--output",
            str(output),
            "--format",
            "docx",
            "--json",
        ],
    )

    assert result.exit_code != 0, (
        "apply-xliff --json on a failed conversion must exit non-zero "
        f"(got {result.exit_code}); stdout={result.stdout!r}"
    )
    obj = _stdout_json(result)
    assert obj["success"] is False


# ---------------------------------------------------------------------------
# convert-batch — exit non-zero when fail_count > 0
# ---------------------------------------------------------------------------


def test_convert_batch_failure_exits_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """convert-batch with a failing file exits non-zero (fail_count > 0).

    Issue #102: ``--output-dir`` is auto-created now, so the failure is
    injected by stubbing the HTML converter instead of pointing at a
    missing output dir. The command must not report overall success via a
    zero exit code.

    RED on the unfixed code (exit 0), GREEN after T-01.
    """
    src = tmp_path / "src"
    src.mkdir()
    md = src / "doc.md"
    md.write_text(
        "---\nsource_lang: en\ntarget_lang: zh\n---\n\n# Hello\n",
        encoding="utf-8",
    )
    missing_out = tmp_path / "missing_out"

    def _fake_convert(self, input_path, output_path):
        return ConversionResult(
            output_path=Path(output_path),
            success=False,
            errors=[
                ErrorDetail(
                    code="CONVERSION_ERROR",
                    message="synthetic conversion failure",
                )
            ],
        )

    from orf.cli import MD2HTMLConverter

    monkeypatch.setattr(MD2HTMLConverter, "convert", _fake_convert)

    result = runner.invoke(
        main,
        [
            "convert-batch",
            str(src),
            "--target-format",
            "html",
            "--output-dir",
            str(missing_out),
            "--json",
        ],
    )

    assert result.exit_code != 0, (
        "convert-batch with fail_count > 0 must exit non-zero "
        f"(got {result.exit_code}); stdout={result.stdout!r}"
    )
