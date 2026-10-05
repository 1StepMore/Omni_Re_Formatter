"""get_capabilities tool for ORF.

Returns module-level static information about what the ORF MCP server
can do. This is the "self-description" feature requested by agents.
"""
from __future__ import annotations

import json
from typing import Optional

from orf.mcp.auth import auth_failure_response, check_auth
from orf.mcp.rate_limiter import check_rate_limit, rate_limit_failure_response


# 16 apply-md output formats (must match apply_md.py's accept list)
_MD_OUTPUT_FORMATS = [
    "docx", "odt", "epub", "html", "rtf", "pdf", "pptx", "icml",
    "srt", "csv", "xlsx", "xml", "ipynb", "eml", "msg", "json",
]

# apply-xliff backfill formats
_XLIFF_BACKFILL_FORMATS = [
    "docx", "pptx", "epub", "html", "odf", "pdf", "json", "xlsx",
]

# All 7 MCP tools (this one included)
_TOOLS = [
    "apply_md", "apply_xliff", "batch_convert",
    "detect_format", "info", "ping", "get_capabilities",
]


def get_capabilities(auth_token: Optional[str] = None) -> str:
    """Return ORF capabilities for MCP clients.

    Returns a JSON string with:
        module (str): "orf"
        version (str | None): ORF version (best-effort)
        output_formats (list[str]): 16 MD target formats
        xliff_backfill_formats (list[str]): 7 XLIFF backfill formats
        input_formats (list[str]): ["md", "xliff"]
        tools (list[str]): 7 available MCP tool names
    """
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps(
            {**rate_limit_failure_response(), "error": rate_err},
            ensure_ascii=False,
        )
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)

    # Try to get version (best-effort; ignore if not available)
    version: str | None = None
    try:
        from orf import __version__ as _v  # type: ignore
        version = _v
    except Exception:  # expected
        pass

    return json.dumps(
        {
            "success": True,
            "content": {
                "module": "orf",
                "version": version,
                "output_formats": _MD_OUTPUT_FORMATS,
                "xliff_backfill_formats": _XLIFF_BACKFILL_FORMATS,
                "input_formats": ["md", "xliff"],
                "tools": _TOOLS,
            },
        },
        ensure_ascii=False,
    )
