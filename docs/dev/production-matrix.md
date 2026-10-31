# ORF production matrix — outputs × backfills × MCP tools (issue ORF#74)

Derived truth: MCP tools: **7** · MD output formats: **16** · XLIFF backfill formats: **9** · scenarios: **6**
（计数由 `scripts/coverage_matrix.py --check` 与源码派生值比对；
`scripts/doc_inventory.py --check` 门禁 README/AGENTS 的对应声明。）

标注含义：✅ 主路径可用 · ⚠️ 可用但依赖可选包 / 外部二进制 · ❌ 不支持。
引擎明细与选型决策表见 `docs/dev/converters.md`；skeleton 契约见
`docs/dev/xliff-backfill.md`。

## MD output channels（`apply-md --target-format <fmt>`，16）

| 格式 | 标注 | 引擎 / 备注 |
|------|------|-------------|
| `docx` | ✅ | pandoc（`pypandoc-binary` 自动装）；`--reference-doc` 自定义样式 |
| `odt` | ✅ | pandoc |
| `epub` | ✅ | pandoc |
| `html` | ✅ | `markdown` 库（纯 Python，无 pandoc 依赖） |
| `rtf` | ✅ | pandoc |
| `pdf` | ⚠️ | `weasyprint` extra；缺省回退 pandoc |
| `pptx` | ⚠️ | `md2pptx` **外部脚本**（E2E-79）；缺 md2pptx 时回退 pandoc；双缺报错 |
| `icml` | ✅ | pandoc |
| `srt` | ✅ | `srt` 库 |
| `csv` | ✅ | pandas |
| `xlsx` | ⚠️ | `openpyxl`（`[office]` extra） |
| `xml` | ✅ | lxml |
| `ipynb` | ⚠️ | `nbformat`（`[notebook]` extra） |
| `eml` | ✅ | `email` 标准库 |
| `msg` | ⚠️ | 商业 `aspose-email-foss`（GPLv3）；推荐 `.eml` |
| `json` | ✅ | 标准库直出 |

## XLIFF backfill channels（`apply-xliff`，9；全部需要 OPP 的 skeleton.zip）

| 格式 | 标注 | 备注 |
|------|------|------|
| `docx` | ✅ | `xliff2docx`（包化目录）；E2E-07 图片去重 |
| `pptx` | ✅ | `xliff2pptx` |
| `epub` | ✅ | `xliff2epub` |
| `html` | ✅ | `xliff2html`（OPP#92 skeleton） |
| `odt` | ✅ | `xliff2odt` |
| `pdf` | ❌ | 无回填通道（PDF skeleton 由设计拒绝） |
| `json` | ✅ | `xliff2json` |
| `xlsx` | ✅ | `xliff2xlsx`（`xliff_map.json` sidecar，OPP#94） |
| `eml` | ✅ | `xliff2eml`（`eml_map.json` sidecar，2026-10-27） |

**跨格式 XLIFF 恒为 ❌**（结构见 ACCEPTED_GAPS.md）——用 `apply-md` 替代。

## MCP tools（7，套件 coverage_audit 的 orf 行必须 7/7）

| 工具 | 能力域 |
|------|--------|
| `apply_md` | MD → 16 输出（inline content E2E-76 或路径） |
| `apply_xliff` | XLIFF + skeleton 回填（同格式） |
| `batch_convert` | 目录批量 |
| `detect_format` | 魔数检测 |
| `info` | 格式 + 大小 + 资源数 |
| `ping` | 健康 |
| `get_capabilities` | 能力自述（本矩阵的机器源） |
