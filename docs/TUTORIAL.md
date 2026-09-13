# ORF Tutorials

Hands-on walkthroughs for the most common ORF tasks. Every snippet is meant to be
copy-pasted into a shell — adapt the file paths to your own data.

For the full command surface, see [API.md](API.md). For error recovery, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

> **Prerequisites.** Python 3.13+ and a working `pip`. ORF depends on `pypandoc-binary`
> which auto-installs a vendored pandoc on `pip install`, so no system pandoc is
> required for the common formats.

---

## Tutorial 1 — 5-minute quickstart

Goal: install ORF, convert a markdown file to DOCX, verify the result.

```bash
# 1. Create a venv (recommended)
python3.13 -m venv .orf-venv
source .orf-venv/bin/activate

# 2. Install ORF
pip install omni-re-formatter

# 3. Confirm the install
orf --version
# omni-re-formatter, version 0.4.17

# 4. Create a tiny markdown file
cat > hello.md <<'EOF'
---
source_lang: en
target_lang: zh
---

# Hello, ORF

This document was produced by **Omni-Re-Formatter**.

- Bullet 1
- Bullet 2

![logo](logo.png)
EOF

# 5. Convert to DOCX
orf apply-md hello.md --target-format docx --output hello.docx

# 6. Verify
ls -la hello.docx
file hello.docx           # should say "Microsoft OOXML"

# 7. Inspect
orf info hello.docx
```

You should see `Created hello.docx` in the terminal and a 5–15 KB DOCX in your
working directory.

**Other one-liners to try:**

```bash
orf apply-md hello.md --target-format epub --output hello.epub
orf apply-md hello.md --target-format html --output hello.html
orf apply-md hello.md --target-format pdf  --output hello.pdf
orf apply-md hello.md --target-format pptx --output hello.pptx
```

PDF requires the `weasyprint` extra (`pip install omni-re-formatter[weasyprint]`).
PPTX requires the `md2pptx` binary on `PATH`.

---

## Tutorial 2 — Convert MD to DOCX (with images and a brand template)

Goal: produce a polished DOCX that matches a brand template, and extract images to
a sidecar directory instead of inlining them.

### 2.1 What you need

- A translated markdown file `manual.md` (typically the output of `ol translate-md`).
- A reference DOCX `brand-template.docx` whose styles, page size, headers, and
  footers you want to inherit.

### 2.2 Steps

```bash
# 1. Sanity-check the source
orf info manual.md
# Format: MD (or similar)
# Size: 0.05 MB

# 2. Convert with the brand template
orf apply-md manual.md \
  --target-format docx \
  --reference-doc brand-template.docx \
  --output manual.docx

# 3. Inspect the sidecar directory
ls manual_images/
# image_001.png  image_002.jpg  image_003.png
cat manual_images/manifest.json | head -40
```

The `--separate-images` flag (on by default since v0.4.0) tells ORF to extract
every `![alt](path)` image from the markdown into `manual_images/` and produce
a `manifest.json` next to the output DOCX. The DOCX no longer carries embedded
base64 — it references the sidecar files. This keeps the DOCX small and lets you
version-control images separately.

To keep images embedded in the DOCX instead, pass `--no-separate-images`:

```bash
orf apply-md manual.md \
  --target-format docx \
  --no-separate-images \
  --output manual-embedded.docx
```

### 2.3 EPUB variant

EPUB accepts metadata flags:

```bash
orf apply-md manual.md \
  --target-format epub \
  --title "User Manual" \
  --author "Acme Corp" \
  --lang en \
  --embed-images \
  --output manual.epub
```

`--embed-images` inlines the images into the EPUB file so it is fully
self-contained.

### 2.4 JSON output (for pipelines)

Add `--json` to get a machine-readable result envelope:

```bash
orf apply-md manual.md --target-format docx --output manual.docx --json
```

```json
{
  "success": true,
  "output_path": "manual.docx",
  "errors": [],
  "warnings": [],
  "metadata": {
    "engine": "pandoc",
    "target_format": "docx",
    "elapsed_seconds": 1.43
  }
}
```

---

## Tutorial 3 — XLIFF skeleton backfill (preserves original formatting)

Goal: take a translated XLIFF and merge it into the original DOCX/PPTX/EPUB/HTML/ODT
**without** losing the original styles, page layout, or image positions.

This is the high-fidelity path. Use it when the source document has tables,
images, multi-column layouts, or other structure that the MD path would flatten.

### 3.1 What you need

- The original document `original.docx`.
- The translated XLIFF `translated.xlf` (produced by `ol translate-xliff`).
- Optional: the `images.json` file produced by OPP (for image re-injection).

### 3.2 Steps

```bash
# 1. Verify the skeleton format matches
orf info original.docx
# Format: DOCX

# 2. Apply the XLIFF
orf apply-xliff original.docx \
  --xliff translated.xlf \
  --output localized.docx

# 3. Confirm
orf info localized.docx
file localized.docx
```

That's it — the resulting `localized.docx` keeps the original styles, headers,
footers, page breaks, and image positions. ORF replaces only the translatable
text segments marked in the XLIFF.

### 3.3 With image re-injection

If OPP produced an `images.json` describing the original image positions, pass
it to ORF so the images are re-inserted at the right paragraph:

```bash
orf apply-xliff original.docx \
  --xliff translated.xlf \
  --images-json images.json \
  --output localized.docx
```

Floating images (anchored to the page, not the text flow) are handled via the
`is_floating=true` / `wp_anchor_*` fields in `images.json`. ORF injects
`<w:drawing><wp:anchor>…</wp:anchor></w:drawing>` instead of the default
`<w:inline>` (see CHANGELOG v0.4.0).

### 3.4 Cross-format backfill (e.g., DOCX skeleton → PPTX output)

ORF is normally format-preserving: the skeleton extension must match `--format`.
To attempt a cross-format conversion (e.g., turn a translated DOCX skeleton into
a PPTX), pass `--force`. Output may be incomplete or visually broken:

```bash
orf apply-xliff original.docx \
  --xliff translated.xlf \
  --output slides.pptx \
  --format pptx \
  --force
```

`--force` emits a `WARNING` to stderr. Prefer the MD path for cross-format
conversion when fidelity matters.

### 3.5 Inline XLIFF content (no file)

If the XLIFF body is already a string (e.g., from an API call), use
`--xliff-content` instead of `--xliff`:

```bash
XLIFF=$(cat translated.xlf)
orf apply-xliff original.docx \
  --xliff-content "$XLIFF" \
  --output localized.docx
```

`--xliff` and `--xliff-content` are mutually exclusive.

---

## Tutorial 4 — Use ORF with an AI agent (Claude Desktop / Cursor / OpenCode)

Goal: expose ORF as an MCP tool so an AI agent can call it directly.

### 4.1 Install

```bash
pip install omni-re-formatter   # includes the mcp extra
```

The install creates two entry points: `orf` (CLI) and `orf-mcp-server` (MCP server).

### 4.2 Configure the agent

For **Claude Desktop**, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "orf-mcp-server": {
      "command": "orf-mcp-server"
    }
  }
}
```

If you prefer `uvx` (no install required):

```json
{
  "mcpServers": {
    "orf-mcp-server": {
      "command": "uvx",
      "args": ["orf-mcp"]
    }
  }
}
```

For **Cursor** (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "orf-mcp-server": {
      "command": "orf-mcp-server"
    }
  }
}
```

For **OpenCode** (`opencode.json`):

```json
{
  "mcpServers": {
    "orf-mcp-server": {
      "command": "orf-mcp-server"
    }
  }
}
```

### 4.3 Restrict the file-access surface

The MCP server refuses to read or write any path outside the directories you
whitelist. This is mandatory in production — without it, an agent could ask
ORF to overwrite `/etc/passwd` or read your SSH keys.

```bash
# Read-write allowlist
export ORF_MCP_ALLOWED_DIRS="/srv/translations,/tmp/agent-output"
```

Set this **before** starting the agent so the config is loaded at import time
(`src/orf/mcp/server.py:69`).

### 4.4 Try the ping tool

In the agent's chat, ask:

> Call the `orf-mcp-server` `ping` tool.

Expected response:

```json
{ "success": true, "module": "orf", "version": "0.4.17" }
```

### 4.5 End-to-end agent run

Assuming you already have `translated.md` and an `images.json` from the upstream
OPP/OL pipeline, ask the agent:

> Please convert `/srv/translations/translated.md` to DOCX using
> `orf-mcp-server` `apply_md`. Save the output to `/srv/translations/manual.docx`.
> Use the reference template at `/srv/translations/brand-template.docx`.

The agent will issue an MCP call like:

```json
{
  "tool": "apply_md",
  "arguments": {
    "input_md": "/srv/translations/translated.md",
    "target_format": "docx",
    "output_path": "/srv/translations/manual.docx",
    "reference_doc": "/srv/translations/brand-template.docx"
  }
}
```

ORF returns a JSON envelope with `success`, `output_path`, `errors`, `warnings`,
and `metadata`. The agent reads the response and can chain subsequent calls
(e.g., `info` to verify the result, or `apply_xliff` for a different format).

### 4.6 Optional — enable shared-secret auth

To require a secret on every tool call:

```bash
export MCP_SHARED_SECRET="$(openssl rand -hex 32)"
```

The agent must pass `auth_token` in every call. Without it, the server returns
`AUTH_FAILED`. Recommended for shared servers; skip it for local dev.

### 4.7 Full Omni Suite agent pipeline

For a full OPP → OL → ORF run orchestrated by the agent, see the
[Omni Suite AGENTS.md](../../AGENTS.md#full-pipeline-chain-all-three-mcp-tools)
in the parent repo. The ORF half of that pipeline is just one `apply_md` or
`apply_xliff` call.
