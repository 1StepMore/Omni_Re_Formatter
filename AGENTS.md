# AGENTS.md — Omni_Re_Formatter (ORF)

Developer + agent context for the **ORF** sub-repo. The suite-level
[suite-level AGENTS.md](https://github.com/1StepMore/e2e-test-suite/blob/main/AGENTS.md) covers cross-module
orchestration (OPP → OL → ORF); this file is for working **inside**
ORF.

> ORF is the backfill step of the Omni Suite pipeline. OPP extracts
> documents to MD + XLIFF, OL translates them, ORF re-injects the
> translated content into the target format (DOCX, PPTX, EPUB, …).

This file is an **index + invariants** file (≤200 lines, #73): detail
lives in `docs/dev/*.md`, `docs/API.md` and the dev skill
(`.opencode/skills/orf-dev/SKILL.md`).

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
├── channels/          # md2* (16 MD outputs), xliff2* (skeleton backfill), reverse
├── converters/        # Base + ConverterOptions dataclass
├── parsers/           # manifest.json, frontmatter, XLIFF parser
├── skeleton/          # skeleton.zip loader/validator
├── mcp/               # MCP server (7 tools) + security + auth + rate limit
├── agents/            # Foreman + Specialist orchestration (+ workflow/ HITL)
├── cli.py             # `orf` CLI entry
└── …                  # detection, resources, cloud, ai, logging, error_handlers
```

Full annotated tree: `docs/ARCHITECTURE.md`.

## MCP tools (7 total)

| Tool | Purpose |
|------|---------|
| `apply_md` | Convert MD to target format (16 outputs). Accepts **inline content** via `content` (E2E-76) OR path via `input_md`. |
| `apply_xliff` | Backfill XLIFF into source DOCX/PPTX/EPUB. Requires skeleton.zip alongside the source (from OPP). |
| `get_capabilities` | Return ORF module capabilities: MD output formats (16), XLIFF backfill formats (9), input formats, available MCP tools |
| … | `batch_convert`, `detect_format`, `info`, `ping` |

Full per-tool parameter reference: [`docs/API.md`](docs/API.md).
Suite-level inventory: `docs/agent-pipeline-guide.md` in the suite repo.

## Channels — the one decision that matters most

Two backfill channels; choosing wrong is the most common agent mistake:

- **`apply-md`**: MD → 16 output formats (pandoc / pure-Python engines;
  per-format engine table in [`docs/dev/converters.md`](docs/dev/converters.md)).
  Native cross-format.
- **`apply-xliff`**: source + XLIFF + **skeleton.zip** (from OPP) →
  same-format reinjection with original layout. Cross-format refused —
  `--force` is inert; use `apply-md`. Skeleton contracts (XLSX
  `xliff_map.json`, EML `eml_map.json` sidecars):
  [`docs/dev/xliff-backfill.md`](docs/dev/xliff-backfill.md).

Decision flow + full comparison table:
[`docs/dev/converters.md`](docs/dev/converters.md).

## MCP security (fail-closed invariants)

- **`ORF_MCP_ALLOWED_DIRS` is required for tool calls** — the
  PathValidator (`src/orf/mcp/security.py`) is the single source of
  truth for allowlist + extension policy; its model and the full env-var
  table (rate limit, health, tracing, shared-secret auth) live in
  [`docs/dev/mcp-security.md`](docs/dev/mcp-security.md).
- Security-relevant policy changes must keep the suite's path-security
  parity gate green and carry attack tests
  (`tests/test_orf_security_attacks.py`).
- Foreman/HITL: operations >100MB, cloud uploads and
  `MANUAL_INTERVENTION` recovery require human approval
  ([`docs/dev/mcp-security.md`](docs/dev/mcp-security.md)).

## Tests

```bash
make test                            # all tests via uv run pytest tests/ -v
pytest tests/test_orf_mcp_server.py  # MCP server only
pytest tests/ -m e2e                 # E2E tests (need real pandoc + network)
```

Test markers: `unit`, `integration`, `e2e` (registered in
`pyproject.toml`). Coverage target ≥90%. Key regression files:
`tests/test_orf_mcp_server.py` (MCP integration),
`tests/test_mcp_apply_md_xliff_parity.py` (CLI/MCP parity),
`tests/turnkey/test_image_fidelity.py` (E2E-07 image dedup).

## Known issues / gotchas

Full list (E2E-07 image dedup, E2E-79 md2pptx-as-script, MSG commercial
dependency, PDF XLIFF refusal, cross-format XLIFF refusal,
`ImageManager` library-only scope, HITL triggers) lives in
[`docs/dev/mcp-security.md`](docs/dev/mcp-security.md). The two agents
hit most:

- **md2pptx is a Python 3 script, not a pip package** (E2E-79) — install
  hint in the gotchas doc; pandoc fallback is automatic.
- **Cross-format XLIFF is refused by design** — use `apply-md`.

## Pointers to the suite-level docs

- Cross-module orchestration: [AGENTS.md](https://github.com/1StepMore/e2e-test-suite/blob/main/AGENTS.md)
- MCP tool full parameter reference: [`docs/API.md`](docs/API.md)
- Pre-commit hooks: [.pre-commit-config.yaml](https://github.com/1StepMore/e2e-test-suite/blob/main/.pre-commit-config.yaml)
- Compatibility matrix: [COMPATIBILITY.md](https://github.com/1StepMore/e2e-test-suite/blob/main/COMPATIBILITY.md)
- Dev skill: `.opencode/skills/orf-dev/SKILL.md` (add channels/formats,
  touch PathValidator policy)

## How to validate this module

ORF ships its own validation scenario library **in this repo** at
`scenarios/` — 6 tier-1 `orf-backfill`/`orf-md`/`orf-xliff` scenarios
with their own `scenarios/STANDARDS.md` + `scenarios/_fixtures/`. They
declare `requires_env: [MCP_ALLOWED_DIRECTORIES]` — ORF is fail-closed
on the MCP allowlist, so export `MCP_ALLOWED_DIRECTORIES=/tmp` when
running. The suite validation engine runs them via `--repo orf`.

```bash
# From the Omni Suite root (clone: https://github.com/1StepMore/e2e-test-suite)
source .venv_ol/bin/activate

# List ORF's in-repo scenarios
python scripts/validation/run_validation.py --repo orf --list

# Run ORF's in-repo hermetic scenarios (tier 1 = no LLM keys needed;
# MCP allowlist is fail-closed, so set it)
MCP_ALLOWED_DIRECTORIES=/tmp python scripts/validation/run_validation.py --repo orf --tier 1

# Or use the suite-level module filter (adds tool-orf-* agent-surface scenarios)
MCP_ALLOWED_DIRECTORIES=/tmp python scripts/validation/run_validation.py --module orf --tier 1

# Coverage: every ORF MCP tool must be scenario-exercised
python scripts/validation/coverage_audit.py   # orf row must show 7/7, 0 missing
```

The standards bar is `scenarios/STANDARDS.md` (AGENT-SURFACE family:
tool-contract, json-parseable, error-clarity, path-security, exit-codes;
plus the format-structure anchors the orf-md/orf-xliff scenarios assert).
Director loop + 10-minute checklist: `docs/dev/validation-director-loop.md`
in the suite repo. Per-repo validation delivery (run_meta, report card,
delivery package): `docs/dev/per-repo-validation-delivery.md`. ORF
scenario fixes now live HERE (in `scenarios/`); ORF product-code fixes
go through the normal fix cycle in this repo.
