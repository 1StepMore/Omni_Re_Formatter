# ORF API Reference

This document is the canonical reference for the **Omni-Re-Formatter (ORF)** public
interface. ORF exposes two equivalent surfaces:

1. A **CLI** (`orf`) for direct shell use and shell scripting.
2. An **MCP server** (`orf-mcp-server` or `python -m orf.mcp.server`) for AI agents
   (Claude Desktop, Cursor, OpenCode, etc.).

Both surfaces share the same underlying conversion logic — the MCP server is a thin
transport/auth wrapper that delegates to the CLI via `python -m orf.cli …` subprocess
(`src/orf/mcp/server.py:79`).

For install instructions, see the [README](../README.md). For worked examples, see
[TUTORIAL.md](TUTORIAL.md). For error recovery, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
For internals, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. CLI Reference

The `orf` command is a Click group. Every subcommand accepts the global `--verbose / -v`
flag and a global `--version` flag (added in v0.4.3 packaging fix; `src/orf/cli.py:198`).

```bash
orf --version          # omni-re-formatter, version 0.4.17
orf --help             # list subcommands
orf <subcommand> --help
```

### 1.1 Common flags

| Flag | Applies to | Meaning |
|------|------------|---------|
| `--verbose`, `-v` | all | Enable `DEBUG` logging |
| `--json` | most output commands | Emit a single JSON document to stdout (instead of human prose) |
| `--max-file-size-mb N` | `apply-md`, `apply-xliff`, `convert-batch` | Refuse files larger than N MB (exits with code 1) |
| `--no-cache` | `apply-md`, `apply-xliff` | Skip ORF's content-addressed cache (`~/.omni_cache/orf/`) |
| `--clear-cache` | `apply-md`, `apply-xliff` | Remove every entry from the ORF cache, then exit |

The cache key for `apply-md` is `sha256(input ‖ target_format ‖ manifest ‖ template ‖ images_json)`
(`src/orf/cli.py:70`); the cache key for `apply-xliff` is
`sha256(input ‖ xliff ‖ format ‖ images_json)` (`src/orf/cli.py:100`).
The cache root can be relocated with the `OMNI_CACHE_DIR` environment variable.

### 1.2 `orf apply-md` — Convert MD to a target format

Converts a markdown file to one of 16 target formats. Implementation: `src/orf/cli.py:233`.

```
orf apply-md INPUT_MD [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `INPUT_MD` (positional) | — | Path to the input markdown file (must exist) |
| `--target-format`, `-t` | `docx` | One of: `auto`, `docx`, `odt`, `epub`, `html`, `rtf`, `pdf`, `pptx`, `csv`, `json`, `xlsx`, `xml`, `ipynb`, `eml`, `msg`, `icml`, `srt` |
| `--auto-detect` | off | Detect the original format via `FormatDetector` and translate it to the matching `--target-format` |
| `--output`, `-o` | `<input_stem>.<target_format>` | Output file path |
| `--template` | none | Pandoc template path |
| `--reference-doc` | none | Reference DOCX (mapped to pandoc `--reference-doc`) |
| `--title` | none | EPUB title metadata |
| `--author` | none | EPUB author metadata |
| `--lang` | `zh` | EPUB language code |
| `--embed-images` | off | Embed images into the EPUB |
| `--text-only` | off | Strip all images and placeholders, emit text only |
| `--separate-images` / `--no-separate-images` | on | Extract images to sidecar `images.zip` + `images.json`; disable to inline via base64 |
| `--images-json PATH` | none | Load image placements from this JSON (typically the file OPP produced) |
| `--json` | off | Emit a JSON envelope with `success`, `output_path`, `errors[]`, `warnings[]`, `metadata` |

**Format routing.** `apply-md` imports the matching channel class from
`src/orf/channels/md2*.py`. Each channel is responsible for calling pandoc,
`md2pptx`, `weasyprint`, `openpyxl`, `aspose-email-foss`, etc., depending on the
target format.

**Image sidecar mode.** When `--separate-images` is on (the default since v0.4.0),
pandoc's DOCX no longer carries embedded base64 images; instead ORF writes
`<output_stem>_images/` plus an `images.json` manifest next to the output file.
Pass `--no-separate-images` to keep images embedded.

**Example.**

```bash
orf apply-md translated.md --target-format docx --output result.docx
orf apply-md translated.md --target-format epub --output book.epub --json
orf apply-md translated.md --target-format docx --reference-doc brand-template.docx
```

### 1.3 `orf apply-xliff` — Backfill translated XLIFF to the original document

Replaces the source text inside an original document (DOCX, PPTX, EPUB, HTML, ODT)
with the target text in a translated XLIFF file. Implementation: `src/orf/cli.py:687`.

```
orf apply-xliff INPUT_FILE [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `INPUT_FILE` (positional) | — | Path to the original document **or** an XLIFF (`.xlf`/`.xliff`) **or** a skeleton ZIP |
| `--xliff`, `-x` | required | Path to the translated XLIFF file |
| `--xliff-content` | none | Inline XLIFF body (mutually exclusive with `--xliff`) |
| `--output`, `-o` | required | Output file path |
| `--format`, `-f` | `docx` | One of: `docx`, `pptx`, `epub`, `html`, `odt` |
| `--images-json` | none | Path to an `images.json` produced by OPP (enables image injection) |
| `--force` | off | Bypass skeleton-vs-format validation for cross-format conversion (output may be broken) |
| `--no-cache` | off | Skip the ORF content-addressed cache |
| `--clear-cache` | off | Remove every ORF cache entry and exit |
| `--max-file-size-mb` | none | Refuse files larger than N MB |
| `--json` | off | Emit a JSON envelope |

**Format validation.** By default, the extension of `INPUT_FILE` must match `--format`
(`.docx`/`.pptx`/`.epub`/`.html`/`.odt`) or be `.xlf`/`.xliff`/`.zip` for the ZIP-based
formats (DOCX/PPTX/EPUB). Use `--force` to bypass — this is required for cross-format
backfill (e.g. DOCX skeleton → PPTX output) and emits a `WARNING` instead of an error
(`src/orf/cli.py:776`).

**Image injection.** When `--images-json` is provided and the format is DOCX, ORF
calls `converter.inject_images(...)` to re-insert images at the original paragraph
index. Inline duplicates are deduped document-wide via
`<wp:extent>` cx/cy match (`src/orf/channels/xliff2docx.py`, see CHANGELOG v0.4.3).

**Example.**

```bash
# Same-format backfill
orf apply-xliff original.docx --xliff translated.xlf --output result.docx

# Backfill with image injection
orf apply-xliff original.docx --xliff translated.xlf \
  --output result.docx --images-json images.json

# Cross-format (DOCX skeleton → PPTX output) — requires --force
orf apply-xliff original.docx --xliff translated.xlf \
  --output result.pptx --format pptx --force
```

### 1.4 `orf convert-batch` — Convert many MD files in parallel

Recursively scans a directory for MD files with frontmatter and converts each one.
Implementation: `src/orf/cli.py:588`.

```
orf convert-batch INPUT_DIR [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `INPUT_DIR` (positional) | — | Directory to scan (must exist) |
| `--target-format`, `-t` | required | One of: `docx`, `odt`, `epub`, `html` |
| `--output-dir`, `-o` | `INPUT_DIR` | Where to write `<stem>.<format>` files |
| `--pattern`, `-p` | `*.md` | Glob pattern for input files |
| `--max-workers` | `4` | Parallel conversion worker count (set to 1 for serial mode) |
| `--max-file-size-mb` | none | Refuse files larger than N MB |
| `--json` | off | Emit `{"success_count": N, "fail_count": M, "total": K}` |

Only files that have YAML frontmatter (`has_frontmatter(...)`) are included — this is
the heuristic that marks a file as "translated by OL".

**Example.**

```bash
orf convert-batch ./translated --target-format docx --pattern "*.md"
orf convert-batch ./translated -t html -o ./html_out --max-workers 8 --json
```

### 1.5 `orf info` — Inspect a document

Detects the document format via magic bytes, reports file size, manifest status, and
image resource count if a `manifest.json` is found next to the file. Implementation:
`src/orf/cli.py:890`.

```
orf info INPUT_FILE [--json]
```

**Example.**

```bash
$ orf info document.docx
Format: DOCX
Size: 2.41 MB
Resources: 8 images
Manifest: present

$ orf info document.docx --json
{
  "format": "DOCX",
  "size_mb": 2.41,
  "resource_count": 8,
  "manifest_status": "present"
}
```

### 1.6 Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | File-size limit exceeded, or no MD files found in batch mode |
| 2 | `click.BadParameter` (e.g., skeleton extension mismatch without `--force`, mutually exclusive flags) |
| Other non-zero | Conversion failed — see `result.errors[]` in the JSON envelope |

---

## 2. MCP Server Reference

The MCP server speaks the standard [Model Context Protocol](https://modelcontextprotocol.io)
over stdio (mcp 1.27.2). It is reachable as:

- **Module form**: `python -m orf.mcp.server`
- **Installed entry point**: `orf-mcp-server`
- **In-process alias**: import from `orf.mcp.server` and call `apply_md(...)`, etc.

The six tools are defined in `src/orf/mcp/server.py:528` and listed by
`@server.list_tools()` at `src/orf/mcp/server.py:538`.

### 2.1 Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `ORF_MCP_ALLOWED_DIRS` | `cwd` | Comma-separated list of directories the server is allowed to read/write. Set this **before** importing the server; the config is loaded at module import time (`src/orf/mcp/server.py:69`). |
| `MCP_SHARED_SECRET` | unset | If set, every tool call must pass `auth_token=<value>` (shared-secret auth, Phase A4). |
| `OMNI_TEST_FAKE_PANDOC` | unset | Stripped from the CLI subprocess env by the MCP server (test-only seam) |
| `OMNI_TEST_FAKE_LLM` | unset | Stripped from the CLI subprocess env by the MCP server |

### 2.2 Tools

Every tool returns a JSON string wrapped in a `TextContent` block. Standard fields:

- `success` (bool) — `true` on success
- `output_path` (str | null) — the produced file
- `errors` (array) — `[{code, message, recovery_strategy?}]`
- `warnings` (array) — `[{code, message}]`
- `metadata` (object) — engine-specific context

#### 2.2.1 `apply_md`

Convert markdown to any of 16 target formats. Delegates to the `orf apply-md` CLI.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `input_md` | string | yes | — | Path to input markdown |
| `target_format` | string | yes | — | One of: `docx`, `odt`, `epub`, `html`, `rtf`, `pdf`, `pptx`, `icml`, `srt`, `csv`, `xlsx`, `xml`, `ipynb`, `eml`, `msg`, `json` |
| `output_path` | string | no | auto | Where to write the result |
| `images` | array<object> | no | `null` | OPP-style image placements (with `data_base64`) |
| `separate_images` | bool | no | `true` | Extract images to sidecar (default) or inline via base64 |
| `reference_doc` | string | no | `null` | Pandoc reference DOCX |
| `template` | string | no | `null` | Pandoc template path |
| `title`, `author`, `lang` | string | no | — | EPUB metadata |
| `embed_images` | bool | no | `false` | Embed images into the EPUB |
| `text_only` | bool | no | `false` | Drop all image references |
| `max_file_size_mb` | number | no | `null` | Per-file size limit |

Schema source: `src/orf/mcp/server.py:551`.

#### 2.2.2 `apply_xliff`

Backfill translated XLIFF into the original document. Delegates to `orf apply-xliff`.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `input_file` | string | yes | — | Path to source document, XLIFF, or skeleton ZIP |
| `xliff_path` | string | conditional | — | Path to translated XLIFF (mutually exclusive with `xliff_content`) |
| `xliff_content` | string | conditional | — | Inline XLIFF body (mutually exclusive with `xliff_path`) |
| `output_path` | string | yes | — | Where to write the backfilled document |
| `format` | string | yes | — | One of: `docx`, `pptx`, `epub`, `html`, `odt` |
| `images` | array<object> | no | `null` | Each entry must carry `data_base64`; `file_path` is rejected (C4 fix) |
| `force` | bool | no | `false` | Bypass skeleton-vs-format validation |
| `no_cache` | bool | no | `false` | Skip the content-addressed cache |
| `max_file_size_mb` | number | no | `null` | Per-file size limit |

Schema source: `src/orf/mcp/server.py:604`.

#### 2.2.3 `batch_convert`

Convert every frontmatter-bearing MD file in a directory. Delegates to `orf convert-batch`.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `input_dir` | string | yes | — | Directory to scan |
| `target_format` | string | yes | — | One of: `docx`, `odt`, `epub`, `html` |
| `pattern` | string | no | `*.md` | Glob pattern |

Returns: `{"success_count": N, "fail_count": M, "total": K}`.
Schema source: `src/orf/mcp/server.py:656`.

#### 2.2.4 `detect_format`

Magic-bytes format detection. Delegates to `orf info`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | yes | Document to inspect |

Returns: `{"format": "DOCX", "confidence": 1.0}`. Schema source: `src/orf/mcp/server.py:678`.

#### 2.2.5 `info`

Document information. Delegates to `orf info`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | yes | Document to inspect |

Returns: `{"format": "...", "size_mb": N, "resource_count": N, "manifest_status": "..."}`.
Schema source: `src/orf/mcp/server.py:692`.

#### 2.2.6 `ping`

Health check. Returns: `{"success": true, "module": "orf", "version": "0.4.17"}`.
Schema source: `src/orf/mcp/server.py:706`.

### 2.3 Error codes

The MCP server uses the following error codes (`recovery_strategy` is the ORF
error-recovery enum value, e.g. `ABORT`, `SKIP`, `RETRY`, `FALLBACK`,
`MANUAL_INTERVENTION`):

| Code | Meaning |
|------|---------|
| `PATH_NOT_ALLOWED` | Path is outside `ORF_MCP_ALLOWED_DIRS` |
| `FILE_PATH_NOT_ALLOWED` | `apply_xliff.images[i].file_path` was supplied (use `data_base64` instead) |
| `MUTUALLY_EXCLUSIVE` | Both `xliff_path` and `xliff_content` were provided |
| `EMPTY_OUTPUT` | The CLI subprocess returned no stdout |
| `JSON_PARSE_ERROR` | The CLI subprocess emitted non-JSON output |
| `CLI_ERROR` | The CLI subprocess exited non-zero without a structured error |
| `AUTH_FAILED` | `MCP_SHARED_SECRET` did not match (when configured) |
| `RATE_LIMIT_EXCEEDED` | Token-bucket rate limit hit (H5) |

---

## 3. Supported Output Formats

ORF writes **16** output formats from `apply-md`, plus **5** from `apply-xliff`.
All `apply-md` formats are listed below with their engine / dependency.

| Format | Engine | Requires | Notes |
|--------|--------|----------|-------|
| `docx` | pandoc | `pypandoc-binary` (auto) | Default target. Supports `--reference-doc` |
| `odt` | pandoc | `pypandoc-binary` | LibreOffice / OpenOffice format |
| `epub` | pandoc | `pypandoc-binary` | E-reader; supports `--title` / `--author` / `--lang` / `--embed-images` |
| `html` | `markdown` + WeasyPrint (optional) | none extra | Pure-Python; no pandoc |
| `rtf` | pandoc | `pypandoc-binary` | Rich Text Format |
| `pdf` | WeasyPrint (default) or pandoc+LaTeX | `weasyprint` extra | Default engine is WeasyPrint since v0.4.1 |
| `pptx` | `md2pptx` (Martin Packer) | `md2pptx` binary on `$PATH` | Slides |
| `icml` | pandoc | `pypandoc-binary` | Adobe InDesign |
| `srt` | ORF internal | none extra | SubRip subtitles |
| `csv` | ORF internal | none extra | Tables only |
| `xlsx` | `openpyxl` | `omni-re-formatter[office]` | Excel |
| `xml` | ORF internal | none extra | Generic XML |
| `ipynb` | `nbformat` | `omni-re-formatter[notebook]` | Jupyter notebook |
| `eml` | ORF internal (email stdlib) | none extra | Open-standard email — **preferred over MSG** |
| `msg` | `aspose-email-foss` (commercial) | `omni-re-formatter[email-output]` | **Discouraged** — see note below |
| `json` | ORF internal | none extra | Structured data |

### 3.1 Pandoc dependency

The `pypandoc-binary` PyPI package ships a vendored pandoc binary, so the `pandoc`
executable is available on `PATH` immediately after `pip install omni-re-formatter`
— no system-wide `apt install pandoc` needed. This covers MD→{DOCX, ODT, EPUB, RTF,
ICML}.

`MD→HTML` and `MD→PDF` (WeasyPrint engine) do **not** need pandoc. The `markdown`
library + WeasyPrint handle them in pure Python.

### 3.2 `MD→MSG` warning

MSG (Microsoft Outlook) format is supported only via `aspose-email-foss`, a
GPLv3-licensed open fork of the commercial Aspose.Email library. Install it
explicitly:

```bash
pip install 'omni-re-formatter[email-output]'
```

Aspose itself is commercial; the `foss` fork is community-maintained. **ORF
recommends `.eml` instead** — `.eml` is the open RFC 5322 standard, is fully
supported by ORF without any extra dependency, and is gracefully handled by
`grep`-friendly text tools. Since v0.4.x, MD→EML auto-synthesizes default
`email_headers` when the frontmatter omits them, so it does not hard-fail.

If you must use MSG, ensure the input MD has an `email_headers` frontmatter block
defining `From`, `To`, `Subject`, and `Date`.

### 3.3 XLIFF backfill formats

`apply-xliff` supports 5 formats: `docx`, `pptx`, `epub`, `html`, `odt`.
The format must match the source skeleton by default; cross-format conversion
requires `--force` and emits a `WARNING`.

---

## 4. Environment Variables

| Variable | Effect |
|----------|--------|
| `ORF_MCP_ALLOWED_DIRS` | Comma-separated allowlist for MCP path validation |
| `MCP_SHARED_SECRET` | Shared-secret for MCP tool auth (omit for dev) |
| `OMNI_CACHE_DIR` | Override ORF's content-addressed cache root (default `~/.omni_cache/orf`) |
| `OMNI_TEST_FAKE_PANDOC` | Test seam — install a fake pandoc that returns stub DOCX (CLI only; **stripped** by MCP) |
| `OMNI_TEST_FAKE_LLM` | Test seam — strip from MCP subprocess env |

---

## 5. See also

- [README](../README.md) — high-level overview
- [TUTORIAL.md](TUTORIAL.md) — worked end-to-end examples
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — error recovery recipes
- [ARCHITECTURE.md](ARCHITECTURE.md) — internals and design decisions
