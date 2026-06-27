> **Note:** This file covers ORF internals only. For cross-module pipeline architecture
> (OPP → OL → ORF), see the [suite-level ARCHITECTURE.md](../../docs/ARCHITECTURE.md).

# ORF Architecture

This document explains the internals of Omni-Re-Formatter (ORF): its module
layout, the data flow through the conversion pipeline, and the key design
decisions that shaped it.

For the public interface, see [API.md](API.md). For end-to-end usage, see
[TUTORIAL.md](TUTORIAL.md).

---

## 1. Module layout

```
src/orf/
├── __main__.py                  # `python -m orf` entry
├── channels/                    # 16 MD→X + 5 XLIFF→X converters
│   ├── md2docx.py
│   ├── md2odt.py
│   ├── md2epub.py
│   ├── md2html.py
│   ├── md2rtf.py
│   ├── md2pdf.py
│   ├── md2pptx.py
│   ├── md2icml.py
│   ├── md2srt.py
│   ├── md2csv.py
│   ├── md2json.py
│   ├── md2xlsx.py
│   ├── md2xml.py
│   ├── md2ipynb.py
│   ├── md2eml.py
│   ├── md2msg.py
│   ├── xliff2docx.py
│   ├── xliff2pptx.py
│   ├── xliff2epub.py
│   ├── xliff2html.py
│   └── xliff2odf.py
├── converters/                  # Base classes & typed options
│   ├── base.py                  # BaseConverter, ConversionResult, ErrorDetail
│   └── options.py               # ConverterOptions dataclass (24 typed fields)
├── parsers/                     # manifest.json, frontmatter
├── detection/                   # FormatDetector (magic bytes)
├── resources/                   # Image resource manager
├── skeleton/                    # skeleton.zip loader & XML helpers
│   ├── skeleton_loader.py
│   └── inline_formatting.py
├── cloud/                       # S3 / Azure Blob clients
├── ai/                          # Layout overflow detection & correction
├── mcp/                         # Agent-facing MCP server (6 tools)
│   ├── server.py                # Standard mcp library server (mcp 1.27.2)
│   ├── schemas.py               # Pydantic models (ImagePlacement, ...)
│   ├── security.py              # PathValidator
│   ├── auth.py                  # MCP_SHARED_SECRET check
│   ├── rate_limiter.py          # Token-bucket DoS limiter
│   └── config.py                # MCPConfig loader
├── agents/                      # Foreman / Specialist orchestration
│   ├── foreman.py               # ForemanAgent (job router)
│   └── specialists/             # 4 specialists
│       ├── format_specialist.py # docx/odt/epub/pptx
│       ├── data_specialist.py   # xlsx/csv/json
│       ├── markup_specialist.py # xml/html
│       └── email_specialist.py  # eml/msg
├── workflow/                    # HITL approval flow
├── logging/                     # Structured JSON logs + audit
├── error_handlers/              # Error codes + recovery strategies
└── cli.py                       # Click CLI (apply-md, apply-xliff, convert-batch, info)
```

---

## 2. Two conversion paths: MD and XLIFF

ORF supports two distinct input pipelines. Choosing the right one is the
single most important architectural decision a user makes.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        INPUT                                             │
│   ┌────────────┐                            ┌────────────────────┐      │
│   │ MD file    │                            │ XLIFF + skeleton   │      │
│   │ (+ images) │                            │ (.xlf / .xliff /   │      │
│   └─────┬──────┘                            │  .zip + original   │      │
│         │                                   │  document)         │      │
│         ▼                                   └─────────┬──────────┘      │
│   ┌────────────┐                                      │                 │
│   │ md2*       │    pandoc / weasyprint / md2pptx /   │                 │
│   │ channel    │    openpyxl / aspose-email / ...     │                 │
│   │ (16 fmts)  │                                      │                 │
│   └─────┬──────┘                                      │                 │
│         │                                             ▼                 │
│         │                                   ┌────────────────────┐      │
│         │                                   │ xliff2*            │      │
│         │                                   │ (5 fmts: docx,     │      │
│         │                                   │  pptx, epub, html, │      │
│         │                                   │  odt)              │      │
│         │                                   └─────────┬──────────┘      │
│         │                                             │                 │
│         ▼                                             ▼                 │
│   ┌────────────────────────────────────────────────────────────┐         │
│   │                    Target format document                  │         │
│   └────────────────────────────────────────────────────────────┘         │
└──────────────────────────────────────────────────────────────────────────┘
```

### 2.1 MD path — `orf apply-md`

- **Input:** a markdown file, optionally with an `images.json` from OPP.
- **Output:** one of 16 target formats. The output document is built fresh
  from the markdown (via pandoc for DOCX/ODT/EPUB/RTF/ICML/PPTX, or via
  pure-Python channels for HTML/PDF/CSV/JSON/XML/EML/SRT/IPYNB/MSG/XLSX).
- **Trade-off:** fast and format-agnostic, but loses the original document's
  paragraph-level structure (e.g., a `![]()` reference is line-level, not
  paragraph-level — pandoc cannot reconstruct the original DOCX paragraph
  layout from a flat markdown stream).
- **When to use:** homogeneous translation, format change, or any case where
  original layout fidelity doesn't matter.

### 2.2 XLIFF path — `orf apply-xliff`

- **Input:** the original document (DOCX/PPTX/EPUB/HTML/ODT) **plus** a
  translated XLIFF (`.xlf`/`.xliff`) or OPP's `skeleton.zip` (a ZIP
  containing the original document's body content).
- **Output:** the original document with every translatable `<text>` segment
  replaced by its XLIFF `<target>` value. All styles, headers, footers,
  tables, image positions, and page breaks are preserved.
- **Trade-off:** slower and format-bound (skeleton extension must match
  `--format`, or pass `--force`), but **lossless** for layout.
- **When to use:** translated documents with complex formatting, tables,
  multi-column layouts, or many images.

### 2.3 The two paths are not interchangeable

If the user has XLIFF + skeleton, do **not** round-trip through MD — they'll
lose the original layout. If they have only a translated MD, they cannot use
the XLIFF path (no skeleton to backfill into). The CLI subcommands reflect
this: `apply-md` for the MD path, `apply-xliff` for the XLIFF path.

---

## 3. Channel architecture

A *channel* is a single-purpose converter that turns the canonical
intermediate format (MD or XLIFF) into one specific target format. Every
channel subclasses `BaseConverter` (`src/orf/converters/base.py`) and
implements `convert(input, output, options)`.

```
                    ┌───────────────────────┐
                    │     BaseConverter      │
                    │  convert(input, out,   │
                    │          options)      │
                    │  inject_images(...)    │   ← optional
                    └──────────┬────────────┘
                               │
       ┌───────────┬───────────┼───────────┬───────────┐
       ▼           ▼           ▼           ▼           ▼
   MD2DOCX    MD2ODT       MD2EPUB      MD2HTML     MD2PDF
   MD2RTF     MD2PPTX      MD2ICML      MD2SRT      ...
   XLIFF2DOCX XLIFF2PPTX   XLIFF2EPUB   XLIFF2HTML  XLIFF2ODF
```

### 3.1 The `ConverterOptions` dataclass

All channels share a single typed options object
(`src/orf/converters/options.py`, 24 fields). This replaced the pre-v0.4.1
`**options: Any` dict-based signature and was a major stability win — the
typed surface catches misnamed flags at the boundary instead of failing deep
inside the channel. The 24 fields cover template, EPUB metadata, image
extraction mode, `text_only`, etc.

### 3.2 Why 21 channel files instead of one big switch?

Each target format has a different engine (pandoc, weasyprint, md2pptx,
openpyxl, aspose-email-foss, ...) and a different set of edge cases
(image embedding for EPUB, floating vs. inline images for DOCX, slide
layouts for PPTX, ...). Splitting them into per-format files keeps each
file under 300 lines and makes it easy to add a new format without
disturbing the existing ones.

### 3.3 Error model

`BaseConverter.convert()` returns a `ConversionResult` with three fields:
`success: bool`, `errors: list[ErrorDetail]`, `warnings: list[WarningDetail]`.
Each `ErrorDetail` carries a `code` (e.g. `MISSING_PANDOC`,
`INVALID_XLIFF`) and a `recovery_strategy` (one of `ABORT`, `SKIP`,
`RETRY`, `FALLBACK`, `MANUAL_INTERVENTION`). The CLI converts these to a
JSON envelope (when `--json`) or a `ClickException` (when text).

---

## 4. Skeleton backfill flow (XLIFF path)

The XLIFF path is the high-fidelity path. It is used whenever the OPP
extractor produced a `skeleton.zip` for the source document. The flow:

```
┌─────────────────────┐
│  OPP extracts       │
│  source.docx →      │
│  - document.xlf     │   (XLIFF with <source> text)
│  - skeleton.zip     │   (original docx body content)
│  - manifest.json    │   (image metadata, source format, ...)
│  - images.json      │   (image placements, floating flags)
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  OL translates      │
│  document.xlf →     │
│  translated.xlf     │   (XLIFF with <target> text)
└──────────┬──────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────┐
│  ORF apply-xliff                                                  │
│                                                                   │
│  1.  Load skeleton  ───────────►  skeleton_loader                 │
│  2.  Parse XLIFF    ───────────►  translate-toolkit / lxml         │
│  3.  For each <trans-unit>:                                       │
│        a. Find target <text> in XLIFF                             │
│        b. Locate matching <text> in skeleton XML (DOCX/PPTX)      │
│           or zip entry (EPUB) or HTML node (HTML)                 │
│        c. Replace text in place, preserving all attributes,      │
│           runs, runs' rPr (run properties), and inline elements    │
│  4.  (Optional) Re-inject images at original paragraph index      │
│        using images.json                                          │
│  5.  Write output document                                        │
└──────────┬───────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────┐
│  result.docx        │   (translated, layout-preserved)
└─────────────────────┘
```

### 4.1 Why the skeleton is mandatory

`translate-toolkit`'s XLIFF backfill is format-aware: it walks the original
document's XML tree and replaces `<w:t>` text nodes (DOCX), `<a:t>` (PPTX),
`<p>` (EPUB), etc. The skeleton (or the original document) is the only
authoritative source for the actual text positions. Without it, ORF has no
way to know *where* to put the translated text.

### 4.2 Cross-format XLIFF (e.g., DOCX skeleton → PPTX output)

Normally ORF refuses this (the skeleton extension must match the output
format). The reason: the DOCX text node `<w:t>` has no direct equivalent in
the PPTX XML schema, so a "backfill" would silently produce a half-empty
PPTX.

The `--force` flag bypasses this guard for experimental workflows. ORF emits
a `WARNING` and proceeds, but the output is likely incomplete or visually
broken. **For cross-format conversion, prefer the MD path** (`apply-md`):
it's a clean re-build in the target format.

### 4.3 Image re-injection (v0.4.0+)

After the text backfill, ORF can re-insert images at the original positions
when an `images.json` is provided (`orf apply-xliff --images-json ...`).
The mechanism is:

1. ORF decodes each image placement to a `w:drawing` element.
2. If `is_floating=true`, an `<wp:anchor>` element is generated with
   `positionH` / `positionV` / `wrapNone` from `wp_anchor_*` fields.
3. Otherwise an `<wp:inline>` element is generated.
4. ORF deduplicates: a drawing whose `<wp:extent>` cx/cy already exists in
   the skeleton is **skipped** (this prevents the 2-extra-drawings bug
   fixed in v0.4.3).

---

## 5. Foreman + Specialist agent system

ORF includes an internal multi-agent orchestration layer
(`src/orf/agents/`). It is **not** invoked by the CLI/MCP directly — it is
the substrate on which agent-style batch conversions are built.

```
                    ┌──────────────────────┐
                    │     ForemanAgent     │
                    │                      │
                    │  1. Receive request  │
                    │  2. Classify format  │
                    │  3. Estimate         │
                    │     complexity       │
                    │     (SIMPLE /        │
                    │      MODERATE /      │
                    │      COMPLEX)        │
                    │  4. Decompose into   │
                    │     sub-tasks        │
                    │  5. Route to a       │
                    │     specialist       │
                    │  6. Aggregate result │
                    └──────────┬───────────┘
                               │
        ┌──────────────┬───────┼─────────┬──────────────┐
        ▼              ▼       ▼         ▼              ▼
   ┌──────────┐  ┌──────────┐ ┌─────────────┐  ┌──────────┐
   │ Format   │  │ Data     │ │ Markup      │  │ Email    │
   │Specialist│  │Specialist│ │ Specialist  │  │Specialist│
   │          │  │          │ │             │  │          │
   │ docx     │  │ xlsx     │ │ xml         │  │ eml      │
   │ odt      │  │ csv      │ │ html        │  │ msg      │
   │ epub     │  │ json     │ │             │  │          │
   │ pptx     │  │          │ │             │  │          │
   └──────────┘  └──────────┘ └─────────────┘  └──────────┘
```

### 5.1 Specialists

- **FormatSpecialist** (`src/orf/agents/specialists/format_specialist.py`):
  Office / publishing formats. Heavy use of pandoc and `md2pptx`.
- **DataSpecialist** (`data_specialist.py`): Tabular / structured data.
  Uses `openpyxl` for XLSX, custom writers for CSV/JSON.
- **MarkupSpecialist** (`markup_specialist.py`): XML and HTML. Uses
  `lxml` and `markdown` library.
- **EmailSpecialist** (`email_specialist.py`): EML uses Python's stdlib
  `email` package; MSG requires `aspose-email-foss`.

### 5.2 Foreman complexity classification

`JobComplexity` is an enum (`src/orf/agents/foreman.py:18`) with three
values:

- **SIMPLE:** one file, one format, default options. Direct channel call.
- **MODERATE:** batch conversion, image sidecar, custom template. Parallel
  channel calls via `ThreadPoolExecutor`.
- **COMPLEX:** multi-document pipeline, cross-format, or HITL-required.
  Decomposed into sub-jobs; each sub-job routed to a specialist.

### 5.3 Error recovery

Every error has a `recovery_strategy` field, and the foreman acts on it
(`src/orf/error_handlers/`):

| Strategy | Action |
|----------|--------|
| `ABORT` | Stop the whole job, return failure to caller |
| `SKIP` | Skip the failing sub-task, continue with the rest |
| `RETRY` | Re-attempt with exponential backoff (3 attempts) |
| `FALLBACK` | Use a fallback engine (e.g., pandoc-laTeX instead of weasyprint) |
| `MANUAL_INTERVENTION` | Pause, queue for HITL approval (see §7) |

### 5.4 Why an agent layer at all?

The CLI and MCP tools expose individual conversions. A real
localization pipeline needs to decide:

- Should I use the MD path or the XLIFF path?
- Should the PDF be WeasyPrint or LaTeX?
- Should I extract images to a sidecar or inline them?
- Is this a cross-format conversion that needs `--force`?

The Foreman encapsulates those decisions. Today it is used by ORF's
internal test orchestration and by a few agent-style batch jobs. The
CLI/MCP surface remains the primary user-facing API.

---

## 6. MCP server design

The MCP server (`src/orf/mcp/server.py`) is intentionally thin:

1. **Standard `mcp` library** (mcp 1.27.2) — the standalone `fastmcp`
   package had a stdio transport bug; ORF migrated to
   `mcp.server.Server` + `mcp.server.stdio.stdio_server`.
2. **CLI delegation** — every tool call invokes
   `python -m orf.cli <args> --json` via `_run_cli_command` and returns
   the parsed JSON. No business logic is duplicated.
3. **Env scrubbing** — `_MCP_SCRUB_ENV_KEYS` strips
   `OMNI_TEST_FAKE_PANDOC`, `OMNI_TEST_FAKE_LLM`, `OMNI_TEST_FAKE`,
   `OMNI_TEST_MOCK`, `OMNI_TEST_STUB` from the subprocess env so a test
   harness that started the MCP server with a fake-engine seam cannot
   silently route live conversions to a stub.
4. **Path safety** — every path input is validated against
   `ORF_MCP_ALLOWED_DIRS` via `PathValidator`
   (`src/orf/mcp/security.py`). Symlinks are not followed.
5. **Auth** — `MCP_SHARED_SECRET` triggers a per-call `check_auth(...)`.
6. **Rate limit** — token-bucket limiter (`H5`) rejects bursts.
7. **No `file_path` in image placements** — `apply_xliff` rejects
   `images[i].file_path` and requires `data_base64` to prevent
   arbitrary file reads (C4 fix).
8. **No `--version` regression** — `orf --version` works because of
   `@click.version_option(package_name="omni-re-formatter", prog_name="orf")`
   in `src/orf/cli.py:198` (added during the v0.4.3 packaging fix).

---

## 7. Content-addressed cache

A small but useful piece of plumbing: ORF caches every successful
conversion under `~/.omni_cache/orf/<sha256>.<ext>` (configurable via
`OMNI_CACHE_DIR`). The cache key for `apply-md` is
`sha256(input ‖ target_format ‖ manifest ‖ template ‖ images_json)`; for
`apply-xliff`, `sha256(input ‖ xliff ‖ format ‖ images_json)`
(`src/orf/cli.py:70` and `:100`).

The cache:

- Skips re-runs of identical inputs (a 14 MB DOCX re-runs in <50 ms
  instead of multiple seconds of pandoc).
- Honors `--no-cache` (force a fresh run).
- Can be wiped with `--clear-cache`.
- Is created with mode `0o700` to protect any sensitive content.
- Is **disabled** when `apply-md` is called with `images_data` (the
  sidecar `images.zip` + `images.json` would otherwise be missing from
  the cache, so the cache would return a DOCX without its sidecar).

---

## 8. Key design decisions

### 8.1 Why two paths (MD and XLIFF)?

Fidelity vs. flexibility. The XLIFF path is lossless for layout but
format-bound. The MD path is format-agnostic but loses paragraph-level
structure. Forcing one on all users would either lose fidelity or block
format conversion. Keeping both is the right call.

### 8.2 Why pandoc instead of writing our own DOCX writer?

DOCX (Office Open XML) is a 6,000-page spec with hundreds of edge cases
(nested styles, run properties, themes, footnotes, comments, ...).
Pandoc has been battle-tested for a decade. Wrapping pandoc is far
cheaper and more correct than re-implementing the OOXML writer.

### 8.3 Why a `ConverterOptions` dataclass instead of `**kwargs`?

Pre-v0.4.1, channels took `**options: Any` — the cost was
misnamed-flag bugs that surfaced deep inside a channel. The dataclass
catches them at the boundary. CHANGELOG v0.4.1: 98 failing tests → 25
failing tests after the migration.

### 8.4 Why the MCP server is a thin CLI wrapper?

A wrapper is:

- **Cheaper to maintain.** When the CLI gains a new flag, the MCP tool
  gains it for free.
- **Safer.** The CLI's own error handling and JSON envelope are reused
  — no risk of the two surfaces drifting.
- **Easier to test.** `tests/test_e2e_orf_mcp.py` invokes the MCP
  aliases directly; the CLI is exercised by the CLI integration tests.
  Both share the same underlying channel.

### 8.5 Why is MSG discouraged?

`aspose-email-foss` is a community fork of a commercial library. The
licensing is GPLv3, the maintenance is volunteer-driven, and the
upstream is closed-source. `.eml` is the open RFC 5322 standard,
needs no extra dependency, and is universally supported. ORF's
recommendation: use `.eml` unless you have a hard requirement for
Outlook-native MSG.

### 8.6 Why strip test seams in the MCP subprocess env?

ORF has several `OMNI_TEST_*` environment variables that monkey-patch
`subprocess.run`, the LLM client, etc. If a test harness starts the MCP
server with `OMNI_TEST_FAKE_PANDOC=1` and then runs a live conversion,
the monkey-patch would silently route every call to a stub DOCX. The
`_MCP_SCRUB_ENV_KEYS` set in `src/orf/mcp/server.py:57` prevents that by
stripping the seams from the CLI subprocess env. The list is centralized
so adding a new seam means updating one constant plus one test.

### 8.7 Why document-wide (not paragraph-local) image dedup?

OPP's `paragraph_index` is off-by-one vs. ORF's `//w:p` enumeration
(observed in the Haier DOCX: OPP says paragraph 6, skeleton places
IM 16 at paragraph 7). Paragraph-local dedup would miss duplicates
whose source paragraph index disagrees with the skeleton's enumeration.
Document-wide `<wp:extent>` cx/cy match is the only safe key.

---

## 9. Testing strategy

ORF has 400+ tests across:

- **Unit tests** — each channel in isolation with synthetic inputs.
- **CLI integration tests** — `tests/test_cli_*.py`.
- **MCP integration tests** — `tests/test_orf_mcp_server.py` and
  `tests/test_e2e_orf_mcp.py`.
- **E2E pipeline tests** — `tests/test_e2e_omni_pipeline.py` and
  `tests/test_e2e_real_llm.py` (24 nightly tests).
- **Security tests** — `tests/security/test_orf_mcp_auth.py`.
- **Observability tests** — `tests/observability/test_mcp_health.py`.
- **Image-fidelity regression** —
  `tests/turnkey/test_image_fidelity.py::test_drawing_count_equals_source`
  (locked in by the v0.4.3 inline-dedup fix).
- **Packaging tests** — `tests/test_packaging.py` exercises
  `python -m build` and `pip install -e .`.

The CI matrix runs unit + integration on every PR and the full E2E +
real-LLM suite nightly.
