"""Regression test for ORF#104 — MCP parameter-level descriptions.

Every property advertised in ``tools/list`` ``inputSchema.properties`` must
carry a non-empty ``description``, so agents can fill parameters correctly
without reading the source (ORF#104: 9 params were schema-only, with no
description at the property level while top-level tool descriptions were
complete).
"""

from __future__ import annotations

import asyncio

from orf.mcp.server import _list_tools


def test_every_mcp_tool_param_has_description():
    tools = asyncio.run(_list_tools())
    assert tools, "tools/list must advertise at least one tool"
    for tool in tools:
        props = (tool.inputSchema or {}).get("properties") or {}
        for prop_name, prop in props.items():
            desc = (prop or {}).get("description")
            assert isinstance(desc, str) and desc.strip(), (
                f"{tool.name}.{prop_name} is missing a parameter-level "
                f"description in inputSchema (ORF#104 regression)."
            )
