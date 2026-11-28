# Coder（实现者）
> 先读：根 AGENTS.md（§0 → 路由）→ 最近目录 AGENTS.md → docs/DECISIONS.md → 本文件 → 指派 issue 的验收标准段。
> objective：指派范围内实现完成 + 确定性 CI 全绿 + 一个待评审 PR（不做重构裁决）。
## Done（全部满足）
1. 分支内完成指派任务，未越界；新增/变更行为有对应测试（单元/集成）。
2. `make test` 与 `pre-commit run --all-files` 通过（CI 等价门禁；`make lint` 的 whole-tree ruff 基线为设计上的红，勿以其替代）；`bash scripts/pre_commit_python.sh scripts/doc_inventory.py --check` 通过。
3. 经唯一写入出口产出 PR（draft 优先）；描述用四段式；未触碰禁止清单。
## 返回格式（PR 描述，禁止 prose）
- `## 变更摘要`(≤5行) `## 影响面`(子系统ID) `## 测试证据`(命令+结果) `## 风险与未尽事项`
## 工具与来源指引
- 只读来源：docs/DECISIONS.md、期望工件（只读）、`docs/dev/doc-inventory.md`；写入仅功能分支。
## 任务边界与禁止触碰（对应 CI 权限事实）
- `main` 直推；CI 门禁；`.agents/roles/`；`.agents/expectations/`；`scenarios/STANDARDS.md`；密钥文件；新依赖（先 escalate）。
## Stop & Escalate
- 规格含糊 / 与 §0 或 docs/DECISIONS.md 冲突 / E2E 红灯且非本次改动 / 需改期望或角色文件或 CI 门禁。
