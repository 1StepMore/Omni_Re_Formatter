# Inventory — Omni_Re_Formatter @ 8ba33043cad42a5c3261d7e7cbb14b5386d94ad7 (2026-11-07)  SOURCE=LOCAL（§7.5）
| ID | 子系统 | 路径 | 类别 | 测试现状 | 文档现状 | CI 覆盖 | 判定依据 | 处置建议(A编号) |
|----|--------|------|------|----------|----------|---------|----------|----------------|
| S-01 | 回填引擎 | src/orf/(16 输出格式, Foreman/Specialist) | 源码 | tests/ | AGENTS.md(175行≤200✅) | test.yml | AGENTS.md | A2 路由指针/A10 核对 |
| S-02 | 测试 | tests/ | 测试 | 有 | — | test.yml | 模块测试套件 | 不动 |
| S-03 | 文档 | docs/ + AGENTS.md + README | 文档 | — | suite: 前缀✅ | doc_inventory(副本) | 跨仓检查(#253) | A13 副本同步(O-2) |
| S-04 | CI | .github/workflows/(test.yml,release.yml + ?? pinned-guard.yml) | CI | — | — | 自身 | — | A7: test.yml 增一致性 job(O-2) |
| S-05 | 工程 | Makefile/pyproject.toml | CI | make test/lint✅ | — | — | 参数取证 | 参数区回填 |

## 待废弃清单（仅标记不删除）
- 疑似：orf_mcp_refdoc_*.docx ×2（未跟踪测试残留；不删,留 Owner）
## 敏感文件清单（仅路径）
.env.example（无 .env）
## 现有 agent 设施清单（D4）
AGENTS.md(保留+路由指针) / .opencode(保留)
## 前置缺口登记（A14）
- 无
## 工作区脏点（D2 类推处置记录）
- ?? .github/workflows/pinned-guard.yml（同上,遗留项）
- ?? orf_mcp_refdoc_ce40t_9b.docx / orf_mcp_refdoc_uy9fbvjg.docx（测试残留,不删,遗留项）
## 回填参数区
BUILD_CMD=pip install -e .; TEST_CMD=make test; LINT_CMD=make lint; E2E_CMD=(模块级); E2E_RUNNER=根仓 e2e-tests.yml; REPO=Omni_Re_Formatter
