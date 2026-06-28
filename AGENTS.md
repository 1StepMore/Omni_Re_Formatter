# AGENTS.md — Omni_Re_Formatter (ORF)

Developer + agent context for the **ORF** sub-repo. The suite-level
[suite-level AGENTS.md](https://github.com/1StepMore/e2e-test-suite/blob/main/AGENTS.md) covers cross-module
orchestration (OPP → OL → ORF); this file is for working **inside**
ORF.

> ORF is the backfill step of the Omni Suite pipeline. OPP extracts
> documents to MD + XLIFF, OL translates them, ORF re-injects the
> translated content into the target format (DOCX, PPTX, EPUB, …).

## Quick start

```bash
# Install
bash scripts/setup_dev.sh

# CLI: convert MD → DOCX
orf apply-md translated.md --target-format docx -o result.docx

# CLI: backfill XLIFF into a source DOCX (requires skeleton.zip)
orf apply-xliff source.docx --xliff translated.xlf --output result.docx

# MCP server (stdio)
orf-mcp-server            # or: python -m orf.mcp.server

# Tests
make test                 # runs uv run pytest tests/ -v
```

## Source layout

```
src/orf/
├── channels/          # Format conversion channels (one per target format)
│   ├── md2docx.py      # MD → DOCX (via pandoc or python-docx fallback)
│   ├── md2pptx.py      # MD → PPTX (via md2pptx CLI)
│   ├── xlsx2md.py, csv2md.py, …  # Reverse direction
│   └── xliff2*.py      # XLIFF backfill channels (need skeleton.zip)
├── converters/         # Base + ConverterOptions dataclass
├── parsers/            # manifest.json, frontmatter, XLIFF parser
├── detection/          # Format auto-detect (magic bytes)
├── resources/          # Image resource manager
├── skeleton/           # skeleton.zip loader/validator
├── cloud/              # S3 + Azure Blob clients
├── ai/                 # AI layout overflow detection
├── mcp/                # MCP server (the Agent-facing surface)
│   ├── server.py       # Standard mcp library, 6 tools
│   ├── security.py     # PathValidator (allowlist + extension whitelist)
│   ├── auth.py         # MCP_SHARED_SECRET shared-secret auth
│   ├── rate_limiter.py  # Per-MCP-tool token bucket
│   ├── metrics.py      # Prometheus metrics
│   ├── health.py       # Health check endpoint
│   └── tracing.py      # OTel tracing
├── agents/             # Foreman + Specialist orchestration
│   ├── foreman.py      # Decomposes jobs, routes to specialists
│   └── specialists/    # One per format family (format, data, markup, email)
├── workflow/           # HITL approval flow
├── logging/            # Structured JSON + console + audit
├── cli.py              # `orf` CLI entry
└── error_handlers/     # Format conversion error recovery
```

## CLI reference

| Command | Purpose |
|---------|---------|
| `orf apply-md <file> --target-format <fmt> -o <out>` | Convert MD to any of 16 formats |
| `orf apply-xliff <source> --xliff <xlf> --output <out>` | Backfill XLIFF into source document |
| `orf convert-batch <dir> --target-format <fmt> --pattern '*.md'` | Batch convert a directory |
| `orf info <file>` | Show document format, size, resource count |
| `orf detect-format <file>` | Format detection only |
| `orf mcp` | Start the MCP server (stdio) |

### Common flags

- `--separate-images` (default true) — extract `![…](…)` refs to a sibling `images.json` instead of inlining
- `--embed-images` — base64-inline images into the output (works for DOCX/HTML/EPUB; not PPTX)
- `--force` (`apply-xliff` only) — bypass skeleton-vs-format validation for cross-format backfill
- `--reference-doc <path>` — pandoc reference template for DOCX output
- `--template <path>` — pandoc template
- `--max-file-size-mb <N>` — reject inputs larger than N MB
- `--json` (CLI only) — emit JSON instead of pretty-printed text

## MCP tools (6 total)

| Tool | Purpose |
|------|---------|
| `apply_md` | Convert MD to target format (16 outputs). Accepts **inline content** via `content` (E2E-76) OR path via `input_md`. |
| `apply_xliff` | Backfill XLIFF into source DOCX/PPTX/EPUB. Requires skeleton.zip alongside the source (from OPP). |
| `batch_convert` | Batch convert a directory |
| `detect_format` | Magic-bytes format detection |
| `info` | Document format + size + resource count + manifest status |
| `ping` | Health check (returns version) |

For full per-tool parameter reference, see the suite-level
[AGENTS.md → MCP Tool Reference](https://github.com/1StepMore/e2e-test-suite/blob/main/AGENTS.md) table,
or [agent-pipeline-guide.md](https://github.com/1StepMore/e2e-test-suite/blob/main/docs/agent-pipeline-guide.md).

## 16 output formats + their conversion engines

| Format | Engine | Notes |
|--------|---------|-------|
| DOCX | pandoc (via `pypandoc-binary`) | Requires `pandoc` binary; auto-installed by `pypandoc-binary`. Use `--reference-doc` for custom styles. |
| ODT | pandoc | Same pandoc stack. |
| EPUB | pandoc | Same. |
| HTML | `markdown` lib (pure Python) | No pandoc needed. |
| RTF | pandoc | Same. |
| PDF | `weasyprint` (`[weasyprint]` extra) | Falls back to pandoc if available. |
| PPTX | **`md2pptx` CLI** (E2E-79) | .NET tool, NOT a pip package. Requires `dotnet tool install --global md2pptx` OR a release binary on PATH. ORF falls back to pandoc automatically if md2pptx is missing and pandoc is available; errors with an actionable install hint only when both are missing. |
| ICML | pandoc | Same. |
| SRT | `srt` lib | Subtitle format. |
| CSV | pandas | Pure Python. |
| XLSX | `openpyxl` (`[office]` extra) | Pure Python. |
| JSON | `json` stdlib | Direct dump. |
| IPYNB | `nbformat` (`[notebook]` extra) | Pure Python. |
| EML | `email` stdlib | Direct construction. |
| MSG | `aspose-email-foss` (`[email-output]` extra, GPLv3) | **Commercial library**; ORF recommends `.eml` instead. |
| XML | `lxml` | Custom XML serialization. |

## Env vars (MCP path)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ORF_MCP_ALLOWED_DIRS` | (none) | Colon/semicolon-separated allowlist of directories the MCP can read. **Required** for tool calls to succeed. |
| `ORF_MCP_MAX_FILE_SIZE` | `104857600` (100 MB) | Max input file size in bytes. |
| `ORF_MCP_TIMEOUT` | `30` | Per-tool request timeout (seconds). |
| `OMNI_METRICS_DIR` | `/tmp/omni-metrics` | Prometheus metrics directory. |
| `OMNI_HEALTH_HOST` | `127.0.0.1` | Health-check endpoint bind host. |
| `OMNI_HEALTH_PORT` | `8766` | Health-check endpoint bind port. |
| `OMNI_HEALTH_ENABLED` | `false` | Set to `1`/`true`/`yes`/`on` to enable the health HTTP endpoint. |
| `OMNI_RATE_LIMIT_RPM` | `60` | Per-MCP-tool token-bucket rate limit (0 = disabled). |
| `OMNI_RATE_LIMIT_BURST` | `10` | Token-bucket burst size. |
| `OMNI_TRACING_ENABLED` | `false` | Set to `1`/`true`/`yes`/`on` to enable OTel tracing. |
| `OMNI_TRACES_DIR` | `/tmp/omni-traces` | OTel trace output directory. |
| `ORF_LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `OMNI_CACHE_DIR` | `~/.omni_cache` | Cache root directory for transient artifacts. |
| `OMNI_LOG_FORMAT` | `console` | Log format: `console` or `json`. |
| `MCP_SHARED_SECRET` | (none — auth disabled) | Shared-secret auth for MCP requests. |
| `OMNI_TEST_FAKE_LLM=1` | unset | Mock LLM responses (only affects tests). |
| `OMNI_TEST_FAKE_PANDOC=1` | unset | Bypass pandoc subprocess (use `markdown` lib instead). |

## Path Configuration (MCP Server)

The ORF MCP server uses `ORF_MCP_ALLOWED_DIRS` to restrict file
system access during format conversion.

```bash
export ORF_MCP_ALLOWED_DIRS="/path/to/docs:/path/to/output"
```

**Fallback behavior:** If unset, ORF defaults to `[Path.cwd()]`
(the current working directory). For production use, always set
this explicitly to avoid unintended directory access.

**Note:** The env var name differs from OPP (`OPP_MCP_ALLOWED_DIRS`).
See [agent-pipeline-guide.md](https://github.com/1StepMore/e2e-test-suite/blob/main/docs/agent-pipeline-guide.md) for a cross-server comparison.

## PathValidator security model

The `orf.mcp.security.PathValidator` (in `src/orf/mcp/security.py`)
is the **single source of truth** for which paths the MCP can touch.
It checks:

1. Path is within the configured `allowed_directories`
2. Path is not in `BLOCKED_EXTENSIONS` (executables: `.exe .bat .sh .ps1 .vbs .js`)
3. Path is in `ALLOWED_EXTENSIONS` (E2E-80: csv, tsv, xlsx, json, ipynb, eml, msg, srt, icml, rtf, pdf added in 0.4.5)
4. Path is not a symlink pointing outside the allowlist
5. File size ≤ `max_file_size_bytes`

**To add a new file format**: edit `PathValidator.ALLOWED_EXTENSIONS` in
`src/orf/mcp/security.py:62` AND add it to the README env var table.

## XLIFF backfill + skeleton.zip

The `apply-xliff` and `apply_xliff` channels need a `skeleton.zip`
alongside the source document. The skeleton preserves the original
DOCX/PPTX/EPUB ZIP structure so ORF can re-inject translated text
without re-rendering styles/media.

**skeleton.zip is produced by OPP** at the same path as the XLIFF.
It contains:
- `word/document.xml` (DOCX) or `ppt/slides/*.xml` (PPTX) — preserved
- `[Content_Types].xml` — preserved
- `word/styles.xml`, `word/numbering.xml` — preserved
- `word/media/*` — preserved (E2E-07 fuzzy match by cx/cy to avoid double-insert)

**Cross-format XLIFF backfill** (e.g., DOCX XLIFF → PPTX): requires
`--force`. ORF warns + proceeds.

## Foreman + Specialist orchestration

ORF includes a full agent orchestration system (separate from the
MCP server):

- `src/orf/agents/foreman.py` — `ForemanAgent` decomposes a job into
  sub-tasks and routes to specialists based on document type.
- `src/orf/agents/specialists/` — one specialist per format family:
  - `format_specialist.py` (DOCX/ODT/EPUB/PPTX)
  - `data_specialist.py` (XLSX/CSV/JSON)
  - `markup_specialist.py` (XML/HTML)
  - `email_specialist.py` (EML/MSG)
- `src/orf/workflow/hitl_approval.py` — HITL (Human-in-the-Loop) approval
  for high-risk operations: files >100MB, S3/Azure uploads, error
  recovery strategy `MANUAL_INTERVENTION`.

To run a Foreman-driven batch:
```bash
python -c "from orf.agents.foreman import ForemanAgent; \
           ForemanAgent().run('docs/', target_format='docx')"
```

## Tests

```bash
make test                            # all tests via uv run pytest tests/ -v
pytest tests/test_orf_mcp_server.py  # MCP server only
pytest tests/ -m unit                # unit tests only
pytest tests/ -m integration         # integration tests
pytest tests/ -m e2e                 # E2E tests (need real pandoc + network)
```

Test markers: `unit`, `integration`, `e2e` (registered in
`pyproject.toml`). Coverage target ≥90%.

Key test files:
- `tests/test_orf_mcp_server.py` — MCP tool-level integration
- `tests/test_apply_md_target_format.py` — apply-md per-format tests
- `tests/test_mcp_apply_md_xliff_parity.py` — CLI/MCP parity
- `tests/turnkey/test_image_fidelity.py` — XLIFF image dedup
  regression (E2E-07)

## Known issues / gotchas

- **E2E-07**: XLIFF→DOCX inline image dedup. The cx/cy match is
  document-wide (not paragraph-local) because OPP's
  `paragraph_index` is off-by-one vs ORF's `//w:p` enumeration.
- **E2E-79**: `md2pptx` is a .NET tool, not a pip package. Install
  with `dotnet tool install --global md2pptx` or download a release
  binary. ORF pre-flight checks for the binary and returns an
  actionable install hint if missing.
- **MSG output**: requires commercial `aspose-email-foss` (GPLv3
  fork). ORF recommends `.eml` for open-source compatibility.
- **PDF XLIFF input** is intentionally **not** supported by OPP
  (PDF→XLIFF raises). Use MD path.
- **Cross-format XLIFF** (e.g., DOCX→XLIFF→PPTX): requires
  `--force`. ORF warns + proceeds.
- **HITL**: ForemanAgent operations on files >100MB, cloud uploads,
  and `MANUAL_INTERVENTION` recovery strategies all require human
  approval before proceeding.

### apply-md vs apply-xliff

ORF has two distinct backfill channels. Choosing the wrong one is the
most common agent mistake.

| Aspect | `apply-md` | `apply-xliff` |
|--------|-----------|--------------|
| Input | `.md` file | Source document + `.xlf` + `skeleton.zip` |
| Output formats | 16 (see table above) | Same format as source |
| Engine | pandoc / pure Python | skeleton.zip reinjection |
| Layout | Rendered by pandoc styles | Original layout preserved |
| Cross-format | Native (MD → DOCX, MD → EPUB, etc.) | Requires `--force` (warns) |
| Images | `--separate-images` (default) or `--embed-images` | Auto-reinjected from skeleton |

**Decision flow:**

1. Do you have a skeleton.zip from OPP? → **`apply-xliff`**
2. Do you need to convert to a different format than the source? → **`apply-md`**
3. Do you need exact original layout (fonts, styles, floating images)? → **`apply-xliff`**
4. Otherwise → **`apply-md`** (simpler, more output options)

**Cross-format XLIFF** (e.g. DOCX XLIFF → PPTX): add `--force`. ORF
will warn that formats don't match but proceed.

**Full pipeline comparison**: See the suite-level
[Pipeline Selection Strategy](https://github.com/1StepMore/e2e-test-suite/blob/main/README.md#pipeline-selection-strategy)
for the complete decision tree and format support matrix.

## Pointers to the suite-level docs

- Cross-module orchestration: [AGENTS.md](https://github.com/1StepMore/e2e-test-suite/blob/main/AGENTS.md)
- MCP tool full parameter reference: [agent-pipeline-guide.md](https://github.com/1StepMore/e2e-test-suite/blob/main/docs/agent-pipeline-guide.md)
- Pre-commit hooks: [.pre-commit-config.yaml](https://github.com/1StepMore/e2e-test-suite/blob/main/.pre-commit-config.yaml)
- Compatibility matrix: [COMPATIBILITY.md](https://github.com/1StepMore/e2e-test-suite/blob/main/COMPATIBILITY.md)
