---
name: orf-dev
description: Develop inside the ORF sub-repo — add a new output channel/format, extend the MCP tool surface, wire skeleton backfill, or touch PathValidator policy. Use when asked to 加输出格式/channel、改 MCP 工具、动 PathValidator 白名单、或修 ORF 的 backfill 行为。
---

# ORF dev — adding channels, formats, and MCP surface

Checklist-driven SOP for the two most common ORF changes. Every touched
number/path is enforced by `scripts/doc_inventory.py --check` — run it
before pushing.

## Adding a new MD output format (e.g. a new `md2X` channel)

1. **Channel module**: `src/orf/channels/md2X.py` — implement the
   converter; register it in the dispatch the other `md2*` channels use
   (see `src/orf/channels/` siblings).
2. **CLI**: `orf apply-md --target-format X` — the CLI dispatch in
   `src/orf/cli.py` / `src/orf/commands/apply_md.py`.
3. **MCP**: add `X` to `_MD_OUTPUT_FORMATS` in
   `src/orf/mcp/tools/get_capabilities.py` (the doc gate derives "16
   output formats" from this list).
4. **PathValidator**: if the new output extension is not already in
   `PathValidator.ALLOWED_EXTENSIONS` (`src/orf/mcp/security.py:62`),
   add it — E2E-80 exists because output paths like `result.csv` were
   rejected.
5. **Engine dependency**: if the engine needs an optional extra
   (like `[office]` / `[weasyprint]`), wire the availability fallback the
   way `md2pptx` does (pre-flight check + actionable install hint +
   pandoc fallback where sensible) and add a row to
   `docs/dev/converters.md` (the engines table).
6. **Scenarios**: add an `orf-md-*` scenario under `scenarios/`
   (hermetic tier 1 where possible) and keep the coverage audit green —
   the suite's `coverage_audit.py` orf row must show every tool
   exercised.
7. **Gate**: `python scripts/doc_inventory.py --check` → regenerate if
   it flags the inventory as stale.

## Adding an XLIFF backfill format (a new `xliff2X` channel)

1. **Skeleton producer first** — the backfill needs OPP to emit
   `<stem>.skeleton.zip` for that input format (see OPP's
   `pipeline.save_skeleton`; XLSX/EML-style sidecar maps are the pattern
   for formats whose OOXML cannot carry injected attributes — read
   `docs/dev/xliff-backfill.md` for the XLSX `xliff_map.json` and EML
   `eml_map.json` contracts).
2. **Channel module**: `src/orf/channels/xliff2X.py` — read the skeleton
   via `src/orf/skeleton/`, re-inject, refuse bare inputs with a clear
   error.
3. **MCP + CLI + docs**: add `X` to `_XLIFF_BACKFILL_FORMATS`
   (get_capabilities), wire CLI `apply-xliff --format X`, update
   `docs/dev/xliff-backfill.md`.
4. **Refusal layers must stay**: extension check, ZIP content check,
   converter `validate_input` gate (see ACCEPTED_GAPS.md "Why the
   cross-format bucket is structural") — a new channel never relaxes
   them.
5. **Scenarios + gate**: same as step 6–7 above.

## Touching PathValidator policy (allowlist / blocked extensions)

`src/orf/mcp/security.py` is the single source of truth; the policy is
mirrored across four repos (suite ADR 0007/0008). Changes must keep the
path-security parity gate green (suite CI) and are **security-relevant**:
never widen `ALLOWED_EXTENSIONS`/allowlist semantics without a
corresponding test in `tests/test_orf_security_attacks.py` and a note in
the PR describing the threat-model delta.

## Gates to run before pushing

```bash
make test                                  # full unit suite
ruff check src/orf/                        # ORF's own lint gate
python scripts/doc_inventory.py --check    # doc truth
```
