# ORF conversion channels — formats + engines (moved from AGENTS.md, #73)

## 16 output formats + their conversion engines

| Format | Engine | Notes |
|--------|---------|-------|
| DOCX | pandoc (via `pypandoc-binary`) | Requires `pandoc` binary; auto-installed by `pypandoc-binary`. Use `--reference-doc` for custom styles. |
| ODT | pandoc | Same pandoc stack. |
| EPUB | pandoc | Same. |
| HTML | `markdown` lib (pure Python) | No pandoc needed. |
| RTF | pandoc | Same. |
| PDF | `weasyprint` (`[weasyprint]` extra) | Falls back to pandoc if available. |
| PPTX | **`md2pptx` CLI** (E2E-79) | `md2pptx` (E2E-79) is a **Python 3 script** ([MartinPacker/md2pptx](https://github.com/MartinPacker/md2pptx)), NOT a pip package and NOT a prebuilt release binary. Clone the repo and `chmod +x md2pptx`; its directory must stay beside its helper modules (`funnel.py`, `paragraph.py`, …). Requires Python 3 + `python-pptx>=1.0`; inline math also needs `latex2mathml` + `lxml`. ORF invokes it as `md2pptx INPUT.md OUTPUT.pptx`. ORF falls back to pandoc automatically if md2pptx is missing and pandoc is available; errors with an actionable install hint only when both are missing. |
| ICML | pandoc | Same. |
| SRT | `srt` lib | Subtitle format. |
| CSV | pandas | Pure Python. |
| XLSX | `openpyxl` (`[office]` extra) | Pure Python. |
| JSON | `json` stdlib | Direct dump. |
| IPYNB | `nbformat` (`[notebook]` extra) | Pure Python. |
| EML | `email` stdlib | Direct construction. |
| MSG | `aspose-email-foss` (`[email-output]` extra, GPLv3) | **Commercial library**; ORF recommends `.eml` instead. |
| XML | `lxml` | Custom XML serialization. |

## apply-md vs apply-xliff (channel selection)

ORF has two distinct backfill channels. Choosing the wrong one is the
most common agent mistake.

| Aspect | `apply-md` | `apply-xliff` |
|--------|-----------|--------------|
| Input | `.md` file | Source document + `.xlf` + `skeleton.zip` |
| Output formats | 16 (see table above) | Same format as source |
| Engine | pandoc / pure Python | skeleton.zip reinjection |
| Layout | Rendered by pandoc styles | Original layout preserved |
| Cross-format | Native (MD → DOCX, MD → EPUB, etc.) | Not supported — refused |
| Images | `--separate-images` (default) or `--embed-images` | Auto-reinjected from skeleton |

**Decision flow:**

1. Do you have a skeleton.zip from OPP? → **`apply-xliff`**
2. Do you need to convert to a different format than the source? → **`apply-md`**
3. Do you need exact original layout (fonts, styles, floating images)? → **`apply-xliff`**
4. Otherwise → **`apply-md`** (simpler, more output options)

**Cross-format XLIFF** (e.g. DOCX XLIFF → PPTX): not available. ORF
refuses the request and emits no artifact; `--force` no longer changes
that. Use `apply-md`.

**Full pipeline comparison**: see the suite-level
[Pipeline Selection Strategy](https://github.com/1StepMore/e2e-test-suite/blob/main/README.md#pipeline-selection-strategy)
for the complete decision tree and format support matrix.
