# ORF MCP security + configuration (moved from AGENTS.md, #73)

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
| `ORF_AUTOLOAD_DOTENV` | unset | Set to `1` to auto-load `.env` file (same as `--load-dotenv` CLI flag). |

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

## Known issues / gotchas

- **E2E-07**: XLIFF→DOCX inline image dedup. The cx/cy match is
  document-wide (not paragraph-local) because OPP's
  `paragraph_index` is off-by-one vs ORF's `//w:p` enumeration.
- **E2E-79**: `md2pptx` is a **Python 3 script**
  ([MartinPacker/md2pptx](https://github.com/MartinPacker/md2pptx)), not a
  pip package and not a prebuilt release binary. Clone the repo and
  `chmod +x md2pptx`; keep the directory beside its helper modules
  (`funnel.py`, `paragraph.py`, …). Requires Python 3 + `python-pptx>=1.0`
  (`pip install 'python-pptx>=1.0'`); inline math also needs
  `latex2mathml` + `lxml`. ORF pre-flight checks for the script on PATH,
  runs it as `md2pptx INPUT.md OUTPUT.pptx`, and returns an actionable
  install hint if missing (with pandoc as the automatic fallback).
  The suite helper `scripts/install_md2pptx.sh` (in the e2e-test-suite repo)
  automates the clone + link and proves the install with a real conversion.
- **MSG output**: requires commercial `aspose-email-foss` (GPLv3
  fork). ORF recommends `.eml` for open-source compatibility.
- **PDF XLIFF input** is intentionally **not** supported by OPP
  (PDF→XLIFF raises). Use MD path.
- **Cross-format XLIFF** (e.g., DOCX→XLIFF→PPTX): **not implemented.**
  ORF refuses; `--force` is inert. Use the MD path.
- **`ImageManager` is library-only**: `src/orf/resources/image_manager.py`
  provides thread-safe MD5 dedup for direct callers that materialize
  images; the backfill channels rely on pandoc/format-native embedding
  and do not route through it. Tests and external callers use it directly.
- **HITL**: ForemanAgent operations on files >100MB, cloud uploads,
  and `MANUAL_INTERVENTION` recovery strategies all require human
  approval before proceeding.
