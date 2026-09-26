"""XLIFF parsing functions for the XLIFF2DOCX converter.

Provides standalone functions for parsing XLIFF files and extracting
inline formatting elements, used by the XLIFF2DOCXConverter class.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import unescape as _xml_unescape

from lxml import etree

from orf.error_handlers.conversion_error import XLIFFParseError
from orf.logging import get_logger
from orf.skeleton.inline_formatting import InlineElement

from ._ns import (
    XLIFF_NS_MAP_1_1,
    XLIFF_NS_MAP_1_2,
    XLIFF_NS_MAP_2_0,
)

logger = get_logger("channel.xliff2docx.parser")


def _strip_wrapper(target_text: str) -> str:
    """Return the inner text with ``<source|target ...>`` wrapper tags removed.

    Handles three cases:
    1. Whole string is one wrapper: ``<source ...>inner</source>`` → ``inner``
    2. Compound wrappers: ``a</source>\\n\\n<source>b</source>`` → ``ab``
    3. No wrapper: returned unchanged
    """
    from ._ns import _INNER_STRIP_RE
    stripped = _INNER_STRIP_RE.sub('', target_text).strip()
    if stripped != target_text.strip():
        return stripped
    return target_text


def _strip_inline_tags(target_text: str) -> str:
    """Strip ``<bx .../>`` / ``<ex .../>`` inline formatting tags from text.

    These tags are XLIFF inline markers that the LLM may reproduce literally
    in its translation output.  If they reach ``<w:t>`` the user sees raw
    ``<bx id="1" type="bold"/>`` in the final DOCX.  This function is a
    defense-in-depth safety net for every backfill path.
    """
    from ._ns import _INLINE_TAGS_RE
    return _INLINE_TAGS_RE.sub('', target_text)


_XML_ENTITY_MAP = {"&quot;": '"'}


def _target_text(target_el: etree._Element | None) -> str:
    """Extract target text, resolving one extra XML-escaping layer.

    lxml's ``itertext()`` already resolves XML text-node escaping.  When a
    producer escaped inline markup twice (``&amp;lt;bx`` in the file) the
    extraction leaves a literal ``&lt;bx``; unescaping once more restores the
    real ``<bx>``/``<ex>`` markup every downstream backfill path expects.
    """
    if target_el is None:
        return ""
    return _xml_unescape(
        _strip_wrapper("".join(target_el.itertext())),
        _XML_ENTITY_MAP,
    )


def parse_xliff(
    xliff_path: Path | str,
    inline_parser: Any,
) -> list[dict[str, Any]]:
    """Parse XLIFF file and extract trans-units.

    Args:
        xliff_path: Path to XLIFF file.
        inline_parser: An XLIFFInlineParser instance for extracting inline elements.

    Returns:
        List of trans-unit dictionaries with id, source, target.

    Raises:
        XLIFFParseError: If XLIFF cannot be parsed.
    """
    path = Path(xliff_path)
    if not path.exists():
        raise XLIFFParseError(
            str(path),
            f"File not found: {path}",
        )

    try:
        tree = etree.parse(str(path), etree.XMLParser(recover=True))
        root = tree.getroot()
    except etree.XMLSyntaxError as e:
        raise XLIFFParseError(str(path), f"XML parse error: {e}")

    if root is None:
        raise XLIFFParseError(str(path), "Failed to parse XML - no root element")

    trans_units: list[dict[str, Any]] = []

    # Auto-detect XLIFF namespace from root element
    root_ns_uri = root.namespaceURI() if hasattr(root, 'namespaceURI') else ""
    if "xliff" in root_ns_uri.lower():
        if "1.1" in root_ns_uri:
            ns_map = XLIFF_NS_MAP_1_1
            logger.debug("Detected XLIFF 1.1 namespace from root element")
        else:
            ns_map = XLIFF_NS_MAP_1_2
            logger.debug("Detected XLIFF namespace from root element: %s", root_ns_uri)
    else:
        ns_map = XLIFF_NS_MAP_1_2
        logger.debug("Using XLIFF namespace 1.2 (fallback)")

    # Try xliff 1.2 trans-unit elements
    for tu in root.xpath("//xliff:trans-unit", namespaces=ns_map):
        _process_trans_unit(tu, ns_map, inline_parser, trans_units)

    # Try xliff 1.1 trans-unit if no 1.2 units found
    if not trans_units and ns_map == XLIFF_NS_MAP_1_2:
        for tu in root.xpath(
            "//xliff:trans-unit",
            namespaces=XLIFF_NS_MAP_1_1,
        ):
            _process_trans_unit(tu, XLIFF_NS_MAP_1_1, inline_parser, trans_units)
        if trans_units:
            logger.debug("Found %d trans-units using XLIFF 1.1 namespace", len(trans_units))

    # Try xliff 2.0 unit elements if no 1.2/1.1 trans-units found
    if not trans_units:
        for unit in root.xpath(
            "//xliff:unit",
            namespaces=XLIFF_NS_MAP_2_0,
        ):
            unit_id = unit.get("id")
            if not unit_id:
                continue

            # xliff 2.0 uses segment elements inside unit
            segments = unit.findall("xliff:segment", namespaces=XLIFF_NS_MAP_2_0)
            if segments:
                for seg in segments:
                    seg_id = seg.get("id", unit_id)
                    source_el = seg.find("xliff:source", namespaces=XLIFF_NS_MAP_2_0)
                    target_el = seg.find("xliff:target", namespaces=XLIFF_NS_MAP_2_0)

                    source_text = (
                        "".join(source_el.itertext()) if source_el is not None else ""
                    )
                    target_text = _target_text(target_el)

                    source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                    inline_elements = extract_inline_elements_from_xml(source_xml, inline_parser)

                    resname = seg.get("resname")
                    para_index, non_body_index = _parse_resname(resname)

                    trans_units.append({
                        "id": seg_id,
                        "source": source_text,
                        "target": target_text,
                        "inline_elements": inline_elements,
                        "para_index": para_index,
                        "non_body_index": non_body_index,
                    })
            else:
                # No segments, treat whole unit as one trans-unit
                source_el = unit.find("xliff:source", namespaces=XLIFF_NS_MAP_2_0)
                target_el = unit.find("xliff:target", namespaces=XLIFF_NS_MAP_2_0)

                source_text = (
                    "".join(source_el.itertext()) if source_el is not None else ""
                )
                target_text = _target_text(target_el)

                source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                inline_elements = extract_inline_elements_from_xml(source_xml, inline_parser)

                resname = unit.get("resname")
                para_index, non_body_index = _parse_resname(resname)

                trans_units.append({
                    "id": unit_id,
                    "source": source_text,
                    "target": target_text,
                    "inline_elements": inline_elements,
                    "para_index": para_index,
                    "non_body_index": non_body_index,
                })

    logger.debug(f"Parsed {len(trans_units)} trans-units from {path}")
    return trans_units


def _process_trans_unit(
    tu: etree._Element,
    ns_map: dict[str, str],
    inline_parser: Any,
    trans_units: list[dict[str, Any]],
) -> None:
    """Process a single XLIFF 1.2/1.1 trans-unit element."""
    tu_id = tu.get("id")
    if not tu_id:
        return

    source_el = tu.find("xliff:source", namespaces=ns_map)
    target_el = tu.find("xliff:target", namespaces=ns_map)

    source_text = (
        "".join(source_el.itertext()) if source_el is not None else ""
    )
    target_text = _target_text(target_el)

    # Extract inline elements from source
    source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
    inline_elements = extract_inline_elements_from_xml(source_xml, inline_parser)

    resname = tu.get("resname")
    para_index, non_body_index = _parse_resname(resname)

    trans_units.append({
        "id": tu_id,
        "source": source_text,
        "target": target_text,
        "inline_elements": inline_elements,
        "para_index": para_index,
        "non_body_index": non_body_index,
    })


def _parse_resname(resname: str | None) -> tuple[int | None, int | None]:
    """Parse resname attribute for para_index or non_body_index.

    Args:
        resname: The resname attribute value, or None.

    Returns:
        Tuple of (para_index, non_body_index).
    """
    para_index = None
    non_body_index = None
    if resname:
        if resname.startswith("para_index_"):
            try:
                para_index = int(resname[len("para_index_"):])
            except ValueError:
                para_index = None
        elif resname.startswith("non_body_"):
            try:
                non_body_index = int(resname[len("non_body_"):])
            except ValueError:
                non_body_index = None
    return para_index, non_body_index


def extract_inline_elements_from_xml(
    source_xml: str,
    inline_parser: Any,
) -> list[InlineElement]:
    """Extract inline elements from XLIFF source XML.

    Args:
        source_xml: Source element XML as string.
        inline_parser: An XLIFFInlineParser instance.

    Returns:
        List of InlineElement objects.
    """
    if not source_xml:
        return []

    try:
        elements = inline_parser.parse_source(source_xml)
        mapped = inline_parser.map_elements_to_positions(source_xml, elements)
        return mapped
    except Exception as e:
        logger.warning(f"Failed to parse inline elements: {e}")
        return []
