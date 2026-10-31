#!/usr/bin/env python3
"""coverage_matrix — ORF 生产矩阵校验（issue ORF#74）。

从源码派生「MD 输出格式 × XLIFF 回填格式 × MCP 工具」的真值，与
docs/dev/production-matrix.md 比对：行集合、工具提及、头部计数必须与
派生值一致。

用法：
    python scripts/coverage_matrix.py --check     # 校验（exit 1 = 漂移）
    python scripts/coverage_matrix.py             # 打印派生真值

注意 fail-CLOSED：导入 orf.mcp.* 前对 MCP_ALLOWED_DIRECTORIES setdefault
占位值（审计只读注册表，值惰性）。与 OPP/OL 的 coverage_matrix 同源。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MATRIX_PATH = ROOT / "docs" / "dev" / "production-matrix.md"


def _derive() -> dict:
    os.environ.setdefault("MCP_ALLOWED_DIRECTORIES", "/tmp")
    from orf.mcp.server import _TOOL_DISPATCH
    from orf.mcp.tools.get_capabilities import (
        _MD_OUTPUT_FORMATS,
        _XLIFF_BACKFILL_FORMATS,
    )

    return {
        "tools": sorted(_TOOL_DISPATCH),
        "md_outputs": list(_MD_OUTPUT_FORMATS),
        "xliff_backfills": list(_XLIFF_BACKFILL_FORMATS),
        "scenarios": _scenario_count(),
    }


def _scenario_count() -> int:
    scenarios = ROOT / "scenarios"
    if not scenarios.is_dir():
        return 0
    return len([
        p for p in scenarios.rglob("*.yaml")
        if p.is_file() and not any(part.startswith(".") for part in p.parts)
    ])


_COVERAGE_MARKS = ("✅", "⚠️", "❌")
_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_-]+)`\s*\|(.+)\|\s*$")


def _parse_rows(section_header: str) -> list[tuple[str, str]]:
    if not MATRIX_PATH.is_file():
        raise SystemExit(f"missing {MATRIX_PATH} — write the matrix first")
    rows, in_section = [], False
    for line in MATRIX_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"## {section_header}"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            m = _ROW_RE.match(line)
            if m:
                rows.append((m.group(1), m.group(2)))
    return rows


def check() -> int:
    failures: list[str] = []
    truth = _derive()
    body = MATRIX_PATH.read_text(encoding="utf-8")

    # 1) MD 输出格式行
    md_rows = [f for f, _ in _parse_rows("MD output channels")]
    if set(md_rows) != set(truth["md_outputs"]):
        failures.append(
            f"MD output rows out of sync: missing="
            f"{sorted(set(truth['md_outputs']) - set(md_rows))} "
            f"extra={sorted(set(md_rows) - set(truth['md_outputs']))}"
        )
    for _, rest in _parse_rows("MD output channels"):
        if not any(mark in rest for mark in _COVERAGE_MARKS):
            failures.append("an MD output row carries no ✅/⚠️/❌ mark")

    # 2) XLIFF 回填行
    xliff_rows = [f for f, _ in _parse_rows("XLIFF backfill channels")]
    if set(xliff_rows) != set(truth["xliff_backfills"]):
        failures.append(
            f"XLIFF backfill rows out of sync: missing="
            f"{sorted(set(truth['xliff_backfills']) - set(xliff_rows))} "
            f"extra={sorted(set(xliff_rows) - set(truth['xliff_backfills']))}"
        )

    # 3) 工具名逐个出现
    for tool in truth["tools"]:
        if f"`{tool}`" not in body:
            failures.append(f"matrix does not mention tool `{tool}`")

    # 4) 头部计数行
    for pattern, expected in (
        (re.compile(r"MCP tools: \*\*(\d+)\*\*"), len(truth["tools"])),
        (re.compile(r"MD output formats: \*\*(\d+)\*\*"), len(truth["md_outputs"])),
        (re.compile(r"XLIFF backfill formats: \*\*(\d+)\*\*"), len(truth["xliff_backfills"])),
        (re.compile(r"scenarios: \*\*(\d+)\*\*"), truth["scenarios"]),
    ):
        m = pattern.search(body)
        if not m:
            failures.append(f"matrix header missing count line: {pattern.pattern}")
        elif int(m.group(1)) != expected:
            failures.append(
                f"matrix header claims {m.group(0)!r} but source derives {expected}"
            )

    if failures:
        print("coverage matrix check FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        f"coverage matrix check passed: {len(truth['md_outputs'])} MD outputs, "
        f"{len(truth['xliff_backfills'])} XLIFF backfills, "
        f"{len(truth['tools'])} tools, {truth['scenarios']} scenarios in sync."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coverage_matrix")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if not args.check:
        truth = _derive()
        print(f"MD outputs ({len(truth['md_outputs'])}):", ", ".join(truth["md_outputs"]))
        print(f"XLIFF backfills ({len(truth['xliff_backfills'])}):",
              ", ".join(truth["xliff_backfills"]))
        print(f"MCP tools ({len(truth['tools'])}):", ", ".join(truth["tools"]))
        print(f"scenarios: {truth['scenarios']}")
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())
