# Omni-Re-Formatter (ORF)

ORF 是 Omni 文档本地化生态的最后一环，负责将标准化中间件还原为目标复杂格式。

## 核心功能

- 读取 OL 翻译后的 MD/XLIFF 及 manifest.json/skeleton.zip
- 调用 Pandoc/md2pptx/translate-toolkit 等成熟轮子
- 配合自研胶水代码（manifest 解析、skeleton 回填、资源路径还原）
- 重组生成本地化的复杂格式文档
- **AI Agent 原生集成**：通过 MCP Server 对外暴露为 AI Agent Tool，支持 Foreman/Specialist 智能编排

## 支持格式

### MD 回写
- DOCX, ODT, EPUB, HTML, RTF, PDF, PPTX
- ICML (InDesign), SRT (字幕)

### XLIFF 回写
- DOCX, PPTX, EPUB, HTML, ODF

### 云存储
- AWS S3, Azure Blob Storage

### AI 辅助
- 布局溢出检测与修正

### 数据格式
- XLSX, CSV, JSON

### 结构化标记
- XML

### Jupyter 笔记本
- IPYNB

### 邮件格式
- EML, MSG

### 转换引擎依赖

- **pandoc 路径**：MD→{DOCX, ODT, EPUB, RTF, ICML} 需要 `pandoc`（由 `pypandoc-binary` 自动提供，`pip install omni-re-formatter` 时安装）。
- **纯 Python 路径**：MD→HTML 和 MD→PDF（weasyprint 引擎）不依赖 pandoc，使用 `markdown` 库 + WeasyPrint。
- **XLIFF 跨格式**：`apply-xliff --force` 可绕过格式校验，在跨格式场景下尝试回填并输出警告。
- **邮件格式**：MD→MSG 在缺少 `email_headers` frontmatter 时自动合成默认头，不再硬失败。
- **MD→MSG 格式**：需要 `aspose-email-foss`（GPLv3 开源 fork）— `pip install 'omni-re-formatter[email-output]'`。
  Aspose 是商业库。**推荐使用 .eml 格式**（ORF 完全支持，W2.1 已实现优雅回退），是开源标准。

## 安装

```bash
# 核心功能
pip install omni-re-formatter

# 开发依赖
pip install -e ".[dev]"

# 可选依赖
pip install -e ".[weasyprint]"   # PDF 生成
pip install -e ".[cloud]"       # S3/Azure 支持
pip install -e ".[ai]"          # AI 布局修正
pip install -e ".[office]"      # XLSX 支持
pip install -e ".[notebook]"    # IPYNB 支持
pip install -e ".[email-output]" # MSG 支持
pip install -e ".[mcp]"         # MCP Server (AI Agent 集成)
```

## 快速开始

### CLI 使用

```bash
# MD 转 DOCX
orf apply-md translated.md --target-format docx --output result.docx

# MD 转 EPUB — JSON 输出
orf apply-md translated.md --target-format epub --output result.epub --json

# XLIFF 回填（需要 skeleton.zip）
orf apply-xliff original.docx --xliff translated.xlf --output result.docx

# XLIFF 回填 + 图片注入（OPP 输出 images.json）
orf apply-xliff original.docx --xliff translated.xlf --output result.docx --images-json images.json

# 批量转换 — JSON 输出
orf convert-batch ./translated --target-format docx --pattern "*.md" --json
```

### MCP Server（AI Agent 集成）

```bash
# 启动 MCP Server
orf-mcp-server

# AI Agent 可通过 MCP 协议调用以下工具：
# - apply_md       将 MD 转换为目标格式
# - apply_xliff    应用 XLIFF 翻译到原始文档（含可选图片注入）
# - batch_convert  批量转换 MD 文件
# - detect_format  自动检测文档格式
# - info           获取文档信息
# - ping           健康检查（返回版本号）
```

所有 MCP 工具均使用 **Pydantic 类型模型** 进行输入输出验证，防止因 schema 不匹配导致的调用失败（实测可将此类错误从 38% 降至 0%）。

## CLI 命令

| 命令 | 说明 | JSON 输出 |
|------|------|-----------|
| `apply-md` | 将 MD 文件转换为目标格式 | `--json` 支持 |
| `apply-xliff` | 应用 XLIFF 翻译到原始文档 | `--json` 支持 |
| `convert-batch` | 批量转换 MD 文件 | `--json` 支持 |
| `info` | 显示文档信息 | `--json` 支持 |

## AI Agent 原生架构

ORF v0.3.0 重构为 **Agent-Oriented** 架构，专为 AI Agent 集成设计，而非传统 CLI 工具。

### MCP Server — 为 AI Agent 而生

ORF 的 MCP Server 基于 **FastMCP** 构建，对外提供 6 个工具，AI Agent（Claude、Cursor、OpenCode 等）可通过标准 MCP 协议直接调用 ORF 的文档转换能力。

```
AI Agent (Claude/Cursor) 
    │
    ▼ MCP 协议
┌──────────────────┐
│  ORF MCP Server   │
│  ┌──────────────┐ │
│  │ apply_md     │ │  ← Pydantic 类型验证
│  │ apply_xliff  │ │  ← 路径安全校验
│  │ batch_convert│ │  ← 子进程隔离
│  │ detect_format│ │
│  │ info         │ │
│  └──────────────┘ │
└──────────────────┘
```

**MCP 安全设计：**
- `PathValidator` 防止目录遍历攻击
- 所有路径输入通过 `resolve()` + 白名单策略校验
- CLI 调用于子进程隔离执行

### Foreman + Specialist Agent 编排系统

ORF 内置完整的 Agent 编排系统，可按文档格式自动路由到专业处理单元：

```
         ┌─────────────────┐
         │  ForemanAgent    │  ← 作业编排编排器
         │  评估复杂度       │
         │  分解子任务       │
         │  调度 Specialist  │
         └──────┬──────────┘
                │
     ┌──────────┼──────────┐
     ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────┐
│Format   │ │Data    │ │Markup  │
│Specialist│ │Specialist│ │Specialist│
│docx/odt │ │xlsx/csv│ │xml/html│
│epub/pptx│ │/json   │ │        │
└────────┘ └────────┘ └────────┘
                    ┌────────┐
                    │Email   │
                    │Specialist│
                    │eml/msg │
                    └────────┘
```

- **ForemanAgent**：接收作业请求 → 评估复杂度（SIMPLE / MODERATE / COMPLEX）→ 分解子任务 → 路由到 Specialist → 聚合结果
- **FormatSpecialist**：docx、odt、epub、pptx
- **DataSpecialist**：xlsx、csv、json
- **MarkupSpecialist**：xml、html
- **EmailSpecialist**：eml、msg
- **Error Recovery**：基于错误码的自动恢复策略（ABORT / SKIP / RETRY / MANUAL_INTERVENTION / FALLBACK）

### Human-in-the-Loop (HITL) 审批

对高风险操作提供人工审批流程：

| 场景 | 风险等级 | 需要审批 |
|------|---------|---------|
| 文件 > 100MB | HIGH | ✅ |
| 云存储上传（S3/Azure） | HIGH | ✅ |
| 需人工干预的恢复策略 | LIMITED | ✅ |
| 文件 > 500MB | UNACCEPTABLE | ✅ |
| 标准操作 | LOW | ❌ |

### 审计追踪

所有操作记录到 `logs/audit_{date}.log`，每条日志包含：
- **correlation_id**：跨系统追踪 ID
- **agent_id**：调用方 Agent 标识
- 时间戳、操作类型、结果状态

## Recent additions (v0.4.3)

### Inline image dedup (regression fix)

XLIFF→DOCX backfill now skips inline drawings that are already in the skeleton. Without this check, ORF re-injected 2 inline duplicates of the first 2 source images (IM 16, 组合 116) in the Haier DOCX — output had 13 `<w:drawing>` blocks instead of the source's 11. The floating-image path already had a similar dedup (positionH/V match); the inline path now mirrors it via document-wide `<wp:extent>` cx/cy match. Document-wide (not paragraph-local) is required because OPP's `paragraph_index` is off-by-one vs. ORF's `//w:p` enumeration.

Locked in by `tests/turnkey/test_image_fidelity.py::test_drawing_count_equals_source` (regression guard).

## Recent additions (v0.4.0)

ORF v0.4.0 扩展了图片处理能力，以适配 OPP 真实 LLM 提取的图片元数据。

### 浮动图片支持 (`wp:anchor`)

XLIFF→DOCX 通道现在支持 **浮动图片**（绝对定位，独立于段落流），由 OPP 在 `images.json` 中通过 `is_floating=true` 标记。当 `ImagePlacement` 包含 `wp_anchor_h` / `wp_anchor_v` / `wp_anchor_relative_h` / `wp_anchor_relative_v` 字段时，ORF 注入 `w:drawing > wp:anchor` 元素（含 `positionH` / `positionV` / `wrapNone`）而非默认的 `wp:inline`。这修复了之前 5/12 浮动图片被静默丢弃的问题。

### MD 图片分离模式 (`--separate-images`)

`apply-md` 命令新增 `--separate-images` / `--images-dir` 标志，启用 **"DOCX + images separate" 模式**。在此模式下，MD 中的 `![alt](path)` 引用被剥离到指定 `images_dir/`，pandoc 生成的 DOCX 仅包含文本结构，图片清单写入 manifest。此模式适用于：MD 源含大量图片导致 pandoc 内嵌体积膨胀、需将图片资源单独发布、需在 DOCX 之外维护图片版本控制等场景。默认启用（`separate_images=True`），可通过 `ConverterOptions(separate_images=False)` 关闭。

```bash
# 启用 MD 图片分离模式
orf apply-md translated.md --target-format docx --output result.docx \
  --separate-images --images-dir ./extracted_images
```

## 架构

```
src/orf/
├── channels/          # 格式转换通道 (MD→X, XLIFF→X)
├── converters/        # 转换器基类与流式处理
│   └── base.py        # ConversionResult, ErrorDetail, WarningDetail
├── parsers/          # manifest.json, frontmatter 解析
├── detection/         # 格式自动探测
├── resources/        # 图片资源管理
├── skeleton/          # skeleton.zip 加载与回填
├── cloud/            # S3, Azure Blob 客户端
├── ai/               # 布局分析与溢出修正
├── mcp/              # MCP Server (Agent 集成层)
│   ├── server.py     # FastMCP 服务，6 个工具
│   ├── schemas.py    # Pydantic 类型模型
│   ├── security.py   # PathValidator 路径安全
│   └── config.py     # MCP 配置
├── agents/           # Agent 编排系统
│   ├── foreman.py    # ForemanAgent 作业编排器
│   └── specialists/  # 格式专用专业 Agent
│       ├── format_specialist.py    # docx/odt/epub/pptx
│       ├── data_specialist.py      # xlsx/csv/json
│       ├── markup_specialist.py    # xml/html
│       └── email_specialist.py     # eml/msg
├── workflow/         # 工作流引擎
│   └── hitl_approval.py  # 人工审批流程
├── logging/          # 日志系统（含审计追踪）
└── error_handlers/   # 错误处理与恢复策略
```

## 项目状态 (v0.4.1)

- **Agent-Oriented 架构**：MCP Server + Foreman/Specialist + HITL 审批
- **图片精确定位注入**：支持 DOCX/PPTX/HTML/EPUB 图片按位置回填
- **MCP 工具**：6 个类型安全的 Agent 工具（Pydantic 模型验证）
- **路径安全**：PathValidator 防止目录遍历
- **审计日志**：correlation_id + agent_id 全链路追踪
- 400+ 个测试用例（含 MCP 集成测试 + 端到端管线测试）
- MD 回写: 9 种格式 (DOCX, ODT, EPUB, HTML, RTF, PDF, PPTX, ICML, SRT)
- XLIFF 回写: 5 种格式 (DOCX, PPTX, EPUB, HTML, ODF)
- 云存储: S3 + Azure Blob 集成
- AI: 布局溢出检测与修正
- 数据格式: XLSX, CSV, JSON
- 结构化标记: XML
- Jupyter 笔记本: IPYNB
- 邮件格式: EML, MSG
- 完整的错误处理和日志系统

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev,mcp]"

# 运行测试
pytest tests/

# 仅测试 MCP
pytest tests/test_orf_mcp_server.py -v

# 仅测试端到端管线
pytest tests/test_e2e_omni_pipeline.py -v

# 代码检查
ruff check src/orf
mypy src/orf --ignore-missing-imports

# 构建包
python -m build
```

## Pipeline — Omni Localization Suite

ORF is **Step 3** (final step) of the Omni Localization Suite pipeline:

```
┌────────────────────────────────────────────────────────────────────────┐
│                     OMNI LOCALIZATION SUITE                             │
│                                                                        │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐               │
│  │     OPP     │───▶│     OL      │───▶│     ORF      │               │
│  │  (提取)     │    │   (翻译)    │    │   (回写)    │               │
│  └─────────────┘    └─────────────┘    └─────────────┘               │
│                                                                        │
│  Step 1: OPP        Step 2: OL            Step 3: ORF                  │
│  Extract →          Translate →           Backfill →                  │
│  MD + XLIFF +       MD + XLIFF            DOCX/PPTX                   │
│  skeleton.zip                                                    │
└────────────────────────────────────────────────────────────────────────┘
```

### Complete Workflow

```bash
# Step 1: OPP - Extract document to MD/XLIFF + skeleton.zip
opp --target-format=both --source-lang=en --target-lang=zh document.docx

# Step 2: OL - Translate to target language
ol translate-md document.md -s en -t zh -o translated/

# Step 3: ORF - Backfill translated content to target format ← YOU ARE HERE
orf apply-xliff document.docx --xliff translated/document.xlf --output result.docx
```

## Related Projects

- [OPP (Omni-Pre-Processor)](https://github.com/1StepMore/Omni_Pre_Processor) - **PREREQUISITE**. Produces skeleton.zip and manifest.json that ORF uses.
- [OL (Omni-Localizer)](https://github.com/1StepMore/Omni_Localizer) - **PREREQUISITE**. Produces translated MD/XLIFF that ORF backfills.

## 许可证

MIT
