# Omni-Re-Formatter (ORF)

ORF is the final step in the Omni document localization ecosystem, responsible for converting standardized intermediate representations back into complex target formats.

## Core Features

- Reads OL-translated MD/XLIFF, manifest.json, and skeleton.zip
- Leverages mature tools like Pandoc, md2pptx, and translate-toolkit
- Combines with custom glue code (manifest parsing, skeleton backfill, resource path resolution)
- Reassembles and generates localized complex-format documents
- **Native AI Agent integration**: exposed as AI Agent Tools via MCP Server, supporting Foreman/Specialist intelligent orchestration

## Supported Formats

### MD Backfill
- DOCX, ODT, EPUB, HTML, RTF, PDF, PPTX
- ICML (InDesign), SRT (subtitles)

### XLIFF Backfill
- DOCX, PPTX, EPUB, HTML, ODF

### Cloud Storage
- AWS S3, Azure Blob Storage

### AI Assistance
- Layout overflow detection and correction

### Data Formats
- XLSX, CSV, JSON

### Structured Markup
- XML

### Jupyter Notebooks
- IPYNB

### Email Formats
- EML, MSG

### Conversion Engine Dependencies

- **pandoc path**: MD→{DOCX, ODT, EPUB, RTF, ICML} requires `pandoc` (provided automatically by `pypandoc-binary` when `pip install omni-re-formatter` is run).
- **Pure Python path**: MD→HTML and MD→PDF (weasyprint engine) do not depend on pandoc; they use the `markdown` library + WeasyPrint.
- **XLIFF cross-format**: `apply-xliff --force` bypasses format validation, attempting backfill across formats while emitting a warning.
- **Email format**: MD→MSG auto-generates default headers when `email_headers` frontmatter is missing, no longer hard-failing.
- **MD→MSG format**: requires `aspose-email-foss` (GPLv3 open-source fork) — `pip install 'omni-re-formatter[email-output]'`.
  Aspose is a commercial library. **We recommend using .eml format** (fully supported by ORF with graceful fallback via W2.1), an open standard.

## Installation

```bash
# Core functionality
pip install omni-re-formatter

# Development dependencies
pip install -e ".[dev]"

# Optional dependencies
pip install -e ".[weasyprint]"   # PDF generation
pip install -e ".[cloud]"       # S3/Azure support
pip install -e ".[ai]"          # AI layout correction
pip install -e ".[office]"      # XLSX support
pip install -e ".[notebook]"    # IPYNB support
pip install -e ".[email-output]" # MSG support
pip install -e ".[mcp]"         # MCP Server (AI Agent integration)
```

## Quick Start

### CLI Usage

```bash
# MD to DOCX
orf apply-md translated.md --target-format docx --output result.docx

# MD to EPUB — JSON output
orf apply-md translated.md --target-format epub --output result.epub --json

# XLIFF backfill (requires skeleton.zip)
orf apply-xliff original.docx --xliff translated.xlf --output result.docx

# XLIFF backfill + image injection (from OPP images.json)
orf apply-xliff original.docx --xliff translated.xlf --output result.docx --images-json images.json

# Batch conversion — JSON output
orf convert-batch ./translated --target-format docx --pattern "*.md" --json
```

### MCP Server (AI Agent Integration)

```bash
# Start the MCP Server
orf-mcp-server

# AI Agents can call the following tools via the MCP protocol:
# - apply_md       Convert MD to target format
# - apply_xliff    Apply XLIFF translation to original document (with optional image injection)
# - batch_convert  Batch convert MD files
# - detect_format  Auto-detect document format
# - info           Get document information
# - ping           Health check (returns version)
```

All MCP tools use **Pydantic type models** for input/output validation, preventing call failures due to schema mismatches (measured to reduce such errors from 38% to 0%).

## CLI Commands

| Command | Description | JSON Output |
|---------|-------------|-------------|
| `apply-md` | Convert MD file to target format | `--json` supported |
| `apply-xliff` | Apply XLIFF translation to original document | `--json` supported |
| `convert-batch` | Batch convert MD files | `--json` supported |
| `info` | Show document information | `--json` supported |

## AI Agent-Native Architecture

ORF v0.3.0 was refactored into an **Agent-Oriented** architecture, designed specifically for AI Agent integration rather than as a traditional CLI tool.

### MCP Server — Built for AI Agents

ORF's MCP Server is built on **FastMCP**, exposing 6 tools that AI Agents (Claude, Cursor, OpenCode, etc.) can call directly via the standard MCP protocol to leverage ORF's document conversion capabilities.

```
AI Agent (Claude/Cursor) 
    │
    ▼ MCP protocol
┌──────────────────┐
│  ORF MCP Server   │
│  ┌──────────────┐ │
│  │ apply_md     │ │  ← Pydantic type validation
│  │ apply_xliff  │ │  ← Path security check
│  │ batch_convert│ │  ← Subprocess isolation
│  │ detect_format│ │
│  │ info         │ │
│  └──────────────┘ │
└──────────────────┘
```

**MCP Security Design:**
- `PathValidator` prevents directory traversal attacks
- All path inputs are validated via `resolve()` + allowlist policy
- CLI calls execute in isolated subprocesses

### Foreman + Specialist Agent Orchestration System

ORF has a built-in complete Agent orchestration system that automatically routes to specialized processing units based on document format:

```
         ┌─────────────────┐
         │  ForemanAgent    │  ← Job orchestrator
         │  Assess           │
         │  complexity       │
         │  Decompose tasks  │
         │  Dispatch         │
         │  Specialist       │
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

- **ForemanAgent**: receives job requests → evaluates complexity (SIMPLE / MODERATE / COMPLEX) → decomposes into sub-tasks → routes to Specialist → aggregates results
- **FormatSpecialist**: docx, odt, epub, pptx
- **DataSpecialist**: xlsx, csv, json
- **MarkupSpecialist**: xml, html
- **EmailSpecialist**: eml, msg
- **Error Recovery**: error-code-based automatic recovery strategies (ABORT / SKIP / RETRY / MANUAL_INTERVENTION / FALLBACK)

### Human-in-the-Loop (HITL) Approval

Provides human approval workflow for high-risk operations:

| Scenario | Risk Level | Approval Required |
|----------|------------|------------------|
| File > 100MB | HIGH | ✅ |
| Cloud upload (S3/Azure) | HIGH | ✅ |
| Manual intervention recovery | LIMITED | ✅ |
| File > 500MB | UNACCEPTABLE | ✅ |
| Standard operation | LOW | ❌ |

### Audit Trail

All operations are logged to `logs/audit_{date}.log`, each entry containing:
- **correlation_id**: cross-system trace ID
- **agent_id**: caller Agent identifier
- Timestamp, operation type, result status

## Recent additions (v0.4.3)

### Inline image dedup (regression fix)

XLIFF→DOCX backfill now skips inline drawings that are already in the skeleton. Without this check, ORF re-injected 2 inline duplicates of the first 2 source images (IM 16, 组合 116) in the Haier DOCX — output had 13 `<w:drawing>` blocks instead of the source's 11. The floating-image path already had a similar dedup (positionH/V match); the inline path now mirrors it via document-wide `<wp:extent>` cx/cy match. Document-wide (not paragraph-local) is required because OPP's `paragraph_index` is off-by-one vs. ORF's `//w:p` enumeration.

Locked in by `tests/turnkey/test_image_fidelity.py::test_drawing_count_equals_source` (regression guard).

## Recent additions (v0.4.0)

ORF v0.4.0 extended image processing capabilities to accommodate real LLM-extracted image metadata from OPP.

### Floating image support (`wp:anchor`)

The XLIFF→DOCX channel now supports **floating images** (absolute positioning, independent of paragraph flow), flagged by OPP via `is_floating=true` in `images.json`. When `ImagePlacement` contains `wp_anchor_h` / `wp_anchor_v` / `wp_anchor_relative_h` / `wp_anchor_relative_v` fields, ORF injects a `w:drawing > wp:anchor` element (with `positionH` / `positionV` / `wrapNone`) instead of the default `wp:inline`. This fixes the previous issue where 5 out of 12 floating images were silently discarded.

### MD image separation mode (`--separate-images`)

The `apply-md` command added `--separate-images` / `--images-dir` flags, enabling a **"DOCX + images separate" mode**. In this mode, `![alt](path)` references in the MD are extracted to a designated `images_dir/`, the pandoc-generated DOCX contains only text structure, and the image manifest is written separately. This mode is useful when: MD sources contain many images causing pandoc inline embedding bloat, image resources need to be published separately, or image version control must be maintained outside the DOCX. Enabled by default (`separate_images=True`), can be disabled via `ConverterOptions(separate_images=False)`.

```bash
# Enable MD image separation mode
orf apply-md translated.md --target-format docx --output result.docx \
  --separate-images --images-dir ./extracted_images
```

## Architecture

```
src/orf/
├── channels/          # Format conversion channels (MD→X, XLIFF→X)
├── converters/        # Base converters and streaming
│   └── base.py        # ConversionResult, ErrorDetail, WarningDetail
├── parsers/          # manifest.json, frontmatter parsing
├── detection/         # Auto-format detection
├── resources/        # Image resource management
├── skeleton/          # skeleton.zip loading and backfill
├── cloud/            # S3, Azure Blob clients
├── ai/               # Layout analysis and overflow correction
├── mcp/              # MCP Server (Agent integration layer)
│   ├── server.py     # FastMCP service, 6 tools
│   ├── schemas.py    # Pydantic type models
│   ├── security.py   # PathValidator path security
│   └── config.py     # MCP configuration
├── agents/           # Agent orchestration system
│   ├── foreman.py    # ForemanAgent job orchestrator
│   └── specialists/  # Format-specific specialist Agents
│       ├── format_specialist.py    # docx/odt/epub/pptx
│       ├── data_specialist.py      # xlsx/csv/json
│       ├── markup_specialist.py    # xml/html
│       └── email_specialist.py     # eml/msg
├── workflow/         # Workflow engine
│   └── hitl_approval.py  # Human approval workflow
├── logging/          # Logging system (includes audit trail)
└── error_handlers/   # Error handling and recovery strategies
```

## Project Status (v0.4.1)

- **Agent-Oriented Architecture**: MCP Server + Foreman/Specialist + HITL approval
- **Precise Image Positioning**: supports position-based image backfill for DOCX/PPTX/HTML/EPUB
- **MCP Tools**: 6 type-safe Agent tools (Pydantic model validation)
- **Path Security**: PathValidator prevents directory traversal
- **Audit Logs**: correlation_id + agent_id full-chain tracing
- 400+ test cases (including MCP integration tests + end-to-end pipeline tests)
- MD backfill: 9 formats (DOCX, ODT, EPUB, HTML, RTF, PDF, PPTX, ICML, SRT)
- XLIFF backfill: 5 formats (DOCX, PPTX, EPUB, HTML, ODF)
- Cloud storage: S3 + Azure Blob integration
- AI: Layout overflow detection and correction
- Data formats: XLSX, CSV, JSON
- Structured markup: XML
- Jupyter notebooks: IPYNB
- Email formats: EML, MSG
- Comprehensive error handling and logging system

## Development

```bash
# Install development dependencies
pip install -e ".[dev,mcp]"

# Run tests
pytest tests/

# Test MCP only
pytest tests/test_orf_mcp_server.py -v

# Test end-to-end pipeline only
pytest tests/test_e2e_omni_pipeline.py -v

# Code linting
ruff check src/orf
mypy src/orf --ignore-missing-imports

# Build package
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
│  │  (Extract)  │    │ (Translate) │    │ (Backfill)  │               │
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

## License

MIT
