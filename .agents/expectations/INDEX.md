<!-- 薄映射层（C 口径，owner 定案 e2e-test-suite#451，2026-11-28）：
期望唯一事实源 = 本仓 `scenarios/STANDARDS.md`。本文件只做指针映射，禁止复制正文；
改期望只能经 Owner 显式指令改 STANDARDS.md，改本映射层不得放松任何期望（C7 机检锚点存在性）。
套件级期望 E-08..E-14（LQA / 段落比例 / CJK 密度 / 未翻译单元 / 标点卫生 / Drawing 计数 / 产物可打开）
中，本仓 STANDARDS 已定义的以 M-xx 列出；未定义的仅在 e2e-test-suite 生效。 -->


# Expectations Index — Omni_Re_Formatter

| id | 标题 | 期望来源（唯一事实源锚点） | owner | 版本 |
|---|---|---|---|---|
| E-01 | Fallbacks 永不作为质量证据 | `scenarios/STANDARDS.md` #fallbacks-never-evidence | Owner | 2026-11-28 |
| E-02 | 环境门禁语义（unconfigured ≠ pass） | `scenarios/STANDARDS.md` #env-gate-semantics | Owner | 2026-11-28 |
| E-03 | 工具契约符合性 | `scenarios/STANDARDS.md` #tool-contract | Owner | 2026-11-28 |
| E-04 | JSON 可解析性 | `scenarios/STANDARDS.md` #json-parseable | Owner | 2026-11-28 |
| E-05 | 错误信息清晰度 | `scenarios/STANDARDS.md` #error-clarity | Owner | 2026-11-28 |
| E-06 | 路径安全 | `scenarios/STANDARDS.md` #path-security | Owner | 2026-11-28 |
| E-07 | 退出码契约 | `scenarios/STANDARDS.md` #exit-codes | Owner | 2026-11-28 |
| M-01 | Backfilled file is produced | `scenarios/STANDARDS.md` #backfill-output | Owner | 2026-11-28 |
| M-02 | HTML structural integrity | `scenarios/STANDARDS.md` #html-structure | Owner | 2026-11-28 |
| M-03 | JSON parseability of backfill output | `scenarios/STANDARDS.md` #json-structure | Owner | 2026-11-28 |
| M-04 | CSV parseability of backfill output | `scenarios/STANDARDS.md` #csv-structure | Owner | 2026-11-28 |
| M-05 | XML well-formedness | `scenarios/STANDARDS.md` #xml-structure | Owner | 2026-11-28 |
| M-06 | SRT passthrough | `scenarios/STANDARDS.md` #srt-structure | Owner | 2026-11-28 |

Tester 判定输出 = 本仓 `.agents/roles/tester.md` 固定 JSON；执行载体 = 本仓 `scenarios/*.yaml` + `uv run pytest`（场景本体不进模块仓，跨仓矩阵在 e2e-test-suite）。
