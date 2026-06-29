"""XLIFF → JSON backfill channel."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from lxml import etree

from orf.channels._json_kv import apply_opp_kv_translations

_logger = logging.getLogger(__name__)

# XLIFF 1.2 namespace
_NS = {"x": "urn:oasis:names:tc:xliff:document:1.2"}


def apply_xliff_to_json(
    xliff_path: Path,
    output_path: Path,
    skeleton_path: Path | None = None,
    original_json_path: Path | None = None,
) -> dict[str, Any]:
    """Apply XLIFF translation to produce a JSON output.

    Parses the XLIFF file, extracts each ``<trans-unit>``'s id, source,
    and target, and writes them as a JSON array to *output_path*.

    If *original_json_path* is provided, also reconstructs the original
    JSON structure with translated values injected at the matching paths,
    stored under a ``reconstructed`` key in the output.

    Args:
        xliff_path: Path to the XLIFF file.
        output_path: Path to write the JSON output.
        skeleton_path: Unused — kept for API consistency with other channels.
        original_json_path: Optional path to original JSON to reconstruct
            with translated values.

    Returns:
        Dict with ``success``, ``output_path``, and ``unit_count`` keys.
        If *original_json_path* was provided, also includes ``reconstructed``.
    """
    _parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.parse(str(xliff_path), _parser)
    units: list[dict[str, str]] = []
    translations: dict[str, str] = {}
    for trans_unit in tree.xpath("//x:trans-unit", namespaces=_NS):
        source = trans_unit.findtext("x:source", "", namespaces=_NS)
        target = trans_unit.findtext("x:target", "", namespaces=_NS)
        unit_id = trans_unit.get("id", "")
        units.append({
            "unit_id": unit_id,
            "source": source,
            "target": target,
        })
        translations[unit_id] = target
        # Build path→value map for reconstruction from json_field: targets
        if target.startswith("json_field:"):
            rest = target[len("json_field:"):]
            if " = " in rest:
                eq_idx = rest.index(" = ")
                path = rest[:eq_idx]
                val = rest[eq_idx + 3:]
                translations[path] = val

    output: dict[str, Any] = {"xliff_units": units, "unit_count": len(units)}

    if original_json_path is not None:
        original = json.loads(
            original_json_path.read_text(encoding="utf-8")
        )
        output["reconstructed"] = apply_opp_kv_translations(
            original, translations
        )

    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _logger.info(
        "Wrote %d XLIFF units to %s", len(units), output_path,
    )
    result: dict[str, Any] = {
        "success": True,
        "output_path": str(output_path),
        "unit_count": len(units),
    }
    if original_json_path is not None:
        result["reconstructed"] = True
    return result
