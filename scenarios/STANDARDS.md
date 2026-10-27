# Validation Standards — Omni_Re_Formatter (ORF)

This is the citable standards reference for the ORF backfill scenario
library (`Omni_Re_Formatter/scenarios/`). Every scenario step that
enforces a quality bar cites the exact standard it checks as
`standard: STANDARDS.md#<anchor-id>` — the same five-part evidence
contract (surface / real call / expect / actual / artifact-to-show) the
suite's validation framework documents in
`docs/agent-tester-validation-guide.md` §2-§3.

The bars are grouped into the two named families of the suite framework
(draft D13): **AGENT-SURFACE** (agent-user conformance — every agent-facing
tool/CLI works as an agent would use it) and **HUMAN-QUALITY**
(human-result quality — the backfill output satisfies human end-users).
This file is repo-scoped to ORF: it restates the suite bar
(`scenarios/STANDARDS.md`) for the ORF backfill surface and adds the
format-structure anchors the orf-backfill scenarios assert.

Thresholds are NOT invented. AGENT-SURFACE bars mirror the suite
`scenarios/STANDARDS.md`; format engines copy
`Omni_Re_Formatter/AGENTS.md:101-120` (16 apply-md outputs).

## AGENT-SURFACE

Mission axis 1 (draft D13): validation proves every agent-facing surface —
the live ORF MCP tool registry (`orf.mcp.server`, `orf.mcp.tools`) and the
`orf` CLI — works as an agent-user would use it: schema-shaped params
accepted, structured JSON returned, clear parseable errors on bad input,
path-security denial honored, sane exit codes. Covered by the
`orf-backfill-*` scenarios in this directory (the suite's own
`scenarios/agent-surface/tool-orf-*` covers each live tool separately).

### Tool contract conformance {#tool-contract}

Every agent-facing ORF tool accepts its schema-shaped parameters,
executes against the real surface, and returns a structured result
carrying the expected keys — no dead tools, no shape drift between the
live registry and the shipped behavior. A step enforces this with an
`expect` block of `success: true` plus `data_has: [...]` naming the keys
the tool's live schema declares (from the live registry at
`orf/mcp/tools/`, NOT the stale AGENTS.md tables — ORF 7 at baseline:
`apply_md`, `apply_xliff`, `batch_convert`, `detect_format`, `info`,
`ping`, `get_capabilities`).

**How to check:** in-process call of the real module tool function (e.g.
`from orf.mcp.tools import ping` per the AGENTS.md in-process pattern),
then grade `success: true` + key presence against the tool's declared
result shape. Citable as `standard: STANDARDS.md#tool-contract`.

### JSON parseability {#json-parseable}

Every tool result and every CLI JSON output parses as valid JSON — an
agent-user's `json.loads` must never fail on ORF's own output. A response
that cannot be parsed is a contract break regardless of content.

**How to check:** `json.loads` on the captured output inside the step; a
`JSONDecodeError` fails the step. Citable as `standard:
STANDARDS.md#json-parseable`.

### Error clarity {#error-clarity}

Failures are reported as clear, parseable, agent-readable error messages
that name the failing surface and the offending parameter — never raw
stack-trace soup. An agent-user must be able to act on the message without
reading the engine source.

**How to check:** invoke the tool with a deliberately bad parameter
(missing input file / nonexistent path); expect an error payload that
names the surface + reason and does NOT contain a `Traceback (most recent
call last)` frame. Citable as `standard: STANDARDS.md#error-clarity`.

### Path security {#path-security}

Path-taking tools deny access outside the configured allowlist
(`MCP_ALLOWED_DIRECTORIES`, with per-module override `ORF_MCP_ALLOWED_DIRS`);
an out-of-allowlist path MUST be rejected with a clear denial, never
silently accepted. ORF is fail-closed — an unset allowlist means denial
(`orf/mcp/config.py:179-186` raises a ValueError naming the required
vars).

**How to check:** call a path-taking MCP tool (e.g. `apply_md`) with a
path outside the declared allowlist; expect a denial error naming the
path. Citable as `standard: STANDARDS.md#path-security`.

### Exit codes {#exit-codes}

CLI commands exit 0 on success and a nonzero code on failure, with the
failure reason on stderr — so scripts and agents can branch on the code.
The missing-input edge locks a nonzero-exit contract: `orf apply-md
<nonexistent.md>` exits 2 with `Error: Invalid value for 'INPUT_MD'...`
on stderr (click path validation).

**How to check:** `expect: {exit_code: 0}` on success paths;
`expect: {exit_code: 2, stderr_has: [...]}` on guarded failure paths.
Citable as `standard: STANDARDS.md#exit-codes`.

## HUMAN-QUALITY

Mission axis 2 (draft D9 + D12): a green ORF backfill run must prove the
converted output meets the end-user bar. The ORF-scoped format-structure
anchors below assert the structural invariants of the generated files —
the same facts the suite's `scenarios/STANDARDS.md` "Reference: format
engines and inputs" section documents.

### Backfilled file is produced {#backfill-output}

Every `apply-md` backfill step must produce a non-empty output file at the
declared `-o` path — the functional floor for any ORF result: the CLI
exited 0, the file exists on disk, and its size is > 0 bytes. This is the
pipeline's "Created <path>" contract plus the `collect_artifacts`
evidence record.

**How to check:** the CLI step grades `exit_code: 0` +
`stdout_has: ["Created <path>"]`; the assert step reads the file and
requires `len(data) > 0`. Citable as `standard: STANDARDS.md#backfill-output`.

### HTML structural integrity {#html-structure}

The HTML output of `apply-md --target-format html` (markdown lib engine,
pure Python — no pandoc) carries the HTML document signature: contains an
`<html` element and a `<body` element, and the document body preserves the
markdown content (frontmatter is stripped, not rendered).

**How to check:** read the output file; require `"<html" in data` and
`"<body" in data`. Citable as `standard: STANDARDS.md#html-structure`.

### JSON parseability of backfill output {#json-structure}

The JSON output of `apply-md --target-format json` (json stdlib engine —
direct dump of the ` ```json ` base structure) parses with `json.loads`
and yields a dict or list — the md2json combined-mode contract
(`Omni_Re_Formatter/CHANGELOG.md` v0.4.7).

**How to check:** `json.loads(open(out).read())` succeeds and the result
is a `dict` or `list`. Citable as `standard: STANDARDS.md#json-structure`.

### CSV parseability of backfill output {#csv-structure}

The CSV output of `apply-md --target-format csv` (pandas engine) parses
with `csv.reader` and yields at least 1 row, and the header row is the
markdown table's header.

**How to check:** `list(csv.reader(fh))` yields `>= 1` row. Citable as
`standard: STANDARDS.md#csv-structure`.

### XML well-formedness {#xml-structure}

The XML output of `apply-md --target-format xml` (lxml engine) is
well-formed: `xml.etree.ElementTree.parse` succeeds.

**How to check:** `ET.parse(path)` inside the step. Citable as `standard:
STANDARDS.md#xml-structure`.

### SRT passthrough {#srt-structure}

The SRT output of `apply-md --target-format srt` (srt lib engine) is a
non-empty passthrough of the MD content when the input carries no SRT
timing blocks, and the translated content survives.

**How to check:** read the output; require non-empty and the content
heading present. Citable as `standard: STANDARDS.md#srt-structure`.

## Reference: format engines and inputs

Not bars — the structural-invariant facts the orf-backfill scenarios cite
per format, copied from `Omni_Re_Formatter/AGENTS.md:101-120`.

**ORF apply-md output formats (16) and their engines:**

| Format | Engine | Notes |
|--------|--------|-------|
| DOCX | pandoc (via `pypandoc-binary`) | Requires pandoc binary; auto-installed by `pypandoc-binary`. |
| ODT | pandoc | Same pandoc stack. |
| EPUB | pandoc | Same. |
| HTML | `markdown` lib (pure Python) | No pandoc needed. |
| RTF | pandoc | Same. |
| PDF | `weasyprint` (`[weasyprint]` extra) | Falls back to pandoc if available. |
| PPTX | `md2pptx` CLI (E2E-79) | Python 3 script + `python-pptx>=1.0`; falls back to pandoc when missing. |
| ICML | pandoc | Same. |
| SRT | `srt` lib | Subtitle format. |
| CSV | pandas | Pure Python. |
| XLSX | `openpyxl` (`[office]` extra) | Pure Python. |
| JSON | `json` stdlib | Direct dump (combined mode: ` ```json ` fence as base structure). |
| IPYNB | `nbformat` (`[notebook]` extra) | Pure Python. |
| EML | `email` stdlib | Direct construction. |
| MSG | `aspose-email-foss` (`[email-output]` extra, GPLv3) | Commercial library; ORF recommends `.eml` instead. |
| XML | `lxml` | Custom XML serialization. |

**Tier-1 hermetic formats (no pandoc / no network / no LLM):** HTML,
SRT, CSV, JSON, XML — the formats the orf-backfill scenarios exercise.
DOCX / ODT / EPUB / RTF / ICML need the pandoc binary; PPTX needs the
`md2pptx` CLI or pandoc; PDF needs the WeasyPrint system C-libs; MSG needs
the commercial Aspose library — none are tier-1 hermetic.
