"""XLIFF → JSON backfill channel."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from lxml import etree

_logger = logging.getLogger(__name__)

# XLIFF 1.2 namespace
_NS = {"x": "urn:oasis:names:tc:xliff:document:1.2"}


def apply_xliff_to_json(
    xliff_path: Path,
    output_path: Path,
    skeleton_path: Path | None = None,
) -> dict[str, Any]:
    """Apply XLIFF translation to produce a JSON output.

    Parses the XLIFF file, extracts each ``<trans-unit>``'s id, source,
    and target, and writes them as a JSON array to *output_path*.

    Args:
        xliff_path: Path to the XLIFF file.
        output_path: Path to write the JSON output.
        skeleton_path: Unused — kept for API consistency with other channels.

    Returns:
        Dict with ``success``, ``output_path``, and ``unit_count`` keys.
    """
    tree = etree.parse(str(xliff_path))
    units: list[dict[str, str]] = []
    for trans_unit in tree.xpath("//x:trans-unit", namespaces=_NS):
        source = trans_unit.findtext("x:source", "", namespaces=_NS)
        target = trans_unit.findtext("x:target", "", namespaces=_NS)
        unit_id = trans_unit.get("id", "")
        units.append({
            "unit_id": unit_id,
            "source": source,
            "target": target,
        })

    output = {"xliff_units": units, "unit_count": len(units)}
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _logger.info(
        "Wrote %d XLIFF units to %s", len(units), output_path,
    )
    return {
        "success": True,
        "output_path": str(output_path),
        "unit_count": len(units),
    }
