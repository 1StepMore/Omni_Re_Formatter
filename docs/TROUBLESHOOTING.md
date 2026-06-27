# ORF Troubleshooting

Recipes for the most common ORF errors. Each entry shows the symptom, the
likely cause, and the fix. Errors are grouped by category.

For the underlying CLI/MCP behavior, see [API.md](API.md). For high-level
workflow guidance, see [TUTORIAL.md](TUTORIAL.md).

---

## 1. Pandoc / engine errors

### 1.1 "pandoc: command not found"

**Symptom.** `orf apply-md` exits with code 1 and stderr contains
`pandoc: command not found` or `FileNotFoundError: [Errno 2] No such file or directory: 'pandoc'`.

**Cause.** Pandoc is missing. This is unusual with `pypandoc-binary` (which ships
a vendored pandoc), but can happen if:
- A minimal `pip install` skipped the `[dev]` extras and `pypandoc-binary` was
  filtered out.
- The venv's `bin/` is not on `PATH` (e.g., a Docker container with `ENV PATH` reset).

**Fix.**

```bash
# 1. Verify the vendored pandoc
python -c "import pypandoc; print(pypandoc.get_pandoc_path())"
# Should print something like /path/to/venv/bin/pandoc

# 2. If empty, reinstall pypandoc-binary
pip install --force-reinstall pypandoc-binary

# 3. If pandoc is at a non-standard location, symlink it onto PATH
ln -s "$(python -c 'import pypandoc; print(pypandoc.get_pandoc_path())')" /usr/local/bin/pandoc
```

### 1.2 "No module named 'weasyprint'" (PDF)

**Symptom.** `orf apply-md manual.md --target-format pdf` fails with
`ModuleNotFoundError: No module named 'weasyprint'`.

**Cause.** WeasyPrint is an optional dependency. ORF uses it as the default PDF
engine (changed from `pdflatex` to WeasyPrint in v0.4.1). WeasyPrint requires
system-level C libraries for rendering.

**Fix.**

```bash
# 1. Install system dependencies (required by WeasyPrint)
# Debian / Ubuntu:
sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
  libcairo2 libcairo-gobject2 libgdk-pixbuf2.0-0 shared-mime-info

# macOS (Homebrew):
brew install pango cairo gdk-pixbuf

# 2. Then install the Python package
pip install 'omni-re-formatter[weasyprint]'

# 3. Verify
orf apply-md manual.md --target-format pdf --output manual.pdf
```

If you are using a `python:3.13-slim` Docker image, the system debs above
must be installed in your Dockerfile before WeasyPrint. Example:

```dockerfile
FROM python:3.13-slim
RUN apt-get update && apt-get install -y \
    libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libcairo-gobject2 libgdk-pixbuf2.0-0 shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
RUN pip install 'omni-re-formatter[weasyprint]'
```

If WeasyPrint is not an option in your environment, install a real LaTeX
toolchain and tell pandoc to use it (set the engine via `--template` plus the
`PDF_ENGINE` env var or by switching the channel).

### 1.3 "XLSX conversion requires openpyxl"

**Symptom.** `orf apply-md data.md --target-format xlsx` errors with
`XLSX conversion requires openpyxl. Install with: pip install omni-re-formatter[office]`.

**Fix.**

```bash
pip install 'omni-re-formatter[office]'
```

The same pattern applies to `[notebook]` for `ipynb` and `[email-output]` for
`msg`.

### 1.4 "MSG conversion requires aspose-email-foss"

**Symptom.** `orf apply-md mail.md --target-format msg` errors with
`MSG conversion requires aspose-email-foss. Install with: pip install omni-re-formatter[email-output]`.

**Cause.** MSG (Microsoft Outlook format) is only supported via the
`aspose-email-foss` library. Aspose.Email is commercial; the `-foss` fork is
GPLv3 and community-maintained.

**Fix.** Either install the optional dep, or — **recommended** — use `.eml`
instead. EML is the open RFC 5322 standard, is fully supported by ORF without
extra dependencies, and works with every mail client.

```bash
# Option A — install the commercial-ish dep
pip install 'omni-re-formatter[email-output]'

# Option B — recommended, use EML
orf apply-md mail.md --target-format eml --output mail.eml
```

---

## 2. XLIFF / skeleton errors

### 2.1 "Skeleton file extension '.pptx' does not match --format 'docx'"

**Symptom.** `orf apply-xliff original.pptx --xliff translated.xlf
--output result.docx` errors with
`Skeleton file extension '.pptx' does not match --format 'docx' (expected '.docx' or '.zip')`.

**Cause.** ORF's `apply-xliff` is **format-preserving** by default. The
skeleton file extension must match `--format`, or be `.xlf`/`.xliff`/`.zip`
(for ZIP-based formats). This guard was added in 2026-06-17 round 5 to surface
mismatches as a clear error instead of letting `translate-toolkit` crash deep
inside (`src/orf/cli.py:786`).

**Fix options.**

```bash
# Option 1 — match the formats
orf apply-xliff original.pptx --xliff translated.xlf \
  --output result.pptx --format pptx

# Option 2 — cross-format with --force (output may be broken)
orf apply-xliff original.pptx --xliff translated.xlf \
  --output result.docx --format docx --force

# Option 3 — use the MD path for cross-format conversion
# (cleaner output, but loses original layout)
orf apply-md translated.md --target-format docx --output result.docx
```

### 2.2 "XLIFF file not found" / "Original document not found"

**Symptom.** `FileNotFoundError` on the XLIFF path or the original document.

**Fix.**

```bash
# Verify both files exist and are readable
ls -la original.docx translated.xlf
file original.docx translated.xlf

# Use absolute paths to avoid cwd ambiguity
orf apply-xliff /abs/path/to/original.docx \
  --xliff /abs/path/to/translated.xlf \
  --output /abs/path/to/result.docx
```

### 2.3 "No manifest found"

**Symptom.** ORF logs `Failed to parse manifest` and continues without
manifest-driven metadata.

**Cause.** Manifests are optional for `apply-md` — ORF will use the
frontmatter instead. This is a warning, not an error.

**Fix.** If you expected a manifest, check that `manifest.json` lives in the
same directory as the input file, or in any parent directory (ORF searches
upward via `find_manifest()`). If you don't have one, ignore the warning —
the conversion will still succeed.

### 2.4 "MUTUALLY_EXCLUSIVE" — `--xliff` and `--xliff-content` both passed

**Symptom.** `click.BadParameter: --xliff and --xliff-content are mutually exclusive`.

**Fix.** Use only one. `--xliff` for a file path, `--xliff-content` for an
inline string.

```bash
# Pick one
orf apply-xliff original.docx --xliff translated.xlf --output result.docx
XLIFF=$(cat translated.xlf)
orf apply-xliff original.docx --xliff-content "$XLIFF" --output result.docx
```

---

## 3. MCP-specific errors

### 3.1 "PATH_NOT_ALLOWED" / "path is outside the allowed directories"

**Symptom.** MCP tool returns
`{"success": false, "errors": [{"code": "PATH_NOT_ALLOWED", ...}]}`.

**Cause.** `ORF_MCP_ALLOWED_DIRS` was set and the requested path is not inside
any of the listed directories. The check is in
`src/orf/mcp/security.py:PathValidator`.

**Fix.**

```bash
# Either expand the allowlist
export ORF_MCP_ALLOWED_DIRS="/srv/translations,/tmp/agent-output,/home/me/docs"

# Or move the file into an allowed directory
mv /home/me/secret.docx /srv/translations/secret.docx
```

Remember: the env var must be set **before** the MCP server starts. If you
change it later, restart the agent.

### 3.2 "AUTH_FAILED" / "401 Unauthorized"

**Symptom.** Every MCP tool returns `AUTH_FAILED`.

**Cause.** `MCP_SHARED_SECRET` is set in the server's environment but the
agent isn't passing `auth_token` in its tool calls.

**Fix.** Either unset the secret (dev only):

```bash
unset MCP_SHARED_SECRET
```

…or pass the matching token from the agent. In Claude Desktop, configure it via
the MCP server `env` block:

```json
{
  "mcpServers": {
    "orf-mcp-server": {
      "command": "orf-mcp-server",
      "env": {
        "MCP_SHARED_SECRET": "your-secret-here"
      }
    }
  }
}
```

### 3.3 "RATE_LIMIT_EXCEEDED"

**Symptom.** MCP tool returns `RATE_LIMIT_EXCEEDED` after a burst of calls.

**Cause.** H5 token-bucket rate limiter. Tune via `MCP_RATE_LIMIT_*` env vars
(see `src/orf/mcp/rate_limiter.py`).

**Fix.** Wait a few seconds, or raise the limit in the server environment
(recommended only for trusted internal agents).

### 3.4 MCP server is silent (no responses on stdio)

**Symptom.** The agent invokes `orf-mcp-server` but no JSON-RPC responses ever
arrive. `ping` times out.

**Cause.** This was a known bug in `fastmcp`'s stdio transport. ORF now uses
the standard `mcp` library (mcp 1.27.2) with `mcp.server.Server` +
`mcp.server.stdio.stdio_server` (`src/orf/mcp/server.py:751`). If you still
see the symptom, check:

```bash
# Use a Python with the standard mcp library installed
.venv_ol/bin/python -m orf.mcp.server   # NOT python3.14
```

System `python3.14` may not have `mcp` installed; the server will fail to
import. Always run via the venv that has ORF installed (the Omni Suite
project uses `.venv_ol/`).

### 3.5 "EMPTY_OUTPUT" / "JSON_PARSE_ERROR"

**Symptom.** MCP tool returns `{"code": "EMPTY_OUTPUT", ...}` or
`{"code": "JSON_PARSE_ERROR", ...}`.

**Cause.** The CLI subprocess exited without producing parseable JSON. Common
triggers:
- A `ClickException` was raised before the JSON envelope was built.
- An unhandled exception in the channel converter.

**Fix.**

```bash
# Run the same command via the CLI to see the real error
orf apply-md /path/to/file.md --target-format docx -o /tmp/out.docx --json
# The CLI's stderr will show the real traceback.

# Common underlying causes:
# - Permission denied on the output path
# - Disk full
# - Input file is not valid markdown
# - One of the channel converters crashed
```

---

## 4. File / IO errors

### 4.1 "File X is N MB, exceeds limit of N MB"

**Symptom.** `orf` exits with code 1 and prints
`Error: File <path> is <size>MB, exceeds limit of <limit>MB`.

**Cause.** The `--max-file-size-mb` safety guard rejected the file. This is
HITL-style protection against runaway conversions.

**Fix.**

```bash
# Either raise the limit
orf apply-md huge.md --target-format docx --max-file-size-mb 500 -o huge.docx

# Or omit the limit
orf apply-md huge.md --target-format docx -o huge.docx
```

### 4.2 Cache hit returns stale data

**Symptom.** A re-run with different settings still produces the cached result.

**Cause.** The content-addressed cache key includes input bytes, format,
manifest, template, and `images.json` — but if any of those didn't change
between runs, the cache key is identical and ORF reuses the prior result
(`src/orf/cli.py:118`).

**Fix.**

```bash
# Force a fresh conversion
orf apply-md manual.md --target-format docx -o manual.docx --no-cache

# Or wipe the entire ORF cache
orf apply-md manual.md --target-format docx -o manual.docx --clear-cache

# Or change the cache root (e.g., for parallel test runs)
OMNI_CACHE_DIR=/tmp/test-cache-$$ orf apply-md ...
```

### 4.3 Permission denied writing the output

**Symptom.** `PermissionError: [Errno 13] Permission denied: 'result.docx'`.

**Fix.**

```bash
# Check the output directory is writable
ls -ld /path/to/output
touch /path/to/output/test && rm /path/to/output/test

# Or pick a different output path
orf apply-md manual.md --target-format docx -o /tmp/result.docx
```

---

## 5. Format-detection errors

### 5.1 "Format detection failed"

**Symptom.** With `--auto-detect` (or `--target-format auto`), ORF raises a
`ClickException` with hints about manifest.json and valid input.

**Fix.** Either:

```bash
# Specify the format manually
orf apply-md file.md --target-format docx -o file.docx

# Or create a manifest.json next to the file:
cat > file.manifest.json <<EOF
{
  "source": {"format": "docx", "path": "file.docx"},
  "images": []
}
EOF
orf apply-md file.md --auto-detect
```

### 5.2 "Unsupported format '<fmt>'"

**Symptom.** `ClickException: Unsupported format '<fmt>'`.

**Cause.** The format string is misspelled or not supported. The valid set for
`apply-md` is: `docx, odt, epub, html, rtf, pdf, pptx, csv, json, xlsx, xml,
ipynb, eml, msg, icml, srt`. For `apply-xliff`: `docx, pptx, epub, html, odt`.

**Fix.** Use one of the valid strings:

```bash
orf apply-md file.md --target-format docx   # good
orf apply-md file.md --target-format DOCX   # BAD — case-sensitive
```

---

## 6. Image / picture errors

### 6.1 Duplicate images in DOCX output (inline)

**Symptom.** Output DOCX has more `<w:drawing>` blocks than the source.

**Cause.** This was a regression in v0.4.2 — the inline image path didn't
dedup against drawings already in the skeleton. Fixed in v0.4.3 by
`_paragraph_already_has_drawing(root, cx, cy)` which does a document-wide
`<wp:extent>` cx/cy match (`src/orf/channels/xliff2docx.py`).

**Fix.** Upgrade to v0.4.3+.

```bash
pip install --upgrade 'omni-re-formatter>=0.4.3'
```

Regression guard: `tests/turnkey/test_image_fidelity.py::test_drawing_count_equals_source`.

### 6.2 Floating images missing (5/12 dropped)

**Symptom.** Some floating (anchored) images from the source are missing in
the output.

**Cause.** Pre-v0.4.0, floating images were silently routed through the
inline path and dropped if their positioning metadata didn't match. Fixed by
the new `_inject_floating_image()` method which honors
`is_floating=true` + `wp_anchor_*` fields in the OPP `images.json`
(`src/orf/channels/xliff2docx.py`).

**Fix.** Upgrade to v0.4.0+ and ensure OPP is producing the floating-image
metadata.

### 6.3 "image[i].file_path is not allowed via MCP"

**Symptom.** MCP `apply_xliff` returns
`{"code": "FILE_PATH_NOT_ALLOWED", ...}`.

**Cause.** The MCP tool rejects `images[i].file_path` to prevent arbitrary
file reads. Clients must embed the image bytes inline via `data_base64`
(C4 fix at `src/orf/mcp/server.py:319`).

**Fix.** Convert your image placements to base64:

```python
import base64, json

with open("logo.png", "rb") as f:
    data_b64 = base64.b64encode(f.read()).decode("ascii")

images = [{
    "data_base64": data_b64,
    "mime_type": "image/png",
    "paragraph_index": 6,
}]
# Pass `images=images` to apply_xliff
```

---

## 7. Concurrency / batch errors

### 7.1 `convert-batch` says "No files found to convert"

**Cause.** The pattern didn't match, or the matched files don't have YAML
frontmatter (the `has_frontmatter()` check in `src/orf/cli.py:613`).

**Fix.**

```bash
# Check what's in the directory
ls /path/to/translated/

# Check the pattern
orf convert-batch /path/to/translated --target-format docx --pattern "*.md"

# Make sure each .md has a frontmatter block:
# ---
# source_lang: en
# target_lang: zh
# ---
# (content)
```

### 7.2 Workers crash in parallel mode

**Symptom.** `convert-batch --max-workers 8` reports failures or hangs.

**Fix.** Reduce to serial mode to isolate the failing file:

```bash
orf convert-batch ./translated --target-format docx --max-workers 1
```

Then re-enable parallelism once the failing file is identified and fixed.

---

## 8. Quick diagnostic checklist

When something fails, work down this list:

1. `orf --version` — confirm ORF is installed and the venv is correct.
2. `orf info <input>` — confirm ORF can see and detect the input.
3. Re-run with `--verbose` (or `-v`) for `DEBUG` logs.
4. Try the smallest possible repro (1 paragraph of markdown → DOCX).
5. Clear the cache: `orf apply-md ... --clear-cache` (uses `apply-md` as a
   convenience command to expose the cache-clearing flag).
6. Check `logs/audit_<date>.log` for `correlation_id` and `agent_id` traces.
7. If using the MCP server, run the same command via the CLI with `--json`
   to get a structured error.

If all else fails, open an issue with: ORF version (`orf --version`), the exact
command, the full `--verbose` output, and a minimal repro markdown file.
