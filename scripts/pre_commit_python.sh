#!/usr/bin/env bash
# 为 pre-commit 钩子解析一个**真正可运行**的 Python 解释器，然后把参数交给它执行。
#
# 为什么需要这个脚本（2026-09-17 实测，见 docs/project-health-report-2026-09-17.md）
# -----------------------------------------------------------------------------
# 1. 裸 `python3` 在不同 shell 里含义不同：
#    * Git Bash（Windows）→ 命中 Microsoft Store 的 execution-alias 占位程序
#      `%LOCALAPPDATA%\Microsoft\WindowsApps\python3`，它不执行任何代码，直接返回
#      49（PowerShell/CMD 下返回 9009）。门禁于是"因环境原因失败"而非运行；
#      同机 `python` 才是真的解释器，且它没装本项目（会 import 失败）。
#    * WSL / Linux → `python` 不存在，只有 `/usr/bin/python3`。
# 2. `[ -x .venv_ol/bin/python ]` 这种守卫在 Git Bash 下恒为假：该符号链接指向
#    Linux ELF 解释器（/home/.../cpython-3.13-linux-x86_64-gnu/bin/python3.13），
#    Windows 无法执行。于是四个门禁走了 "skip → exit 0" 分支 = **跳过即通过**，
#    形成伪绿（scenarios/STANDARDS.md#fallbacks-never-evidence 明确禁止这种证据）。
#
# 解析规则（存在 ≠ 可执行：每个候选都用 `-c ''` 真跑一次）
# -----------------------------------------------------------------------------
# 第 1 层 —— 项目 venv，按布局逐个试（POSIX `bin/` 与 Windows `Scripts/`）：
#     .venv_ol（CI/文档所指的共享 venv）、.venv_win（setup_dev.ps1 在 Windows 建的）、
#     .venv。第一个真能跑的即采用。
# 第 2 层 —— venv 目录存在却没有可用解释器 = 环境坏了：**红**，不许静默跳过。
# 第 3 层 —— 仓库里根本没有 venv（全新克隆、尚未 bootstrap）：才退回系统
#     `python` / `python3` / `py`（所以"缺依赖就跳过"的既有语义保持不变）。
#
# 用法：
#   bash scripts/pre_commit_python.sh scripts/doc_inventory.py --check
#   bash scripts/pre_commit_python.sh -m pytest tests/security -q
set -u

# 候选是否真的可执行：存在（command -v）+ 真能解释一行代码。
_omni_runnable() {
    command -v "$1" >/dev/null 2>&1 && "$1" -c '' >/dev/null 2>&1
}

# 第 1 层：项目 venv（两种目录布局都列出来，Windows 上只有 Scripts/ 可执行）。
for _omni_candidate in \
    .venv_ol/bin/python \
    .venv_ol/Scripts/python.exe \
    .venv_win/bin/python \
    .venv_win/Scripts/python.exe \
    .venv/bin/python \
    .venv/Scripts/python.exe
do
    if _omni_runnable "$_omni_candidate"; then
        exec "$_omni_candidate" "$@"
    fi
done

# 第 2 层：有 venv 却一个都跑不起来 —— 环境坏了，必须红。
if [ -d .venv_ol ] || [ -d .venv_win ] || [ -d .venv ]; then
    echo "FAIL: a venv directory exists but no interpreter in it is runnable here" >&2
    exit 1
fi

# 第 3 层：全新克隆（无 venv），退回系统解释器。
for _omni_candidate in python python3 py; do
    if _omni_runnable "$_omni_candidate"; then
        exec "$_omni_candidate" "$@"
    fi
done

echo "skip: no runnable Python interpreter found (venv not built, none on PATH)" >&2
exit 0
