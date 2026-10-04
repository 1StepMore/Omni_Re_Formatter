---
name: orf-formatter
description: Convert translated Markdown/XLIFF documents into target formats (DOCX, EPUB, PDF, PPTX, HTML, and more) with AI agent integration.
compatibility: opencode
---

# Omni-Re-Formatter

## When to Use
Use this skill when you need to convert localized Markdown or XLIFF documents to final output formats. Examples:
- Converting translated MD files to DOCX/PDF for distribution
- Backfilling XLIFF translations into original DOCX/PPTX with skeleton.zip
- Batch-converting multiple MD files to EPUB or HTML
- Extracting document info (format, size, embedded resources)

## Procedure

### apply-md (Single File)
1. Ensure `OMNI_TEST_FAKE_LLM=1` is set (unless real LLM keys are configured)
2. Invoke the CLI:
   ```
   orf apply-md <translated.md> --target-format <fmt> --output <output_path>
   ```
3. Supported formats: `docx`, `odt`, `epub`, `html`, `rtf`, `pdf`, `pptx`, `icml`, `srt`, `csv`, `xlsx`, `xml`, `ipynb`, `eml`, `msg`, `json`
4. For JSON machine-readable output, add `--json`

### apply-xliff (XLIFF Backfill)
1. You need: original document, skeleton.zip (from OPP), translated XLIFF
2. Invoke the CLI:
   ```
   orf apply-xliff <original.docx> --xliff <translated.xlf> --output <result.docx> --format docx
   ```
3. Cross-format backfill (e.g. DOCX XLIFF → PPTX): **not supported** — `apply-xliff` refuses it (exit 2, no artifact) and `--force` is inert. Use `apply-md` instead.

### batch-convert (Directory)
1. Place translated MD files in an input directory
2. Invoke the CLI:
   ```
   orf batch-convert <input_dir> --target-format epub
   ```

### Choosing apply-md vs apply-xliff

| Use apply-md... | Use apply-xliff... |
|-----------------|-------------------|
| For 16 output format options | For exact original layout |
| When you have translated .md | When you have skeleton.zip from OPP |
| For cross-format conversion | For same-format backfill |
| With `--separate-images` (default) | With `--images-json images.json` (optional) |

**Decision**: Do you have a skeleton.zip and need layout preservation?
→ apply-xliff. Otherwise → apply-md.

See the full decision tree in [ORF AGENTS.md](https://github.com/1StepMore/Omni_Re_Formatter/blob/main/AGENTS.md)
and the suite-level [Pipeline Selection Strategy](https://github.com/1StepMore/Omni_Suite/blob/main/README.md#pipeline-selection-strategy).

## Configuration
Environment variables:
- `OMNI_TEST_FAKE_PANDOC=1` — Bypass pandoc for test/dev (generates stub DOCX)
- `OMNI_CACHE_DIR` — Cache root (default: `~/.omni_cache/orf/`)

Optional for cloud storage:
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — S3 upload
- `AZURE_STORAGE_CONNECTION_STRING` — Azure Blob upload

## Pitfalls
- **No pandoc**: DOCX/ODT/EPUB/RTF/ICML require pandoc (auto-installed via `pypandoc-binary`)
- **MSG output**: Requires commercial Aspose.Email; use `.eml` instead (open standard)
- **Images**: XLIFF backfill with images needs `--images-json images.json` (produced by OPP)
- **PDF engine**: Default is `weasyprint` (pure Python). Use `--engine pandoc` for pandoc PDF

## Verification
1. Check output file exists at the specified path
2. For XLIFF backfill, verify `--json` output reports `"success": true`
3. For image-injected documents, use `orf info <output_path>` to verify embedded resources
