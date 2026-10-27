"""ORF MCP Server (standard mcp library, mcp 1.27.2).

This module is the Agent-facing MCP entry point for Omni-Re-Formatter. It
exposes 6 tools (apply_md, apply_xliff, batch_convert, detect_format, info,
ping) over the stdio MCP transport. Tool bodies dispatch to the ORF CLI via
``run_cli_command`` so the MCP server is a thin transport/auth wrapper
around the existing CLI surface — no business logic is duplicated.

The module is split into three layers, in order:

1. **Subprocess + path-safety helpers** (``common.py``): ``run_cli_command``,
   ``safe_unlink``, ``safe_temp_output``, response formatters, config +
   validator singletons.

2. **Module-level function aliases** (``tools/`` package): ``apply_md``,
   ``apply_xliff``, ``batch_convert``, ``detect_format``, ``info``, ``ping``:
   the canonical in-process API. Tests import these directly. The standard
   MCP server below wraps them via the ``_TOOL_DISPATCH`` table.

3. **Standard MCP server**: uses ``mcp.server.Server`` +
   ``mcp.server.stdio.stdio_server`` (mcp 1.27.2) — the replacement for the
   now-removed standalone ``fastmcp`` dependency which has a stdio transport
   bug. ``python -m orf.mcp.server`` starts the stdio transport and blocks
   until EOF on stdin.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# Re-export tool functions for backward-compat direct imports
# (tests import from orf.mcp.server — these re-exports keep them working).
from orf.mcp.tools import (  # noqa: F401
    apply_md,
    apply_xliff,
    batch_convert,
    detect_format,
    info,
    ping,
    get_capabilities,
)
# Re-export common helpers for backward compat (README imports
# ``_run_cli_command`` for inline testing).
from orf.mcp._errors import AUTH_FAILED, ORF_INTERNAL_ERROR, ORF_UNKNOWN_TOOL, RATE_LIMITED
from orf.mcp.common import (  # noqa: F401
    MCP_SCRUB_ENV_KEYS as _MCP_SCRUB_ENV_KEYS,
    augment_error as _augment_error,
    error_response as _error_response,
    get_path_validator as _get_path_validator,
    logger,
    orf_config as _orf_config,
    path_validator as _path_validator,
    reset_config_and_validator as _reset_config_and_validator,
    run_cli_command as _run_cli_command,
    safe_temp_output as _safe_temp_output,
    safe_unlink as _safe_unlink,
    success_response as _success_response,
)
from orf.mcp.metrics import (
    STATUS_AUTH_FAILED as _ORF_STATUS_AUTH_FAILED,
    STATUS_ERROR as _ORF_STATUS_ERROR,
    STATUS_RATE_LIMITED as _ORF_STATUS_RATE_LIMITED,
    STATUS_SUCCESS as _ORF_STATUS_SUCCESS,
    record_request_from_arguments,
    time_block as _orf_metrics_timer,
)
from orf.mcp.tracing import (
    set_span_status as _orf_tracing_set_status,
    start_call_tool_span as _orf_tracing_start_span,
    inject_traceparent as _orf_tracing_inject_traceparent,
)
from orf.mcp.health import start_health_server as _orf_health_start
# Keep PathValidator accessible at orf.mcp.server.PathValidator for
# E2E tests that patch ``orf.mcp.server.PathValidator.validate``.
from orf.mcp.security import PathValidator  # noqa: F401

# ─── Standard MCP server (mcp 1.27.2) ─────────────────────────────────


server: Server = Server("orf-mcp")


_TOOL_DISPATCH: dict[str, Any] = {
    "apply_md": apply_md,
    "apply_xliff": apply_xliff,
    "batch_convert": batch_convert,
    "detect_format": detect_format,
    "info": info,
    "ping": ping,
    "get_capabilities": get_capabilities,
}


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    """Advertise the 6 ORF MCP tools to the client.

    Schemas are written by hand (the standard ``mcp`` library does not
    auto-generate them from function signatures the way ``fastmcp`` did).
    They mirror the keyword-argument surface of the module-level function
    aliases above, so any kwarg accepted by the function is also accepted
    by the MCP tool. ``auth_token`` is intentionally omitted — the stdio
    transport is treated as a trusted boundary, and the in-process alias
    path that is also exercised by tests enforces auth via
    ``MCP_SHARED_SECRET``.
    """
    return [
        types.Tool(
            name="apply_md",
            description=(
                "Convert MD to target format. Images (from OPP images.json) are "
                "extracted during conversion into images.zip + images.json alongside "
                "the output document when separate_images=True (default). When "
                "separate_images=False, images embedded as base64 data URIs in the "
                "markdown are decoded and placed inline in the output document by "
                "Pandoc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "input_md": {
                        "type": "string",
                        "description": "Path to input markdown file. Mutually exclusive with ``content``.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Inline markdown content (text-in/text-out agent flow). Mutually exclusive with ``input_md``.",
                    },
                    "target_format": {
                        "type": "string",
                        "description": (
                            "Target format (docx, odt, epub, html, rtf, pdf, "
                            "pptx, icml, srt, csv, xlsx, xml, ipynb, eml, msg, json)."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Optional output path. Must be inside an allowed directory.",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Optional image placements (OPP images.json shape).",
                    },
                    "separate_images": {"type": "boolean", "default": True},
                    "reference_doc": {
                        "type": "string",
                        "description": "Pandoc reference DOCX for style template.",
                    },
                    "reference_doc_content": {
                        "type": "string",
                        "description": (
                            "Inline base64-encoded DOCX bytes (alternative to "
                            "reference_doc). Mutually exclusive with reference_doc."
                        ),
                    },
                    "template": {
                        "type": "string",
                        "description": "Pandoc template path.",
                    },
                    "title": {"type": "string"},
                    "author": {"type": "string"},
                    "lang": {"type": "string"},
                    "embed_images": {"type": "boolean", "default": False},
                    "text_only": {"type": "boolean", "default": False},
                    "max_file_size_mb": {"type": "number"},
                    "traceparent": {
                        "type": "string",
                        "description": "Optional W3C Trace Context traceparent header to make this ORF span a child of an upstream trace (e.g. from OL translate_md_text).",
                    },
                    "context_dir": {
                        "type": "string",
                        "description": (
                            "Optional base directory for resolving relative paths "
                            "(reference_doc, template, output_path). If omitted, "
                            "absolute paths are required."
                        ),
                    },
                },
                "anyOf": [
                    {"required": ["input_md", "target_format"]},
                    {"required": ["content", "target_format"]},
                ],
            },
        ),
        types.Tool(
            name="apply_xliff",
            description=(
                "Apply XLIFF translation to original document with optional image "
                "injection. Provide either xliff_path or xliff_content (mutually "
                "exclusive). Image placements must use data_base64 — file_path is "
                "rejected (C4 fix)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "input_file": {
                        "type": "string",
                        "description": "Path to source document (DOCX, PPTX, EPUB, ...).",
                    },
                    "xliff_path": {
                        "type": "string",
                        "description": "Path to translated XLIFF file.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Path to write the backfilled document.",
                    },
                    "format": {
                        "type": "string",
                        "description": "Output format (docx, pptx, epub, html, odf, json, xlsx, eml, ...).",
                    },
                    "xliff_content": {
                        "type": "string",
                        "description": "XLIFF body as a string (mutually exclusive with xliff_path).",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Optional image placements.",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Bypass skeleton-vs-format validation for cross-format conversions.",
                    },
                    "no_cache": {
                        "type": "boolean",
                        "default": False,
                        "description": "Skip ORF's content-addressed cache.",
                    },
                    "max_file_size_mb": {"type": "number"},
                    "traceparent": {
                        "type": "string",
                        "description": "Optional W3C Trace Context traceparent header to make this ORF span a child of an upstream trace (e.g. from OL translate_xliff).",
                    },
                },
                "required": ["input_file", "xliff_path", "output_path", "format"],
            },
        ),
        types.Tool(
            name="batch_convert",
            description="Batch convert MD files in a directory to a target format.",
            inputSchema={
                "type": "object",
                "properties": {
                    "input_dir": {
                        "type": "string",
                        "description": "Directory containing MD files to convert.",
                    },
                    "target_format": {
                        "type": "string",
                        "description": "Target format (docx, html, epub, ...).",
                    },
                    "pattern": {
                        "type": "string",
                        "default": "*.md",
                        "description": "Glob pattern for input files.",
                    },
                },
                "required": ["input_dir", "target_format"],
            },
        ),
        types.Tool(
            name="detect_format",
            description="Detect document format using magic bytes detection.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to document file.",
                    },
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="info",
            description="Get document information (format, size, resource count, manifest status).",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to document file.",
                    },
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="ping",
            description="Health check endpoint. Returns module name and version.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="get_capabilities",
            description=(
                "Return ORF module capabilities: supported MD output formats (16), "
                "XLIFF backfill formats (5), input formats, and the list of available "
                "MCP tools. Use this to discover what the server can do at runtime."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "auth_token": {"type": "string"},
                },
            },
        ),
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    """Dispatch an MCP tool call to the matching in-process function alias.

    Each alias returns a JSON-encoded string. We wrap that string in a
    single ``TextContent`` block so the client receives the same payload
    shape regardless of the underlying tool.

    Any exception raised by the alias propagates out of this handler; the
    standard ``mcp`` library catches it (because we run with
    ``raise_exceptions=False`` in ``_amain``) and surfaces it to the
    client as an ``isError=True`` response with the exception message.
    Tools that want to return a structured error payload instead of a
    traceback should catch their own exceptions and return a JSON string
    with ``success=False`` — which is what all six aliases already do.
    """
    if name not in _TOOL_DISPATCH:
        record_request_from_arguments(
            name, arguments, _ORF_STATUS_ERROR, 0.0,
        )
        with _orf_tracing_start_span(name, arguments) as _span:
            _orf_tracing_set_status(_span, "error", error_code=ORF_UNKNOWN_TOOL)
        raise ValueError(f"Unknown tool: {name}")
    fn = _TOOL_DISPATCH[name]
    timer = _orf_metrics_timer()
    _traceparent_arg = (arguments or {}).get("traceparent")
    with _orf_tracing_start_span(name, arguments, traceparent=_traceparent_arg) as _span:
        try:
            result = fn(**arguments)
        except Exception:
            record_request_from_arguments(
                name, arguments, _ORF_STATUS_ERROR, timer.seconds(),
            )
            _orf_tracing_set_status(
                _span, "error",
                error_code=ORF_INTERNAL_ERROR,
                duration_ms=timer.seconds() * 1000.0,
            )
            raise
        if name in ("apply_md", "apply_xliff") and isinstance(result, str):
            try:
                payload = json.loads(result)
            except Exception:
                logger.debug("Failed to parse result JSON for traceparent injection", exc_info=True)
                payload = None
            if isinstance(payload, dict):
                tp = _orf_tracing_inject_traceparent(_span)
                if tp is not None:
                    payload["traceparent"] = tp
                    result = json.dumps(payload, ensure_ascii=False)
        try:
            payload = json.loads(result) if isinstance(result, str) else {}
        except Exception:
            logger.debug("Failed to parse result payload JSON", exc_info=True)
            payload = {}
        status = _orf_classify_status(payload)
        record_request_from_arguments(
            name, arguments, status, timer.seconds(),
        )
        _orf_tracing_set_status(
            _span, status,
            error_code=payload.get("error_code") if isinstance(payload, dict) else None,
            duration_ms=timer.seconds() * 1000.0,
        )
        return [types.TextContent(type="text", text=result)]


def _orf_classify_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return _ORF_STATUS_ERROR
    if payload.get("success") is True:
        return _ORF_STATUS_SUCCESS
    code = payload.get("error_code")
    if not code:
        err = payload.get("error")
        if isinstance(err, dict):
            code = err.get("code")
    if code == RATE_LIMITED:
        return _ORF_STATUS_RATE_LIMITED
    if code == AUTH_FAILED:
        return _ORF_STATUS_AUTH_FAILED
    return _ORF_STATUS_ERROR


def get_server() -> Server:
    """Return the standard MCP ``Server`` instance.

    Kept for backward compatibility with callers that imported this in
    the FastMCP era. The standard ``mcp`` server is also accessible as
    the module-level ``server`` attribute; ``main()`` is the recommended
    way to actually run the stdio transport.
    """
    return server


async def _amain() -> None:
    """Run the standard MCP server over stdio until EOF on stdin."""
    _orf_health_start()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )


def main() -> None:
    """Run the ORF MCP server over stdio (CLI / pyproject script entry point)."""
    import anyio
    anyio.run(_amain)


if __name__ == "__main__":
    main()
