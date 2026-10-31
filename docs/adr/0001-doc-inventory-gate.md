# 0001. 文档真值门禁（doc_inventory + ADR 体系）

- 状态：已接受
- 日期：2026-10-31
- 决策人：maintainer（issue ORF#73）

## 背景

ORF 的 README / AGENTS 携带会随功能变化的数字（7 个 MCP 工具、16 个
MD 输出格式、9 个 XLIFF 回填格式），此前没有机制校验它们与源码派生值
一致；架构决策无 ADR 可追溯；AGENTS.md 357 行超出 200 行 adherence
上限。

## 决策

1. 新增 `scripts/doc_inventory.py`：派生（7 tools from
   `_TOOL_DISPATCH`、16 MD outputs from `_MD_OUTPUT_FORMATS`、9 XLIFF
   backfills from `_XLIFF_BACKFILL_FORMATS`、场景数+tier、版本），门禁
   README/AGENTS 声明，反引号路径两档存在性检查（本仓总是强制；suite
   父仓仅在树存在时强制——CI 单仓 checkout 打印跳过说明），生成
   `docs/dev/doc-inventory.md` 行集 + 新鲜度检查。**注意 fail-CLOSED**：
   导入 `orf.mcp.*` 前对 `MCP_ALLOWED_DIRECTORIES` setdefault 占位值
   （审计只读注册表）。
2. 新增 `docs/adr/`：TEMPLATE + 索引 + ADR 0001。
3. 新增开发 skill `.opencode/skills/orf-dev/SKILL.md`（加新 channel/
   格式的操作清单）。
4. AGENTS.md 瘦身 357 → ≤200（细节移入 docs/dev/ 五个子文档）。
5. CI（test.yml）加 `doc_inventory --check` 步骤。

## 备选方案

- **手写一致性检查** —— 缺「生成行集 + 新鲜度」；未选。
- **不引入，靠 review** —— 同型门禁（套件仓 / OPP / OL）上线即各抓到
  真实断链（本仓门禁首跑抓到 `docs/API.md` 引用已包化的
  `xliff2docx.py`）；review 实测不可靠。

## 后果

- 正面：关键数字 CI 强制为对；断链在 PR 阶段暴露；AGENTS 回到
  adherence 上限内。
- 负面 / 代价：改文档后须重新生成 inventory；声明点清单随新文档维护。
- 中性：`scripts/` 新增常驻脚本；`orf.mcp` 导入发生在 --check 运行时。

## 验证

- `python scripts/doc_inventory.py --check` exit 0；
- 门禁首跑抓到 `xliff2docx.py` → `xliff2docx/` 包化后的陈旧引用（本
  PR 修复）。
