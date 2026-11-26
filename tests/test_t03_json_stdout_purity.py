"""T-03 regression: ``orf convert-batch --json`` stdout is one JSON object.

Plan gap T-03 (``.omo/plans/agent-oriented-gap-register.md`` §4.1,
"``--json`` stdout pollution"): ``convert_batch.py`` wrote the human
``Completed: N succeeded, M failed`` line to **stdout** before the JSON
payload (``convert_batch.py:242``), and the empty-directory branch wrote
``No files found to convert`` with no JSON at all. An agent doing
``json.loads(stdout)`` (``#json-parseable``) failed even though the
command succeeded.

The fix routes the human line to stderr under ``--json`` and emits the
documented summary object on every exit path. The JSON *schema*
(``success_count`` / ``fail_count`` / ``total``) is unchanged.

``test_*_stdout_single_object`` are the failing-first assertions: RED on
the unfixed code, GREEN once the human line moves to stderr.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from orf.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _parse_single_json(stdout: str) -> dict[str, Any]:
    """``json.loads`` the WHOLE stdout — rejects any banner/trailer."""
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict), f"stdout JSON is not an object: {type(parsed)}"
    return parsed


def _write_frontmatter_md(directory: Path, name: str = "doc.md") -> Path:
    """convert-batch only picks up MD files carrying YAML frontmatter."""
    directory.mkdir(parents=True, exist_ok=True)
    md = directory / name
    md.write_text(
        "---\nsource_lang: en\ntarget_lang: zh\n---\n\n# Hello\n",
        encoding="utf-8",
    )
    return md


def test_convert_batch_json_success_stdout_single_object(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A successful batch + ``--json`` → stdout parses as one object.

    RED on the unfixed code: stdout was
    ``\\nCompleted: 1 succeeded, 0 failed\\n{json}`` (``convert_batch.py:242``).
    """
    src = tmp_path / "src"
    _write_frontmatter_md(src)
    out = tmp_path / "out"
    out.mkdir()

    result = runner.invoke(
        main,
        [
            "convert-batch",
            str(src),
            "--target-format",
            "html",
            "--output-dir",
            str(out),
            "--json",
        ],
    )

    assert result.exit_code == 0, f"stderr: {result.stderr[-800:]}"
    obj = _parse_single_json(result.stdout)
    assert obj["success_count"] == 1
    assert obj["fail_count"] == 0
    assert obj["total"] == 1


def test_convert_batch_json_failure_stdout_single_object(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing batch + ``--json`` → ONE JSON object, non-zero exit.

    Issue #102: ``--output-dir`` is auto-created now, so the failure is
    injected by stubbing the HTML converter instead of pointing at a
    missing output dir. Contract: ``#exit-codes`` (non-zero) AND
    ``#json-parseable`` (single object).
    """
    from orf.converters.base import ConversionResult, ErrorDetail

    src = tmp_path / "src"
    _write_frontmatter_md(src)
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

    assert result.exit_code != 0, f"stdout: {result.stdout!r}"
    obj = _parse_single_json(result.stdout)
    assert obj["fail_count"] == 1
    assert obj["success_count"] == 0


def test_convert_batch_json_empty_dir_stdout_single_object(
    runner: CliRunner, tmp_path: Path
) -> None:
    """An empty input dir + ``--json`` still emits one JSON object.

    RED on the unfixed code: stdout was ``No files found to convert``
    with no JSON.
    """
    src = tmp_path / "empty_src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()

    result = runner.invoke(
        main,
        [
            "convert-batch",
            str(src),
            "--target-format",
            "html",
            "--output-dir",
            str(out),
            "--json",
        ],
    )

    assert result.exit_code == 0, f"stderr: {result.stderr[-800:]}"
    obj = _parse_single_json(result.stdout)
    # R-04 extended the schema: status + succeeded/failed/retryable were added;
    # the legacy T-03 keys keep their values.
    assert obj["status"] == "empty"
    assert obj["succeeded"] == 0
    assert obj["failed"] == 0
    assert obj["retryable"] == 0
    assert obj["success_count"] == 0
    assert obj["fail_count"] == 0
    assert obj["total"] == 0
