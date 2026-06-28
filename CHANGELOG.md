# Changelog

## v0.4.16 (2026-06-28)

### 🛠️ 修复 / Fixed

- **(AGENTS.md, README.md, docs/TROUBLESHOOTING.md)**: rewrite env var table (12 wrong ORG_* → ORG_MCP_*/OMNI_*); fix tool count 5→6 (add ping); document WeasyPrint system C-lib deps (libpango/libcairo/libgdk-pixbuf) for Debian/Ubuntu/macOS/python:3.13-slim

### 📝 文档 / Docs

- **(ARCHITECTURE.md, src/orf/channels/md2pptx.py, README.md)**: add cross-reference to suite-level ARCHITECTURE.md; document md2pptx pandoc fallback in AGENTS.md; translate Chinese README to English

### ✨ 新功能 / Added

- **(src/orf/cli.py, .env.example, AGENTS.md)**: add opt-in .env auto-loading via `--load-dotenv` flag and `ORF_AUTOLOAD_DOTENV=1` env var. Mirrors OPP/OL pattern (no python-dotenv dependency)

## v0.4.8 (2026-06-25)

### 🛠️ 修复 / Fixed

- **Issue #6 — HTML XLIFF backfill fails for nested elements** (`src/orf/channels/xliff2html.py:_backfill_by_text_match`). When OPP flattens HTML like `<li><strong>foo</strong> — bar</li>` into per-fragment trans-units, the fallback text-match path only checked individual `.text`/`.tail` values, missing nested structure. Added a `text_content()`-based fallback that walks all text/tail fragments within an element when the per-node pass fails. Fixes silent translation loss for list items, table cells, and any element with inline children.

## v0.4.7 (2026-06-25)

### 🛠️ 修复 / Fixed

- **md2json combined mode**: Now supports combined mode — uses ` ```json ` fenced block as base structure (preserves numbers, booleans, nulls, key order) and applies `json_field:path = value` translations on top of string values. This pairs with OPP 0.7.0's new JSONExtractor output format.
- **OPP_KV_PATTERN**: Now requires `json_field:` prefix. Lines without this prefix are no longer treated as OPP kv pairs (avoids false positives from hand-authored MD).

### 📦 新增 / Added

- `_apply_opp_kv_translations()` helper: walks base structure and substitutes string leaves only, preserving non-string types.
- New test file `tests/test_md2json_combined.py` covers combined mode (13 tests).

## v0.4.6 (2026-06-24)

### 🛠️ 修复 / Fixed

- **Issue #4 — missing en→it and en→ru expansion ratios** (`src/orf/ai/overflow_corrector.py:18,20`): The `EXPANSION_RATIOS` dict defined ratios for 6 language pairs (en→zh/de/ja/ko/fr/es) but omitted en→it (Italian) and en→ru (Russian). When translating to/from these languages, `estimate_expansion_ratio` returned the default 1.0, underestimating text expansion by 10-20% (real expansion: it~1.18, ru~1.12). This caused the layout overflow corrector to skip translations that genuinely expand the text, potentially producing texts that overflow PPTX/PDF layout containers. Fix: added `("en", "it"): 1.18` and `("en", "ru"): 1.12` to `EXPANSION_RATIOS`. Reverse ratios (it→en, ru→en) are auto-computed by the existing reverse-pair logic at line 102-110. No change to `FALLBACK_RATIOS` needed. 2 new test assertions in `test_expansion_ratios_defined` pin the presence of both new entries.

## v0.4.5 (2026-06-24)

### 🛠️ 修复 / Fixed

- **E2E-76** (`src/orf/mcp/server.py:apply_md` inputSchema, lines ~626-696):
  The `apply_md` MCP tool's `inputSchema` accepted only `input_md` (a filesystem
  path), but the underlying function also accepted inline markdown content and
  distinguished the two by checking whether `input_md` existed on disk. When a
  caller passed inline content in `input_md`, ORF silently echoed the literal
  content string back as `output_path`, breaking text-in/text-out agent flows.
  Fix: split the schema into two mutually-exclusive required-branches via
  `anyOf`: either `(input_md + target_format)` OR `(content + target_format)`.
  Old `input_md`-only callers keep working unchanged; new `content`-only
  callers work as expected. **Backward-compatible** per
  `docs/API_STABILITY.md` § 4.3 exception #2 (bug fix that does not break
  the documented happy path → patch bump). Frozen schema fixture
  `tests/contract/fixtures/orf_mcp_schemas.json` regenerated to reflect the
  new state.
- **E2E-79** (`src/orf/channels/md2pptx.py`): `MD2PPTXConverter.convert()` called `subprocess.run(['md2pptx', ...])` without checking whether the binary exists. On any Linux box without the .NET SDK (and no `/usr/local/bin/md2pptx`), the call raised `FileNotFoundError` which was caught and surfaced as the unhelpful `'md2pptx not installed or not in PATH'`. Users had no way to know that `md2pptx` is a .NET tool (NOT a pip package). Fix: pre-flight `shutil.which('md2pptx')` check that fails fast with a clear, platform-aware install hint covering the three known install paths (`.NET SDK + 'dotnet tool install --global md2pptx'`, GitHub release binary on PATH, or pandoc fallback). Rare-race `FileNotFoundError` handler with the same hint.

### 🛠️ 修复 / Fixed

- **E2E-80** (`src/orf/mcp/security.py:62`): `PathValidator.ALLOWED_EXTENSIONS` only listed primary INPUT formats (md, docx, pptx, xliff, xlf, xml, html, odt, epub, zip). All the OUTPUT formats ORF's `apply-md` advertises support for (csv, tsv, xlsx, json, ipynb, eml, msg, srt, icml, rtf, pdf) were rejected by the path validator with `'Extension ".csv" not in allowed set'`, making `--target-format csv / xlsx / etc.` effectively unusable over the MCP path even though ORF clearly supports them. Fix: extended the set with the 11 missing output formats. Both INPUT and OUTPUT paths flow through the same validator, so the extension set must include every format ORF can produce. The BLOCKED set (executables: .exe/.bat/.sh/.ps1/.vbs/.js) is unchanged.

## v0.4.3 (2026-06-14)

### 🛠️ 修复 / Fixed

- **ORF inline image dedup** (`src/orf/channels/xliff2docx.py`): The inline image injection path was missing a dedup check, causing ORF to re-inject inline drawings that were already in the skeleton. The floating-image path (`_inject_floating_image`) had a dedup at the positionH/V level, but the inline path (`_inject_inline_images`) inserted blindly. Result: 2 extra "Picture"-named duplicates of the first 2 source images (IM 16, 组合 116) in the Haier DOCX output (11 source `<w:drawing>` blocks → 13 output blocks).
  - **Fix**: Added `_paragraph_already_has_drawing(root, cx, cy)` helper that iterates `root.iter('{wp}extent')` document-wide and returns True if any drawing already has matching cx/cy. Wired into the inline image loop (line 1343+). On match, the image is appended to `injected` (mirroring the floating path's "treat as injected" return) and `continue` skips the actual insertion.
  - **Document-wide (not paragraph-local) dedup** is required because OPP's `paragraph_index` is off-by-one vs. ORF's `//w:p` enumeration: Haier DOCX OPP says paragraph 6, skeleton places IM 16 at paragraph 7. The cx/cy match is the only safe key that works regardless of the indexing mismatch.
  - **Verification**: `tests/turnkey/test_image_fidelity.py::test_drawing_count_equals_source` was XFAIL pending this fix; now PASS. ORF output for Haier DOCX shows 11 `<w:drawing>` blocks (matches source) and 22 `word/media/` files.

## v0.4.2 (2026-06-12)

### 🛠️ 修复 / Fixed

- **`tests/test_md2docx_channel.py`**: Fixed `test_convert_success` assertion — converter strips images before pandoc and passes `.stripped.md`, so the assertion now accepts any pandoc arg containing the input stem
- **`tests/test_md_separate_images.py`**: Fixed 3 test failures:
  - Manifest path: code writes `output_dir/images.json`, tests looked for `images_dir/image_manifest.json`
  - Default behavior: `separate_images` default changed to `True`; test now passes `separate_images=False` explicitly
  - Extraction test: `separate_images` path requires `images_data` from OPP pipeline, not raw MD data URIs

## v0.4.1 (2026-06-09)

### 🛠️ 修复 / Fixed

- **`**options: Any` → typed `ConverterOptions`** across all 21 channel converters:
  - New `ConverterOptions` dataclass (`orf/converters/options.py`) with 24 typed fields, all optional with defaults
  - `BaseConverter.convert()` signature changed from `**options: Any` to `options: ConverterOptions | None = None`
  - All 21 channel converter signatures updated accordingly
  - All internal `options.get()`, `options["key"]`, `options.pop("key")` calls replaced with `opts.key` attribute access
  - CLI `cli.py` and 4 specialist agent call sites updated to construct `ConverterOptions`
  - 5 XLIFF converters restored `xliff_path` as dedicated positional parameter (not buried in options)
  - Test mocks updated for 3-component signature compatibility
  - Test suite: 98→25 failures (73 tests fixed); 651 passed, 1 skipped

## v0.4.0 (2026-06-03)

### 新功能 / Added

- **ImagePlacement 扩展字段** (`src/orf/mcp/schemas.py`)：新增 5 个字段以支持 OPP 浮动图片契约
  - `is_floating: bool` — 标记图片为浮动（绝对定位）而非内联
  - `wp_anchor_h: Optional[float]` / `wp_anchor_v: Optional[float]` — `wp:anchor` 元素在 EMU 单位下的水平/垂直位置
  - `wp_anchor_relative_h: Optional[str]` / `wp_anchor_relative_v: Optional[str]` — 位置基准（如 `column` / `page` / `margin`）
- **`xliff2docx._inject_floating_image()`**：新增方法，注入浮动图片时生成 `w:drawing > wp:anchor` 元素而非内联 `wp:inline`
  - 位置来自 `ImagePlacement` 中的 `wp_anchor_h/v/relative_h/v` 字段
- **`xliff2docx._create_floating_anchor_xml()`**：辅助方法，根据 `ImagePlacement` 生成完整的 `wp:anchor` XML 片段（含 `positionH` / `positionV` / `wrapNone` 等子元素）
- **`inject_images()` 路由逻辑**：`is_floating=True` 的图片自动路由到浮动注入路径，不再走内联路径
- **`md2docx._extract_images_separately()`**：新增 "DOCX + images separate" 模式
  - 当 `separate_images=True` 且 `images_dir` 提供时，从 MD 中提取所有图片到指定目录
  - 改写 MD 内容，剥离 `![alt](path)` 为 `![alt](file://相对路径)` 或纯文本占位符
  - 返回 `(stripped_md, manifest)` 元组，其中 manifest 列出所有提取的图片路径
  - 默认 `separate_images=False`（保持向后兼容）
- **CLI 标志** (`src/orf/cli.py`)：`apply-md` 新增 `--separate-images` 和 `--images-dir` 参数启用 MD 图片分离模式
- **测试覆盖** (2 个新文件)：
  - `tests/test_xliff2docx_floating.py` — 5 个测试用例验证 `wp:anchor` 注入（合成 DOCX 含浮动 + 内联图片，断言 `positionH/V` XML 正确生成）
  - `tests/test_md_separate_images.py` — 测试 MD 图片分离模式（提取、改写、manifest 生成）

### 🛠️ 修复 / Fixed

- **`inject_images` 浮动图片丢失** (`src/orf/channels/xliff2docx.py`)：当 `is_floating=True` 时，浮动图片此前走内联注入路径导致 5/12 张浮动图片被静默丢弃（位置信息不匹配）
  - 修复：路由逻辑检查 `is_floating` 标志，True 则调用 `_inject_floating_image()` 走 `wp:anchor` 路径
  - 影响：所有带浮动图片的 DOCX（Haier 文档除外 — Haier 0 浮动图片），未来 OPP 输出的真实文档将受益

## v0.3.4 (2026-05-29)

### 🛠️ 修复

- **Bug E2E-03: `temp_created` undefined in `apply-xliff` MCP tool**: When `images=None` or empty, the `if images:` block is skipped, leaving `temp_created` undefined. Line 233 `if temp_created:` then raises `NameError`.
  - Fix: Initialize `temp_created = False` before the `if images:` block
  - Location: `src/orf/mcp/server.py` line 189

- **Bug #7: images_json 加载失败**: OPP 生成的 `{"images": [...]}` 格式未被正确解析
  - CLI 加载逻辑直接遍历 dict keys 而非数组元素
  - 修复：检测并提取 `images_data["images"]` 数组
  - 影响：`apply-md --images-json` 和 `apply-xliff --images-json` 两个命令

- **Bug #MD-02: pandoc 相对路径图片丢失**: 所有 MD 转换器（md2docx, md2epub, md2html, md2odt, md2rtf, md2pdf）现在传递 `cwd=input_path.parent`
  - pandoc 运行时未指定 cwd 参数，导致 MD 文件中的相对路径图片（如 `![img](images/photo.png)`）无法找到
  - 修复：subprocess.run() 添加 `cwd=str(input_path.parent)` 参数，确保 pandoc 从 MD 文件所在目录解析相对路径

## v0.3.3 (2026-05-28)

### 🛠️ 修复

- **Bug #4: XPath namespace 未注册**: `_backfill_translation` 等方法中调用 `root.xpath("//w:t")` 前现注册 word 命名空间
  - 添加 `WORD_NS_MAP = {"w": W_NS}` 常量
  - 所有 `root.xpath("//w:t")` 和 `root.xpath("//w:p")` 调用现传入 `namespaces=WORD_NS_MAP`
  - 修复 "Undefined namespace prefix 'w'" 异常导致翻译静默失败的问题

- **Bug #5: xml_declaration 与 unicode encoding 不兼容**: `etree.tostring(encoding="unicode", xml_declaration=True)` 导致 ValueError
  - `encoding="unicode"` 返回 Python str，不兼容 XML 声明
  - 改为 `xml_declaration=False`
  - 修复所有 trans-unit 翻译未写入输出的静默失败问题

- **Bug #6: `inject_images` 覆盖翻译结果**: `convert` 和 `apply-xliff` 命令中图片注入后翻译被原始骨架覆盖
  - `inject_images(skeleton_path, images, output_path)` 将 `skeleton_path` 内容重新打包，忽略已翻译内容
  - 修复：先将已翻译输出复制到临时文件，以临时文件为骨架注入图片，再写入最终输出
  - 影响：`convert --images-json` 和 `apply-xliff --images-json` 两个命令

## v0.3.2 (2026-05-28)

### 🛠️ 修复

- **MD 管道图片注入限制**：文档说明 `--images-json` 参数对 MD 管道无效
  - `md2docx.inject_images()` 返回 `([], images)` 空操作
  - MD 管道使用 pre-processing 方案（base64 → temp files → pandoc）
  - 如需精确图片注入，需使用 XLIFF 管道

- **MD 管道结构丢失**：文档说明 Markdown 格式限制
  - MD 是行级格式，不保留 DOCX 段落边界
  - Pandoc 将 `![Image](url)` 当作行内元素处理
  - 原始 DOCX 的 15 个独立图片段落会在 MD→DOCX 后丢失
  - 需要精确结构保留时请使用 XLIFF 管道

### ✨ 新功能

- **MCP `xliff_content` 参数**：新增 `xliff_content: Optional[str]` 参数
  - `apply_xliff` 工具现在接受内联 XLIFF 内容字符串
  - 新增 `--xliff-content` CLI 选项
  - 与 `--xliff` 互斥，同时提供返回错误

### 🔧 通道注入实现

| 通道 | 方法 | 注入方式 |
|------|------|---------|
| `xliff2docx` | `inject_images()` | 按 `paragraph_index` 插入 `<w:drawing>` 到段落 |
| `xliff2pptx` | `inject_images()` | 按 `slide_index` 插入 `<p:pic>` 到幻灯片 |
| `xliff2html` | `inject_images()` | 按 `element_index` 插入 `<img>` 到 DOM |
| `xliff2epub` | `inject_images()` | 按 `spine_index` 插入到章节 XHTML |

## v0.3.0 (2026-05-27)

### ✨ 新功能

- **图片精确定位注入**：ORF 现在支持在 XLIFF 回填时精确注入图片到目标文档
  - OPP 提取图片时附带 `paragraph_index` / `slide_index` / `element_index` / `spine_index` 等位置元数据
  - ORF 通过新增 `--images-json` 参数接收图片注入指令
  - 支持 DOCX、PPTX、HTML、EPUB 四种格式的图片注入

### 🆕 新增 API

- **MCP `apply_xliff` 扩展**：新增 `images` 参数，接收 OPP 传来的图片 placement 数据
- **CLI `--images-json` 参数**：`apply-xliff` 命令新增图片注入支持
- **源格式自动检测**：`detect_from_skeleton()` 从 skeleton.zip 内部结构自动识别源格式

### 📦 新增数据结构

- **`ImagePlacement`** (mcp/schemas.py)：图片注入指令数据结构
  ```python
  {
      data_base64: str,           # base64 编码的图片数据
      mime_type: str,             # image/png, image/jpeg, etc.
      width: Optional[int],
      height: Optional[int],
      # 位置字段 (格式互斥)
      paragraph_index: Optional[int],  # DOCX
      slide_index: Optional[int],      # PPTX
      page_number: Optional[int],       # PDF
      element_index: Optional[int],    # HTML
      spine_index: Optional[int],       # EPUB
  }
  ```

### 🔧 通道注入实现

| 通道 | 方法 | 注入方式 |
|------|------|---------|
| `xliff2docx` | `inject_images()` | 按 `paragraph_index` 插入 `<w:drawing>` 到段落 |
| `xliff2pptx` | `inject_images()` | 按 `slide_index` 插入 `<p:pic>` 到幻灯片 |
| `xliff2html` | `inject_images()` | 按 `element_index` 插入 `<img>` 到 DOM |
| `xliff2epub` | `inject_images()` | 按 `spine_index` 插入到章节 XHTML |

### 🛠️ 修复

- **xliff2docx.py:28-38**: XLIFF namespace 兼容性
  - 新增 `XLIFF_NS_1_1` / `XLIFF_NS_2_0` 常量，支持 OPP 输出的 XLIFF 1.1 namespace
  - 自动检测 root 元素 namespace，先尝试 1.2 → 1.1 → 2.0 降级策略
  - 解决 OPP XLIFF 1.1 与 ORF 硬编码 1.2 导致的解析失败问题
- **cli.py:75**: `apply-md` 命令新增 `--images-json` 参数
  - 支持接收 OPP 图片 placement JSON 文件
  - 加载后调用 converter 的 `inject_images()` 方法注入图片
- **cli.py:267-285**: JSON 输出 metadata 序列化修复
  - 新增 `_safe_json_dumps()` / `_sanitize_for_json()` 过滤不可序列化对象
  - 修复 `result.metadata` 含复杂对象时 `json.dumps` 崩溃问题
- **16 MD converters**: 新增 `inject_images()` stub methods
  - 所有 Pandoc-based MD 转换器（md2docx/md2odt/md2pdf/md2rtf/md2html/md2csv/md2json/md2xlsx/md2xml/md2ipynb/md2eml/md2msg/md2icml/md2srt/md2epub）
  - MD 链路图片由 Pandoc 自动处理（base64 内嵌在 MD 中）
  - stub 返回 `(empty, all_images)` 并记录 WARNING 日志说明不支持段落索引注入
- **md2docx.py:44-114**: MD 图片预处理器（替换 `--embed-media`）
  - 新增 `_find_base64_images()` / `_preprocess_md_images()` 方法
  - 在 Pandoc 调用前将 MD 中的 base64 data URI 图片提取到临时文件
  - 将 MD 中的 `![alt](data:image/...;base64,...)` 改写为 `![alt](temp/images/image_HASH.ext)`
  - 修复 OPP generate_markdown 嵌入 base64 data URI 时 Pandoc 丢失 32% 图片的问题
  - 不依赖 `--embed-media` flag（适用于 pandoc 3.1.x）
- **xliff2docx.py:377-490**: 回填逻辑重构（内联标签支持）
  - `_backfill_translation` 重写：strip inline tag 后匹配 normalized text
  - 新增 `_backfill_with_inline_elements()` / `_backfill_split_runs()` 处理跨多 run 文本
  - 修复 OPP XLIFF 含 `<bx>/<ex>` 内联标签时回填 100% 失败的问题
  - **修复 XPath 无效表达式**：所有 `//{{{W_NS}}}t` 改为 `//w:t`（XPath 1.0 不支持 `{uri}element` 语法）
- **xliff2docx.py:343-356**: `_backfill_translation` 返回修改后的 document_xml
  - 从 `bool` 改为 `str`，每次调用返回最新的 document_xml
  - 解决 `document_xml` 传值而非传引用导致修改无法回写的问题

### 🐛 Orphaned 图片处理

- 无位置信息的图片统一采用 **WARNING 日志 + 末尾追加**策略
- 不再静默丢弃图片

---

## v0.2.2 (2026-05-26)

### 🐛 修复

- **mcp/server.py:20-73**: MCP `apply_md` 工具 JSON-RPC 响应为空
  - 添加空 stdout 检查和 JSON 解析异常处理
  - 修复 `_run_cli_command` 在 CLI 返回空输出时抛出 `JSONDecodeError` 的问题
- **converters/base.py:44-61**: `ConversionResult.errors` 类型不匹配
  - `__post_init__` 自动将字符串错误归一化为 `ErrorDetail` 对象
  - 覆盖全部 22 个文件、77 处字符串错误传参
- **parsers/manifest.py:99-106**: OPP manifest 字段兼容性
  - 添加 `file_path`/`path`、`original_filename`/`name`、`format`/`type` 字段映射
  - 兼容 OPP 不同版本输出的字段名差异
- **cli.py:224**: `click.progressbar` 在 JSON 模式下污染 stdout
  - `--json` 模式下 progressbar 输出到 stderr，保持 stdout 纯净供 JSON 使用

---

## v0.2.1 (2026-05-25)

### 🐛 修复

- **cli.py:219**: 修复转换失败时 crash（AttributeError on string errors）
  - 4 处错误处理代码现在能同时处理 `ErrorDetail` 对象和纯字符串
  - 影响范围：`apply-md` / `apply-xliff` 成功/失败路径的 JSON 输出 + 文本错误输出
- **frontmatter.py:80**: `original_file` 改为 Optional（某些 pipeline 场景不需原始文件名）
- **manifest.py**: `Manifest.version` → `Manifest.manifest_version` 与 OPP JSON key 保持一致

---

## v0.2.0 (2026-05-24)

### Agent-Oriented 架构重构

ORF v0.2.0 从传统 CLI 工具重构为 **AI Agent 原生集成** 的文档转换引擎。

### ✨ 新功能

- **MCP Server** (`orf-mcp-server`)：基于 FastMCP，对外暴露 5 个类型安全工具
  - `apply_md` — MD 转目标格式
  - `apply_xliff` — XLIFF 翻译回填
  - `batch_convert` — 批量转换
  - `detect_format` — 文档格式自动检测
  - `info` — 文档信息查询
- **Pydantic 类型模型**：所有 MCP 工具输入输出由 Pydantic 模型严格验证，解决 38% schema 不匹配问题
- **PathValidator 路径安全**：全局目录遍历防护，`resolve()` + 白名单策略
- **ForemanAgent 作业编排**：按格式自动路由到 Specialist，支持 SIMPLE/MODERATE/COMPLEX 三级复杂度评估
- **4 个 Format Specialist**：FormatSpecialist、DataSpecialist、MarkupSpecialist、EmailSpecialist
- **HITL 人工审批**：大文件/云存储/人工干预场景需审批通过方可执行
- **审计日志**：每条日志携带 correlation_id + agent_id，支持跨系统全链路追踪
- **CLI --json 模式**：所有 4 个命令支持 `--json` 参数输出结构化 JSON 结果
- **ConversionResult 类型增强**：ErrorDetail 增加 code/message/recovery_strategy 字段
- **端到端管线测试**：OPP→OL→ORF 全链路 E2E 测试套件

### 🔧 变更

- pyproject.toml: `version = "0.2.0"`，新增 `[mcp]` optional dependency
- 新增 `orf-mcp-server` 控制台入口点
- 测试覆盖率扩展至 MCP 集成测试 + E2E 管线测试（400+ 用例）

### 🐛 修复

- 无

### 📦 依赖

- 新增可选依赖：`fastmcp>=0.1.0`

---

## v0.1.0 (2026-04-??)

- 初始发布
- MD 回写：DOCX, ODT, EPUB, HTML, RTF, PDF, PPTX, ICML, SRT
- XLIFF 回写：DOCX, PPTX, EPUB, HTML, ODF
- 云存储集成：AWS S3, Azure Blob Storage
- AI 布局检测与溢出修正
- 数据格式：XLSX, CSV, JSON
- 结构化标记：XML
- Jupyter 笔记本：IPYNB
- 邮件格式：EML, MSG

## v0.4.4 (2026-06-23)

### 🛠️ 修复 / Fixed

- **XLIFF→DOCX fuzzy paragraph match (E2E-07)**: `_backfill_split_runs()` now falls back to `difflib.SequenceMatcher` fuzzy matching (ratio ≥ 0.85, length diff ≤ 5) when exact substring match fails. Fixes cases where XLIFF source text doesn't exactly match DOCX paragraph concat text due to editor paragraph splitting.
  - `src/orf/channels/xliff2docx.py`
