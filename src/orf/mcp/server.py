"""ORF MCP Server (standard mcp library, mcp 1.27.2).

This module is the Agent-facing MCP entry point for Omni-Re-Formatter. It
exposes 6 tools (apply_md, apply_xliff, batch_convert, detect_format, info,
ping) over the stdio MCP transport. Tool bodies dispatch to the ORF CLI via
``_run_cli_command`` so the MCP server is a thin transport/auth wrapper
around the existing CLI surface — no business logic is duplicated.

The module is split into three layers, in order:

1. **Subprocess + path-safety helpers** (``_run_cli_command``,
   ``_safe_unlink``, ``_safe_temp_output``): plumbing for the CLI dispatch
   and the C3 / C5 path-allowlist fixes.

2. **Module-level function aliases** (``apply_md``, ``apply_xliff``,
   ``batch_convert``, ``detect_format``, ``info``, ``ping``): the canonical
   in-process API. Tests in ``tests/test_e2e_orf_mcp.py`` and
   ``tests/security/test_orf_mcp_auth.py`` import these directly. The
   standard MCP server below wraps them via the ``_TOOL_DISPATCH`` table.

3. **Standard MCP server** (``server``, ``_list_tools``, ``_call_tool``):
   uses ``mcp.server.Server`` + ``mcp.server.stdio.stdio_server`` (mcp
   1.27.2) — the replacement for the now-removed standalone ``fastmcp``
   dependency which has a stdio transport bug. ``python -m orf.mcp.server``
   starts the stdio transport and blocks until EOF on stdin.
"""

from __future__ import annotations

import anyio
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from orf.mcp.security import PathValidator
from orf.logging import get_logger
from orf.mcp.auth import check_auth, auth_failure_response
from orf.mcp.rate_limiter import check_rate_limit, rate_limit_failure_response
from orf.mcp.config import MCPConfig, load_config
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

# ULTRAREADY-VERIFY (2026-06-07): env vars that must NEVER be inherited
# by the CLI subprocess. These are test-only seams — if a test harness
# started the MCP server with one of them set, every MCP conversion
# would silently produce fake output. The list is centralized here so
# adding a new seam in the future means updating one constant + one test.
_MCP_SCRUB_ENV_KEYS = frozenset({
    "OMNI_TEST_FAKE_PANDOC",
    "OMNI_TEST_FAKE_LLM",
    "OMNI_TEST_FAKE",
    "OMNI_TEST_MOCK",
    "OMNI_TEST_STUB",
})

logger = get_logger("mcp.server")

# Config & validator (module-level singletons; tests patch the validator
# via ``_path_validator`` but the underlying config is read once at import).
_orf_config: MCPConfig = load_config()
_path_validator = PathValidator(
    allowed_directories=_orf_config.allowed_directories or [Path.cwd()],
    max_file_size_bytes=_orf_config.max_file_size_mb * 1024 * 1024,
)


def _error_response(code: str, message: str, **extra: Any) -> dict:
    """Standardized error response with backward-compat fields."""
    resp: dict[str, Any] = {
        "success": False,
        "error": {"code": code, "message": message},
        "error_code": code,
        "message": message,
    }
    resp.update(extra)
    return resp


def _success_response(content: dict) -> dict:
    """Standardized success response wrapping payload under ``content``."""
    return {"success": True, "content": content}


def _augment_error(resp: dict) -> dict:
    """Add top-level ``error: {code, message}`` to an existing error dict.

    Extracts code/message from ``errors[0]`` if present, preserving all
    existing fields (backward compat).
    """
    if resp.get("success") is True:
        return resp
    if "error" not in resp:
        errors = resp.get("errors", [])
        if errors and isinstance(errors[0], dict):
            resp["error"] = {
                "code": errors[0].get("code", "ORF_ERROR"),
                "message": errors[0].get("message", "Unknown error"),
            }
        else:
            resp["error"] = {"code": "ORF_ERROR", "message": "Unknown error"}
    return resp


# ─── CLI subprocess helper ─────────────────────────────────────────────


def _run_cli_command(args: list[str]) -> dict:
    """Run ORF CLI command and return parsed JSON result."""
    # ULTRAREADY-VERIFY (2026-06-07): scrub test-only env vars before
    # invoking the CLI subprocess. Without this, a test harness that
    # started the MCP server with OMNI_TEST_FAKE_PANDOC=1 would silently
    # route every MCP conversion to a stub-DOCX (the FAKE_PANDOC seam
    # in orf/cli.py:191-212 monkey-patches subprocess.run for the
    # lifetime of the CLI process).
    scrubbed_env = {
        k: v for k, v in os.environ.items()
        if k not in _MCP_SCRUB_ENV_KEYS
    }
    result = subprocess.run(
        [sys.executable, "-m", "orf.cli"] + args + ["--json"],
        capture_output=True,
        text=True,
        env=scrubbed_env,
    )

    # Bug 4 Fix: Handle empty stdout
    if not result.stdout.strip():
        logger.error(f"CLI returned empty stdout. args={args}, stderr={result.stderr[:500]}")
        return {
            "success": False,
            "output_path": None,
            "errors": [{
                "code": "EMPTY_OUTPUT",
                "message": f"CLI returned empty. stderr: {result.stderr[:500]}",
                "recovery_strategy": None
            }],
            "warnings": [],
            "metadata": {}
        }

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}. stdout: {result.stdout[:200]}")
        return {
            "success": False,
            "output_path": None,
            "errors": [{
                "code": "JSON_PARSE_ERROR",
                "message": f"JSON decode failed: {e}. Output: {result.stdout[:500]}",
                "recovery_strategy": None
            }],
            "warnings": [],
            "metadata": {}
        }

    if result.returncode == 0:
        return parsed
    else:
        # CLI failed but might have valid error JSON
        return parsed if "success" in parsed else {
            "success": False,
            "output_path": None,
            "errors": [{
                "code": "CLI_ERROR",
                "message": result.stderr or "Unknown error",
                "recovery_strategy": None
            }],
            "warnings": [],
            "metadata": {}
        }


# ─── Path safety helpers ───────────────────────────────────────────────


def _safe_unlink(path: str) -> bool:
    """Resolve+revalidate path before unlink; refuse to follow symlinks."""
    try:
        resolved = Path(path).resolve()
    except (ValueError, OSError):
        return False
    if Path(path).is_symlink():
        return False
    result = _path_validator.validate_path(str(resolved), allow_missing=True)
    if not result.success:
        return False
    try:
        os.unlink(resolved)
        return True
    except OSError:
        return False


def _safe_temp_output(suffix: str, parent: Optional[Path] = None) -> str:
    """Create a tempfile inside parent dir (must be in an allowed dir)."""
    if parent is None:
        parent = Path.cwd()
    parent_resolved = parent.resolve()
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="orf_mcp_", dir=str(parent_resolved))
    os.close(fd)
    return name


# ─── Module-level function aliases (in-process API) ────────────────────
# These wrappers are the canonical in-process call path. They duplicate
# the dispatch + security logic so they work whether or not the MCP
# server has been started (i.e., before ``main()`` is invoked, or in
# environments where the stdio transport is not in use). The standard
# MCP ``_call_tool`` handler below dispatches to the same functions
# through the ``_TOOL_DISPATCH`` table, so there is exactly one body per
# tool.
#
# Do NOT remove or rename these without updating tests:
#   - tests/test_e2e_orf_mcp.py (apply_md, apply_xliff, batch_convert,
#     detect_format, info)
#   - tests/security/test_orf_mcp_auth.py (detect_format, info)
#   - tests/observability/test_mcp_health.py (ping)
#   - tests/test_e2e_pipeline_full.py, test_e2e_real_llm.py (apply_md,
#     apply_xliff)


def apply_md(
    input_md: Optional[str] = None,
    target_format: str = "",
    output_path: Optional[str] = None,
    images: Optional[list[dict]] = None,
    separate_images: bool = True,
    reference_doc: Optional[str] = None,
    template: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    lang: Optional[str] = None,
    embed_images: bool = False,
    text_only: bool = False,
    max_file_size_mb: Optional[float] = None,
    auth_token: Optional[str] = None,
    traceparent: Optional[str] = None,
    content: Optional[str] = None,
) -> str:
    # H5: token bucket rate limiter
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps({**rate_limit_failure_response(), "error": rate_err})
    # 2026-06-18 round 16 Phase A4: MCP shared-secret auth.
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)
    """Convert MD to target format. In-process equivalent of the MCP tool."""
    # E2E-76: accept inline markdown via ``content`` alongside the
    # path-based ``input_md`` so text-in/text-out agent flows work.
    if not input_md and not content:
        return json.dumps(_augment_error({
            "success": False,
            "output_path": None,
            "errors": [{
                "code": "MISSING_INPUT",
                "message": "Either input_md (path) or content (inline markdown) is required.",
                "recovery_strategy": None,
            }],
            "warnings": [],
            "metadata": {},
        }))
    if input_md and content:
        return json.dumps(_augment_error({
            "success": False,
            "output_path": None,
            "errors": [{
                "code": "MUTUALLY_EXCLUSIVE",
                "message": "input_md and content are mutually exclusive — supply one, not both.",
                "recovery_strategy": None,
            }],
            "warnings": [],
            "metadata": {},
        }))

    content_temp_path: str | None = None
    if content is not None:
        # PathValidator requires the path to live under an allowed dir;
        # mkstemp with an explicit parent keeps it inside cwd.
        parent = Path.cwd()
        try:
            parent_resolved = parent.resolve()
            fd, content_temp_path = tempfile.mkstemp(
                suffix=".md", prefix="orf_mcp_inline_", dir=str(parent_resolved),
            )
            os.close(fd)
            with open(content_temp_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            logger.error("Failed to write inline content to tempfile: %s", e)
            return json.dumps(_augment_error({
                "success": False,
                "output_path": None,
                "errors": [{
                    "code": "INLINE_CONTENT_WRITE_FAILED",
                    "message": f"Failed to materialize inline content: {e}",
                    "recovery_strategy": None,
                }],
                "warnings": [],
                "metadata": {},
            }))
        input_md = content_temp_path

    try:
        result = _path_validator.validate_path(input_md)
        if not result.success:
            return json.dumps(_augment_error({
                "success": False,
                "output_path": None,
                "errors": [{"code": "PATH_NOT_ALLOWED", "message": result.error, "recovery_strategy": None}],
                "warnings": [],
                "metadata": {}
            }))

        if output_path:
            result_out = _path_validator.validate_path(output_path, allow_missing=True)
            if not result_out.success:
                return json.dumps(_augment_error({
                    "success": False,
                    "output_path": None,
                    "errors": [{"code": "PATH_NOT_ALLOWED", "message": f"output_path: {result_out.error}", "recovery_strategy": None}],
                    "warnings": [],
                    "metadata": {}
                }))

        for path_param, path_value in (("reference_doc", reference_doc), ("template", template)):
            if path_value:
                pv = _path_validator.validate_path(path_value, allow_missing=True)
                if not pv.success:
                    return json.dumps(_augment_error({
                        "success": False,
                        "output_path": None,
                        "errors": [{"code": "PATH_NOT_ALLOWED",
                                    "message": f"{path_param}: {pv.error}",
                                    "recovery_strategy": None}],
                        "warnings": [],
                        "metadata": {}
                    }))

        args = ["apply-md", input_md, "--target-format", target_format]
        if output_path:
            args.extend(["--output", output_path])
        if not separate_images:
            args.append("--no-separate-images")
        if reference_doc:
            args.extend(["--reference-doc", reference_doc])
        if template:
            args.extend(["--template", template])
        if title:
            args.extend(["--title", title])
        if author:
            args.extend(["--author", author])
        if lang:
            args.extend(["--lang", lang])
        if embed_images:
            args.append("--embed-images")
        if text_only:
            args.append("--text-only")
        if max_file_size_mb is not None:
            args.extend(["--max-file-size-mb", str(max_file_size_mb)])

        images_json_path: str | None = None
        if images:
            try:
                fd, images_json_path = tempfile.mkstemp(suffix=".json", prefix="orf_mcp_images_")
                os.close(fd)
                with open(images_json_path, "w") as f:
                    json.dump({"images": images}, f)
                args.extend(["--images-json", images_json_path])
            except Exception as e:
                logger.warning("Failed to write images temp file: %s", e)

        try:
            cli_result = _run_cli_command(args)
            if cli_result.get("success"):
                return json.dumps(_success_response(cli_result))
            return json.dumps(_augment_error(cli_result))
        finally:
            if images_json_path:
                _safe_unlink(images_json_path)
    finally:
        if content_temp_path:
            _safe_unlink(content_temp_path)


def apply_xliff(
    input_file: str,
    xliff_path: str,
    output_path: str,
    format: str,
    xliff_content: Optional[str] = None,
    images: Optional[list[dict]] = None,
    force: bool = False,
    no_cache: bool = False,
    max_file_size_mb: Optional[float] = None,
    auth_token: Optional[str] = None,
    traceparent: Optional[str] = None,
) -> str:
    # H5: token bucket rate limiter
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps({**rate_limit_failure_response(), "error": rate_err})
    # 2026-06-18 round 16 Phase A4: MCP shared-secret auth.
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)
    """Apply XLIFF translation. In-process equivalent of the MCP tool."""
    # C4 fix: reject image placements that carry `file_path` (arbitrary
    # file read). MCP clients must provide `data_base64` only.
    if images:
        for idx, img_dict in enumerate(images):
            if isinstance(img_dict, dict) and img_dict.get("file_path"):
                return json.dumps(_augment_error({
                    "success": False,
                    "output_path": None,
                    "errors": [{
                        "code": "FILE_PATH_NOT_ALLOWED",
                        "message": (
                            f"image[{idx}].file_path is not allowed via MCP; "
                            "supply data_base64 instead."
                        ),
                    }],
                    "warnings": [],
                    "metadata": {},
                }))
    # C5 fix: validate output_path against allowlist before subprocess
    result_out = _path_validator.validate_path(output_path, allow_missing=True)
    if not result_out.success:
        return json.dumps(_augment_error({
            "success": False,
            "output_path": None,
            "errors": [{"code": "PATH_NOT_ALLOWED", "message": f"output_path: {result_out.error}"}],
            "warnings": [],
            "metadata": {},
        }))
    if xliff_path and xliff_content:
        return json.dumps(_augment_error({
            "success": False,
            "output_path": None,
            "errors": [{"code": "MUTUALLY_EXCLUSIVE", "message": "xliff_path and xliff_content are mutually exclusive"}],
            "warnings": [],
            "metadata": {}
        }))

    xliff_to_use = xliff_path
    xliff_temp_path = None
    if xliff_content:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.xliff', delete=False)
        tmp.write(xliff_content)
        tmp.close()
        xliff_temp_path = tmp.name
        xliff_to_use = xliff_temp_path

    for p in [input_file, xliff_to_use]:
        result_p = _path_validator.validate_path(p)
        if not result_p.success:
            if xliff_temp_path:
                _safe_unlink(xliff_temp_path)
            return json.dumps(_augment_error({
                "success": False,
                "output_path": None,
                "errors": [{"code": "PATH_NOT_ALLOWED", "message": result_p.error}],
                "warnings": [],
                "metadata": {}
            }))

    args = [
        "apply-xliff", input_file,
        "--xliff", xliff_to_use,
        "--output", output_path,
        "--format", format,
    ]
    if force:
        args.append("--force")
    if no_cache:
        args.append("--no-cache")
    if max_file_size_mb is not None:
        args.extend(["--max-file-size-mb", str(max_file_size_mb)])

    temp_created = False
    temp_path = ""
    if images:
        images_data = []
        for img_dict in images:
            try:
                img_bytes = None
                if "data_base64" in img_dict and img_dict["data_base64"]:
                    img_bytes = base64.b64decode(img_dict["data_base64"])
                elif "file_path" in img_dict and img_dict["file_path"]:
                    img_bytes = Path(img_dict["file_path"]).read_bytes()

                if img_bytes:
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    img_record = {
                        "data_base64": b64,
                        "mime_type": img_dict.get("mime_type", "image/png"),
                        "width": img_dict.get("width"),
                        "height": img_dict.get("height"),
                        "paragraph_index": img_dict.get("paragraph_index"),
                        "slide_index": img_dict.get("slide_index"),
                        "page_number": img_dict.get("page_number"),
                        "element_index": img_dict.get("element_index"),
                        "spine_index": img_dict.get("spine_index"),
                    }
                    images_data.append(img_record)
            except Exception as e:
                logger.warning("Failed to process image placement: %s", e)
                continue

        if images_data:
            fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="orf_images_")
            try:
                os.write(fd, json.dumps(images_data).encode("utf-8"))
                os.close(fd)
                args.extend(["--images-json", temp_path])
                temp_created = True
            except Exception as e:
                logger.error("Failed to create temp images file: %s", e)
                os.close(fd)
                temp_created = False

    result = _run_cli_command(args)

    if temp_created:
        _safe_unlink(temp_path)

    if xliff_temp_path:
        _safe_unlink(xliff_temp_path)

    if result.get("success"):
        return json.dumps(_success_response(result))
    return json.dumps(_augment_error(result))


def batch_convert(input_dir: str, target_format: str, pattern: str = "*.md", auth_token: Optional[str] = None) -> str:
    """Batch convert MD files. In-process equivalent of the MCP tool."""
    # H5: token bucket rate limiter
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps({**rate_limit_failure_response(), "error": rate_err})
    # 2026-06-18 round 16 Phase A4: MCP shared-secret auth.
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)
    result_dir = _path_validator.validate_path(input_dir, allow_missing=True)
    if not result_dir.success:
        return json.dumps(_error_response(
            "PATH_NOT_ALLOWED", result_dir.error or "Path validation failed",
            content={"success_count": 0, "fail_count": 0, "total": 0},
        ))

    args = ["convert-batch", input_dir, "--target-format", target_format, "--pattern", pattern]
    cli_result = _run_cli_command(args)
    if cli_result.get("success"):
        return json.dumps(_success_response(cli_result))
    return json.dumps(_augment_error(cli_result))


def detect_format(file_path: str, auth_token: Optional[str] = None) -> str:
    """Detect document format. In-process equivalent of the MCP tool."""
    # H5: token bucket rate limiter
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps({**rate_limit_failure_response(), "error": rate_err})
    # 2026-06-18 round 16 Phase A4: MCP shared-secret auth.
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)
    result_df = _path_validator.validate_path(file_path)
    if not result_df.success:
        return json.dumps(_error_response(
            "PATH_NOT_ALLOWED", result_df.error or "Path validation failed",
            content={"format": "UNKNOWN", "confidence": 0.0},
        ))

    args = ["info", file_path]
    result = _run_cli_command(args)
    return json.dumps(_success_response({
        "format": result.get("format", "UNKNOWN"), "confidence": 1.0,
    }))


def info(file_path: str, auth_token: Optional[str] = None) -> str:
    """Get document information. In-process equivalent of the MCP tool."""
    # H5: token bucket rate limiter
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps({**rate_limit_failure_response(), "error": rate_err})
    # 2026-06-18 round 16 Phase A4: MCP shared-secret auth.
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)
    result_info = _path_validator.validate_path(file_path)
    if not result_info.success:
        return json.dumps(_error_response(
            "PATH_NOT_ALLOWED", result_info.error or "Path validation failed",
            content={
                "format": "UNKNOWN",
                "size_mb": 0.0,
                "resource_count": None,
                "manifest_status": "error",
            },
        ))

    cli_result = _run_cli_command(["info", file_path])
    if cli_result.get("success"):
        return json.dumps(_success_response(cli_result))
    return json.dumps(_augment_error(cli_result))


def ping(auth_token: Optional[str] = None) -> str:
    """Health check endpoint. In-process equivalent of the MCP tool."""
    # H5: token bucket rate limiter
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps({**rate_limit_failure_response(), "error": rate_err})
    # 2026-06-18 round 16 Phase A4: MCP shared-secret auth.
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)
    from orf import __version__ as _orf_version
    return json.dumps(
        _success_response({"module": "orf", "version": _orf_version}),
        ensure_ascii=False,
    )


# ─── Standard MCP server (mcp 1.27.2) ─────────────────────────────────


server: Server = Server("ORF MCP Server")


_TOOL_DISPATCH = {
    "apply_md": apply_md,
    "apply_xliff": apply_xliff,
    "batch_convert": batch_convert,
    "detect_format": detect_format,
    "info": info,
    "ping": ping,
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
                        "description": "Output format (docx, pptx, epub, html, odf, ...).",
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
            _orf_tracing_set_status(_span, "error", error_code="ORF_UNKNOWN_TOOL")
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
                error_code="ORF_INTERNAL_ERROR",
                duration_ms=timer.seconds() * 1000.0,
            )
            raise
        if name in ("apply_md", "apply_xliff") and isinstance(result, str):
            try:
                payload = json.loads(result)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                tp = _orf_tracing_inject_traceparent(_span)
                if tp is not None:
                    payload["traceparent"] = tp
                    result = json.dumps(payload, ensure_ascii=False)
        try:
            payload = json.loads(result) if isinstance(result, str) else {}
        except Exception:
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
    if code == "RATE_LIMITED":
        return _ORF_STATUS_RATE_LIMITED
    if code == "AUTH_FAILED":
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
    anyio.run(_amain)


if __name__ == "__main__":
    main()
