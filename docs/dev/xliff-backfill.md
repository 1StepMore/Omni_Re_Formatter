# ORF XLIFF backfill + skeleton.zip (moved from AGENTS.md, #73)

The `apply-xliff` and `apply_xliff` channels need a `skeleton.zip`
alongside the source document. The skeleton preserves the original
DOCX/PPTX/EPUB/HTML/XLSX ZIP structure (and, for EML, the original message)
so ORF can re-inject translated text without re-rendering styles/media.

**skeleton.zip is produced by OPP** at the same path as the XLIFF.
It contains:
- `word/document.xml` (DOCX) or `ppt/slides/*.xml` (PPTX) — preserved
- `[Content_Types].xml` — preserved
- `word/styles.xml`, `word/numbering.xml` — preserved
- `word/media/*` — preserved (E2E-07 fuzzy match by cx/cy to avoid double-insert)

**XLSX skeletons are different** (Omni_Pre_Processor#92): the archive holds
the original workbook bytes *plus* one archive-root sidecar, `xliff_map.json`,
which maps XLIFF trans-unit ids to cells (`{"ref": "A1", "translatable": …}`).
`src/orf/channels/xliff2xlsx.py` needs it — a bare `.xlsx` cannot be
backfilled — and it is never handed to openpyxl. A trans-unit's text is one
row joined by `" | "`, so segment position is cell position; only
`translatable: true` cells are written, which keeps formulas, merged-range
non-anchor cells, numbers and blanks untouched.

**EML skeletons** carry the original `message.eml` bytes *plus* an
`eml_map.json` sidecar naming the MIME body part offered for translation
(`walk_index`, `content_type`, `unit_ids`, `joiner`). `src/orf/channels/xliff2eml.py`
rewrites only that part's payload and splices it under the original header
block; a bare `.eml` cannot be backfilled, and headers, the multipart boundary,
the alternative body part and attachments are preserved.

**Cross-format XLIFF backfill** (e.g., DOCX XLIFF → PPTX): **not
implemented.** ORF refuses with a clear error and emits no file. Use
`orf apply-md --target-format pptx` instead.
