"""XLIFF→DOCX backfill with inline formatting preservation."""

from __future__ import annotations

import base64
import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from orf.converters.base import BaseConverter, ConversionResult
from orf.error_handlers.conversion_error import XLIFFParseError
from orf.logging import get_logger
from orf.mcp.schemas import ImagePlacement
from orf.skeleton.inline_formatting import (
    DOCXInlineApplier,
    InlineElement,
    XLIFFInlineParser,
)
from orf.skeleton.skeleton_loader import SkeletonLoader
from orf.converters.options import ConverterOptions

logger = get_logger("channel.xliff2docx")

        # XLIFF namespaces
XLIFF_NS_1_2 = "urn:oasis:names:tc:xliff:document:1.2"
XLIFF_NS_1_1 = "urn:oasis:names:tc:xliff:document:1.1"
XLIFF_NS_2_0 = "urn:oasis:names:tc:xliff:document:2.0"
XLIFF_NS_MAP_1_2 = {"xliff": XLIFF_NS_1_2}
XLIFF_NS_MAP_1_1 = {"xliff": XLIFF_NS_1_1}
XLIFF_NS_MAP_2_0 = {"xliff": XLIFF_NS_2_0}
XLIFF_NS = XLIFF_NS_1_2  # Default to 1.2 for backwards compatibility
XLIFF_NS_MAP = XLIFF_NS_MAP_1_2

# Drawing namespaces
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WORD_NS_MAP = {"w": W_NS}

# C13 fix: OOXML DrawingML allows these values for relativeFrom attributes.
# Anything else is rejected at the channel boundary.
ALLOWED_RELATIVE_FROM: frozenset[str] = frozenset({
    "page",
    "column",
    "margin",
    "paragraph",
    "line",
    "character",
})

# ORF-2: minimum SequenceMatcher ratio (0.0-1.0) for fuzzy paragraph match
# when the OPP source has been rephrased by the LLM (punctuation, word
# order, dropped articles). Lower = more permissive but more false matches.
FUZZY_MATCH_THRESHOLD = 0.70

# Phase A.2: permissive wrapper-strip regex. 30.3% of slim units had
# the LLM echo `<source xmlns=...>...</source>` around the translation.
# Some LLM outputs are compound: multiple `<source>...</source>` segments
# concatenated. The strip removes all of them.
_INNER_STRIP_RE = re.compile(
    r'<\s*/?\s*(?:source|target)\b[^>]*>',
    re.DOTALL,
)


def _strip_wrapper(target_text: str) -> str:
    """Return the inner text with `<source|target ...>` wrapper tags removed.

    Handles three cases:
    1. Whole string is one wrapper: `<source ...>inner</source>` → `inner`
    2. Compound wrappers: `a</source>\n\n<source>b</source>` → `ab`
    3. No wrapper: returned unchanged
    """
    stripped = _INNER_STRIP_RE.sub('', target_text).strip()
    # Only return stripped version if it differs (no-op for plain text)
    if stripped != target_text.strip():
        return stripped
    return target_text


# Phase A.2.a: regex to strip <bx>/<ex> inline formatting tags that may leak
# from the LLM output into the final DOCX <w:t> elements.
_INLINE_TAGS_RE = re.compile(r'</?(?:bx|ex)\b[^>]*/?>', re.DOTALL)


def _strip_inline_tags(target_text: str) -> str:
    """Strip ``<bx .../>`` / ``<ex .../>`` inline formatting tags from text.

    These tags are XLIFF inline markers that the LLM may reproduce literally
    in its translation output.  If they reach ``<w:t>`` the user sees raw
    ``<bx id="1" type="bold"/>`` in the final DOCX.  This function is a
    defense-in-depth safety net for every backfill path.

    Examples::

        _strip_inline_tags('<bx id="1" type="bold"/>hello')  → 'hello'
        _strip_inline_tags('hello<ex id="1"/>')              → 'hello'
        _strip_inline_tags('<bx/>A<ex/><bx/>B<ex/>')         → 'AB'
        _strip_inline_tags('plain text')                     → 'plain text'

    Idempotent — calling twice produces the same result.
    """
    return _INLINE_TAGS_RE.sub('', target_text)


def _distribute_text_across_runs(runs: list, target_text: str) -> None:
    """Distribute target_text across runs, preserving newlines.

    Instead of flattening all text into runs[0] and clearing the rest
    (which loses ``\\n``), this helper splits on newlines and distributes
    one line per run.  If there are more lines than runs, extra lines are
    appended (with ``\\n``) to the last run.  If there are more runs than
    lines, the tail runs are cleared.

    ORF#12: fixes newline flattening in xliff2docx backfill.
    """
    lines = target_text.split('\n')
    for i, line in enumerate(lines):
        if i < len(runs):
            runs[i].text = line
        else:
            runs[-1].text += '\n' + line
    for r in runs[len(lines):]:
        r.text = ""


@dataclass
class XLIFFTransUnitData:
    """Minimal trans-unit data extracted from XLIFF."""

    id: str
    source: str
    target: Optional[str] = None
    segments: list[str] | None = None


@dataclass
class InlineElementData:
    """Inline element data for backfill."""

    id: str
    type: str
    begin_pos: int
    end_pos: int
    text_covered: Optional[str] = None


class XLIFF2DOCXConverter(BaseConverter):
    """XLIFF→DOCX backfill with inline formatting preservation.

    Loads a DOCX skeleton, parses XLIFF translation units, and backfills
    the target translations with inline formatting preserved.
    """

    def __init__(
        self,
        manifest: Optional[Any] = None,
        frontmatter: Optional[Any] = None,
    ):
        super().__init__(manifest, frontmatter)
        self.skeleton_loader = SkeletonLoader()
        self.inline_parser = XLIFFInlineParser()
        self.docx_applier = DOCXInlineApplier()

    @property
    def supported_format(self) -> str:
        return "DOCX"

    def validate_input(self, input_skeleton: Path | str) -> bool:
        """Validate input skeleton file."""
        path = Path(input_skeleton)
        return path.exists() and path.suffix.lower() in (".docx", ".zip")

    def _parse_xliff(self, xliff_path: Path | str) -> list[dict[str, Any]]:
        """Parse XLIFF file and extract trans-units.

        Args:
            xliff_path: Path to XLIFF file.

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
        # Check namespace-uri() of root to determine if 1.1 or 1.2
        root_ns_uri = root.namespaceURI() if hasattr(root, 'namespaceURI') else ""
        if "xliff" in root_ns_uri.lower():
            if "1.1" in root_ns_uri:
                ns_map = XLIFF_NS_MAP_1_1
                logger.debug("Detected XLIFF 1.1 namespace from root element")
            else:
                ns_map = XLIFF_NS_MAP_1_2
                logger.debug("Detected XLIFF namespace from root element: %s", root_ns_uri)
        else:
            # Fallback to 1.2 then 1.1
            ns_map = XLIFF_NS_MAP_1_2
            logger.debug("Using XLIFF namespace 1.2 (fallback)")

        # Handle both xliff 1.2 and 2.0 formats
        # xliff 1.2: /xliff/file/body/trans-unit
        # xliff 2.0: /xliff/ns:file/ns:unit

        # Try xliff 1.2 trans-unit elements
        for tu in root.xpath(
            "//xliff:trans-unit",
            namespaces=ns_map,
        ):
            tu_id = tu.get("id")
            if not tu_id:
                continue

            source_el = tu.find("xliff:source", namespaces=ns_map)
            target_el = tu.find("xliff:target", namespaces=ns_map)

            source_text = (
                "".join(source_el.itertext()) if source_el is not None else ""
            )
            target_text = _strip_wrapper(
                "".join(target_el.itertext()) if target_el is not None else ""
            )

            # Extract inline elements from source
            source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
            inline_elements = self._extract_inline_elements(source_xml)

            resname = tu.get("resname")
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

            trans_units.append({
                "id": tu_id,
                "source": source_text,
                "target": target_text,
                "inline_elements": inline_elements,
                "para_index": para_index,
                "non_body_index": non_body_index,
            })

        # Try xliff 1.1 trans-unit if no 1.2 units found
        if not trans_units and ns_map == XLIFF_NS_MAP_1_2:
            for tu in root.xpath(
                "//xliff:trans-unit",
                namespaces=XLIFF_NS_MAP_1_1,
            ):
                tu_id = tu.get("id")
                if not tu_id:
                    continue

                source_el = tu.find("xliff:source", namespaces=XLIFF_NS_MAP_1_1)
                target_el = tu.find("xliff:target", namespaces=XLIFF_NS_MAP_1_1)

                source_text = (
                    "".join(source_el.itertext()) if source_el is not None else ""
                )
                target_text = _strip_wrapper(
                    "".join(target_el.itertext()) if target_el is not None else ""
                )

                source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                inline_elements = self._extract_inline_elements(source_xml)

                resname = tu.get("resname")
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

                trans_units.append({
                    "id": tu_id,
                    "source": source_text,
                    "target": target_text,
                    "inline_elements": inline_elements,
                    "para_index": para_index,
                    "non_body_index": non_body_index,
                })
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

                # xliff 2.0 uses segement elements inside unit
                segments = unit.findall("xliff:segment", namespaces=XLIFF_NS_MAP_2_0)
                if segments:
                    for seg in segments:
                        seg_id = seg.get("id", unit_id)
                        source_el = seg.find("xliff:source", namespaces=XLIFF_NS_MAP_2_0)
                        target_el = seg.find("xliff:target", namespaces=XLIFF_NS_MAP_2_0)

                        source_text = (
                            "".join(source_el.itertext()) if source_el is not None else ""
                        )
                        target_text = _strip_wrapper(
                            "".join(target_el.itertext()) if target_el is not None else ""
                        )

                        source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                        inline_elements = self._extract_inline_elements(source_xml)

                        resname = seg.get("resname")
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
                    target_text = _strip_wrapper(
                        "".join(target_el.itertext()) if target_el is not None else ""
                    )

                    source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                    inline_elements = self._extract_inline_elements(source_xml)

                    resname = unit.get("resname")
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

    def _extract_inline_elements(self, source_xml: str) -> list[InlineElement]:
        """Extract inline elements from XLIFF source XML.

        Args:
            source_xml: Source element XML as string.

        Returns:
            List of InlineElement objects.
        """
        if not source_xml:
            return []

        # Use the inline parser to extract elements from the XML
        try:
            elements = self.inline_parser.parse_source(source_xml)
            # Map to text positions
            mapped = self.inline_parser.map_elements_to_positions(source_xml, elements)
            return mapped
        except Exception as e:
            logger.warning(f"Failed to parse inline elements: {e}")
            return []

    def convert(  # type: ignore[override]
        self,
        input_path: Path | str,
        xliff_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        """Convert XLIFF + skeleton to DOCX.

        Args:
            input_path: Path to skeleton DOCX file.
            xliff_path: Path to the XLIFF translation file.
            output_path: Path to output DOCX file.
            options: Converter options (segment_mapping, etc.).

        Returns:
            ConversionResult with output path and metadata.
        """
        input_skeleton = Path(input_path)
        xliff_path = Path(xliff_path)
        output_path = Path(output_path)

        warnings: list[str] = []
        inline_elements_applied = 0

        # 1. Load skeleton (original DOCX ZIP)
        try:
            skeleton_data = self.skeleton_loader.load_skeleton(str(input_skeleton))
            document_xml = skeleton_data.get("xml")
            if document_xml is None:
                skeleton_kind = (
                    "pptx" if "slides" in skeleton_data else
                    "epub" if "opf" in skeleton_data else
                    "unknown"
                )
                return ConversionResult(
                    output_path=output_path,
                    success=False,
                    errors=[
                        f"Skeleton is {skeleton_kind} format, not DOCX. "
                        f"Cross-format XLIFF (e.g. PPTX→DOCX) is not supported by this converter."
                    ],
                )
        except Exception as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to load skeleton: {e}"],
            )

        # 2. Parse XLIFF
        try:
            trans_units = self._parse_xliff(xliff_path)
        except XLIFFParseError as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"XLIFF parse error: {e}"],
            )

        if not trans_units:
            warnings.append("No trans-units found in XLIFF file")
            final_xml = document_xml
        else:
            # 3. For each trans-unit, backfill target text. Log every 5% so
            # interactive users see incremental progress.
            # Phase A.1: parse once, mutate root in place, serialize once
            # at the end — eliminates O(N×D) parse+serialize per trans-unit.
            total = len(trans_units)
            log_every = max(50, total // 20) if total else 1
            root = etree.fromstring(document_xml.encode("utf-8"))
            body = root.find("w:body", WORD_NS_MAP)
            body_paragraphs = body.xpath("./w:p", namespaces=WORD_NS_MAP)

            # Phase B.3 setup: build a flat paragraph list matching OPP's
            # extraction order (body paragraphs first, then table cells,
            # then textboxes deduplicated). OPP assigns non_body_N as the
            # GLOBAL index in result.paragraphs (not a non-body-only index).
            # We mirror that flat list here so non_body_idx maps directly.
            #
            # OPP's extract_paragraphs order:
            #   1. doc.paragraphs     — body-level <w:p> (non-empty only)
            #   2. table cells        — body//w:tc//w:p
            #   3. textboxes          — body//w:txbxContent//w:p (deduped)
            all_paragraphs = self._collect_all_paragraphs(root)
            # Also keep body_paragraphs for _backfill_translation text matching.
            body_paragraphs = body.xpath("./w:p", namespaces=WORD_NS_MAP)

            # ── O(P+M) lookup indexes: built ONCE before the trans-unit loop ──
            # Pattern C: exact match on individual <w:t> elements
            wt_text_map: dict[str, etree._Element] = {}
            for t_elem in root.xpath("//w:t", namespaces=WORD_NS_MAP):
                if t_elem.text:
                    t_norm = re.sub(r"\s+", " ", t_elem.text).strip()
                    if t_norm:
                        wt_text_map[t_norm] = t_elem

            # Pattern A: exact match on body paragraphs
            body_paragraph_text_map: dict[str, etree._Element] = {}
            for p in body_paragraphs:
                text_runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
                text_content = "".join(t.text or "" for t in text_runs)
                normalized = re.sub(r"\s+", " ", text_content).strip()
                if normalized:
                    body_paragraph_text_map[normalized] = p

            # Pattern B: exact match on all paragraphs (for inline-elements path)
            all_paragraph_text_map: dict[str, etree._Element] = {}
            for p in root.xpath("//w:p", namespaces=WORD_NS_MAP):
                text_runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
                text_content = "".join(t.text or "" for t in text_runs)
                normalized = re.sub(r"\s+", " ", text_content).strip()
                if normalized:
                    all_paragraph_text_map[normalized] = p

            # Build a mapping from original Chinese text → target text for
            # textbox paragraphs, so we can also translate their Fallback-branch
            # counterparts (which are skipped by the text-based dedup).
            chinese_to_target: dict[str, str] = {}

            for idx, tu in enumerate(trans_units, 1):
                tu_id = tu["id"]
                target_text = tu.get("target", "")
                inline_elements = tu.get("inline_elements", [])

                if not target_text:
                    continue

                # Phase B.2: position-based lookup via resname="para_index_N".
                # Robust against LLM rephrasing and whitespace drift.
                if tu.get("para_index") is not None:
                    try:
                        self._backfill_by_position(
                            root, body_paragraphs,
                            tu["para_index"],
                            target_text,
                        )
                        continue
                    except Exception as e:
                        logger.warning(
                            f"Position-based backfill failed for unit {tu_id} "
                            f"at index {tu['para_index']}: {e}; "
                            "falling back to text matching"
                        )

                # Phase B.3: non_body_N position-based lookup. OPP assigns
                # non_body_N as the GLOBAL index in result.paragraphs (flat
                # list: body → table cells → textboxes deduped). We mirror
                # that flat list in all_paragraphs and index directly.
                non_body_idx = tu.get("non_body_index")
                if non_body_idx is not None:
                    if 0 <= non_body_idx < len(all_paragraphs):
                        # Store original Chinese text before backfill modifies it,
                        # so Fallback-branch textbox paragraphs can be translated too.
                        if non_body_idx >= 9:
                            para_txbx = all_paragraphs[non_body_idx]
                            orig_zh = "".join(
                                t.text or "" for t in para_txbx.iter(f"{{{W_NS}}}t")
                            ).strip()
                            if orig_zh and orig_zh not in chinese_to_target:
                                chinese_to_target[orig_zh] = target_text
                        self._backfill_by_non_body_position(
                            all_paragraphs,
                            non_body_idx,
                            target_text,
                        )
                        continue
                    else:
                        logger.warning(
                            f"non_body index {non_body_idx} out of range for "
                            f"unit {tu_id} (have {len(all_paragraphs)} total "
                            f"paragraphs); falling back to text matching"
                        )

                try:
                    self._backfill_translation(
                        root, body_paragraphs,
                        tu["source"],
                        target_text,
                        inline_elements,
                        wt_text_map,
                        body_paragraph_text_map,
                        all_paragraph_text_map,
                    )
                except Exception as e:
                    logger.warning(f"Failed to backfill trans-unit {tu_id}: {e}")
                    warnings.append(f"Failed to backfill unit {tu_id}: {e}")

                if idx % log_every == 0 or idx == total:
                    logger.info(
                        f"ORF backfill progress: {idx}/{total} units "
                        f"({idx/total*100:.0f}%)"
                    )
            if chinese_to_target:
                logger.info(
                    "Backfilling %d Fallback-branch textbox paragraphs",
                    len(chinese_to_target),
                )
                self._backfill_fallback_textboxes(root, chinese_to_target)
            final_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                + etree.tostring(root, encoding="unicode")
            )

        # 4. Repack as DOCX
        try:
            self.skeleton_loader.repack_docx(str(output_path), final_xml)
        except Exception as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to repack DOCX: {e}"],
            )

        return ConversionResult(
            output_path=output_path,
            success=True,
            warnings=warnings if warnings else [],
            metadata={
                "inline_elements_applied": inline_elements_applied,
                "trans_units_processed": len(trans_units),
            },
        )

    def _backfill_translation(
        self,
        root: etree._Element,
        body_paragraphs: list[etree._Element],
        source_text: str,
        target_text: str,
        inline_elements: list[InlineElement],
        wt_text_map: dict[str, etree._Element],
        body_paragraph_text_map: dict[str, etree._Element],
        all_paragraph_text_map: dict[str, etree._Element],
    ) -> bool:
        r"""Backfill a single translation into the parsed document root.

        Mutates ``root`` in place. Returns True if a match was applied.

        POST_MORTEM ORF-2: matching is now layered:
          1. Try exact match via pre-built O(1) lookup indexes.
          2. Try SequenceMatcher ratio >= FUZZY_MATCH_THRESHOLD for cases
             where the LLM rephrased (punctuation, missing/extra words).
          3. Last resort: skip so we never clobber unrelated content.
        """
        if not source_text and not target_text:
            return False

        found = False
        source_stripped = re.sub(r'<[^>]+>', '', source_text) if source_text else ""
        source_normalized = re.sub(r"\s+", " ", source_stripped).strip()

        if inline_elements:
            found = self._backfill_with_inline_elements(
                root, source_normalized, target_text, inline_elements,
                all_paragraph_text_map,
            )
        else:
            # Pattern C: O(1) exact match on individual <w:t> elements
            t_elem = wt_text_map.get(source_normalized)
            if t_elem is None:
                t_elem = wt_text_map.get(source_stripped)
            if t_elem is not None:
                found = True
                t_elem.text = target_text

        if not found:
            # Pattern A: O(1) exact match on body paragraphs
            p = body_paragraph_text_map.get(source_normalized)
            if p is None:
                p = body_paragraph_text_map.get(source_stripped)
            if p is not None:
                found = self._backfill_split_runs(p, source_normalized, target_text)

        if not found:
            found = self._fuzzy_backfill(root, source_normalized, target_text)

        if not found:
            self._fallback_backfill(root, target_text)

        return found

    def _fuzzy_backfill(
        self,
        root: etree._Element,
        source_normalized: str,
        target_text: str,
        threshold: float = FUZZY_MATCH_THRESHOLD,
    ) -> bool:
        """Apply target_text to the paragraph whose text is most similar to
        source_normalized. Returns True if applied.

        Uses three layered signals, in order:
          1. SequenceMatcher ratio on whitespace-normalized text.
          2. CJK character-set Jaccard similarity (catches OPP source /
             docx text that group characters differently but share the
             same characters).
          3. Plain SequenceMatcher ratio without normalization (catches
             "no whitespace at all" matches).
        """
        import difflib
        paragraphs = root.xpath("//w:p", namespaces=WORD_NS_MAP)
        cjk_re = __import__("re").compile(r"[\u4e00-\u9fff]")
        source_chars = set(cjk_re.findall(source_normalized))

        best_para = None
        best_score = 0.0
        for p in paragraphs:
            runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
            para_text = "".join(r.text or "" for r in runs)
            para_norm = re.sub(r"\s+", " ", para_text).strip()
            if not para_norm or not source_normalized:
                continue
            sm_ratio = difflib.SequenceMatcher(None, source_normalized, para_norm).ratio()
            para_chars = set(cjk_re.findall(para_norm))
            jaccard = (
                len(source_chars & para_chars) / len(source_chars | para_chars)
                if (source_chars | para_chars) else 0.0
            )
            raw_ratio = difflib.SequenceMatcher(None, source_normalized, para_text).ratio()
            score = max(sm_ratio, jaccard, raw_ratio)
            if score > best_score:
                best_score = score
                best_para = p

        if best_para is not None and best_score >= threshold:
            runs = best_para.xpath(".//w:t", namespaces=WORD_NS_MAP)
            if not runs:
                return False
            para_text = "".join(r.text or "" for r in runs)
            if len(para_text.strip()) < 4:
                return False
            _distribute_text_across_runs(runs, target_text)
            return True
        return False

    def _backfill_by_position(
        self,
        root: etree._Element,
        body_paragraphs: list[etree._Element],
        para_index: int,
        target_text: str,
    ) -> bool:
        """Phase B.2: apply target_text to the w:p at the given index.

        Used when the OPP source has resname="para_index_N". This is
        a deterministic, position-based lookup that bypasses all the
        text-matching heuristics.

        NOTE: resname uses the index of the para among body DIRECT
        children (set by OPP via `list(body).index(para._element)`).
        We must use the same indexing here — `//w:p` would include
        nested w:p (tables, text boxes) and shift the index.

        BX/EX safety: if target_text contains ``<bx .../>`` / ``<ex .../>``
        tags (from LLM literal reproduction), they are parsed into proper
        DOCX ``<w:rPr>`` formatted runs via ``_build_formatted_runs``.
        Plain text without tags is written directly as before.
        """
        if not (0 <= para_index < len(body_paragraphs)):
            logger.warning(
                "resname para_index=%d out of range (have %d body-level paragraphs)",
                para_index, len(body_paragraphs),
            )
            return False
        para = body_paragraphs[para_index]
        runs = para.xpath(".//w:t", namespaces=WORD_NS_MAP)
        if not runs:
            return False

        # BX/EX leak fix: parse inline tags into formatted DOCX runs
        formatted_runs = self._build_formatted_runs(target_text)
        has_real_inline_tags = bool(
            re.search(
                r"<\s*/?\s*bx\b|<\s*/?\s*ex\b",
                target_text,
            )
        )
        if formatted_runs and has_real_inline_tags:
            target_run = runs[0]
            parent = target_run.getparent()
            if parent is not None:
                W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                w_tag = f"{{{W_NS}}}"
                insert_pos = list(parent.getparent()).index(parent) if parent.getparent() is not None else -1
                if insert_pos >= 0:
                    parent_para = parent.getparent()
                    for i, fr in enumerate(formatted_runs):
                        parent_para.insert(
                            list(parent_para).index(parent) + i,
                            fr,
                        )
                    # lxml: setting .text on the parent w:r does NOT
                    # clear text in child w:t nodes. Set the w:t directly.
                    target_run.text = ""
                    for r in runs[1:]:
                        r.text = ""
                    return True
                # Fallback: strip tags if parent manipulation fails
                runs[0].text = _strip_inline_tags(target_text)
                for r in runs[1:]:
                    r.text = ""
                return True

        # No bx/ex tags — plain text path (original behavior)
        _distribute_text_across_runs(runs, target_text)
        return True

    def _collect_all_paragraphs(
        self, root: etree._Element
    ) -> list[etree._Element]:
        """Collect ALL w:p elements mirroring OPP's result.paragraphs order.

        OPP's extract_paragraphs builds result.paragraphs in this exact order:
          1. Body-level <w:p> (via doc.paragraphs, non-empty only)
          2. Table cell <w:p> (via body//w:tc//w:p)
          3. Textbox <w:p> (via body//w:txbxContent//w:p, deduplicated by text)

        non_body_N in the XLIFF resname is the GLOBAL index into this flat
        list, NOT an index into non-body-only paragraphs. By mirroring the
        same order here, we can use non_body_idx as a direct index.

        Returns:
            List of w:p elements in OPP extraction order.
        """
        paragraphs: list[etree._Element] = []
        W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        w_tag = f"{{{W_NS}}}"
        seen_texts: set[str] = set()
        body = root.find("w:body", WORD_NS_MAP)

        # 1. Body-level <w:p> — non-empty only, matching OPP's
        #    doc.paragraphs filtering (``if not full_text: continue``).
        for p in body.xpath("./w:p", namespaces=WORD_NS_MAP):
            text = "".join(
                t.text or ""
                for t in p.iter(f"{w_tag}t")
                if not any(
                    anc.tag == f"{w_tag}txbxContent"
                    for anc in t.iterancestors()
                )
            ).strip()
            if text:
                paragraphs.append(p)

        # 2. Table cell paragraphs — matching OPP's _walk_table_paragraphs.
        for tc in root.iter(f"{w_tag}tc"):
            for p in tc.iter(f"{w_tag}p"):
                paragraphs.append(p)

        # 3. Textbox paragraphs — deduplicated by text content, matching
        #    OPP's _walk_textbox_paragraphs (skips mc:AlternateContent dupes).
        for txbx in root.iter(f"{w_tag}txbxContent"):
            for p in txbx.iter(f"{w_tag}p"):
                text = "".join(
                    t.text or "" for t in p.iter(f"{w_tag}t")
                ).strip()
                if text and text not in seen_texts:
                    seen_texts.add(text)
                    paragraphs.append(p)

        return paragraphs

    def _backfill_by_non_body_position(
        self,
        paragraphs: list[etree._Element],
        para_idx: int,
        target_text: str,
    ) -> bool:
        """Phase B.3: apply target_text to the w:p at the given flat-list index.

        Used when the OPP source has resname="non_body_N". The index is the
        GLOBAL position in OPP's flat result.paragraphs list (body paragraphs
        first, then table cells, then textboxes deduplicated).

        BX/EX safety: same as ``_backfill_by_position`` — inline tags are
        parsed into proper ``<w:rPr>`` formatted runs.
        """
        if not (0 <= para_idx < len(paragraphs)):
            logger.warning(
                "non_body paragraph index %d out of range "
                "(have %d total paragraphs)",
                para_idx, len(paragraphs),
            )
            return False
        para = paragraphs[para_idx]
        runs = para.xpath("./w:r/w:t", namespaces=WORD_NS_MAP)
        if not runs:
            return False

        # BX/EX leak fix: parse inline tags into formatted DOCX runs
        formatted_runs = self._build_formatted_runs(target_text)
        has_real_inline_tags = bool(
            re.search(
                r"<\s*/?\s*bx\b|<\s*/?\s*ex\b",
                target_text,
            )
        )
        if formatted_runs and has_real_inline_tags:
            target_run = runs[0]
            parent = target_run.getparent()
            if parent is not None:
                parent_para = parent.getparent()
                if parent_para is not None:
                    for i, fr in enumerate(formatted_runs):
                        parent_para.insert(
                            list(parent_para).index(parent) + i,
                            fr,
                        )
                    # lxml: setting .text on the parent w:r does NOT
                    # clear text in child w:t nodes. Set the w:t directly.
                    target_run.text = ""
                    for r in runs[1:]:
                        r.text = ""
                    return True
                runs[0].text = _strip_inline_tags(target_text)
                for r in runs[1:]:
                    r.text = ""
                return True

        _distribute_text_across_runs(runs, target_text)
        return True

    def _backfill_fallback_textboxes(
        self,
        root: etree._Element,
        chinese_to_target: dict[str, str],
    ) -> None:
        """Apply translations to <mc:Fallback> textbox paragraphs.

        Choice-branch textbox paragraphs are collected and translated by
        _backfill_by_non_body_position, but their Fallback-branch counterparts
        (with identical original text) are skipped by the text-based dedup in
        _collect_all_paragraphs. This method finds Fallback textbox paragraphs
        and applies the same translation from the pre-built mapping.

        Args:
            root: The document XML root.
            chinese_to_target: Mapping from original Chinese text (joined & stripped)
                               to the target English text applied to the Choice branch.
        """
        W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        w_tag = f"{{{W_NS}}}"
        WORD_NS_MAP_LOCAL = {"w": W_NS}

        for txbx in root.iter(f"{w_tag}txbxContent"):
            parent = txbx.getparent()
            in_fallback = False
            while parent is not None:
                if parent.tag.endswith("Fallback"):
                    in_fallback = True
                    break
                parent = parent.getparent()
            if not in_fallback:
                continue

            for p in txbx.iter(f"{w_tag}p"):
                text = "".join(t.text or "" for t in p.iter(f"{w_tag}t")).strip()
                if not text:
                    continue
                target_text = chinese_to_target.get(text)
                if target_text is None:
                    continue
                runs = p.xpath("./w:r/w:t", namespaces=WORD_NS_MAP_LOCAL)
                if runs:
                    _distribute_text_across_runs(runs, target_text)

    def _fallback_backfill(self, root: etree._Element, target_text: str) -> bool:
        """Last-resort fallback: log a warning and skip.

        The previous implementation wrote the LLM target to the FIRST
        non-empty paragraph in the document, clobbering unrelated content
        and corrupting 2,700+ paragraphs in the slim. The new behavior
        leaves the OPP source paragraph untouched: the reader sees a
        partial translation (some Chinese remains) instead of corruption.
        """
        logger.warning(
            "No matching paragraph for LLM target; skipping. "
            "Original OPP source text is preserved. Target: %r",
            target_text[:80],
        )
        return False

    def _backfill_with_inline_elements(
        self,
        root: etree._Element,
        source_normalized: str,
        target_text: str,
        inline_elements: list[InlineElement],
        all_paragraph_text_map: dict[str, etree._Element],
    ) -> bool:
        """Backfill when source has inline formatting tags.

        Finds paragraphs where the source text (with inline tags stripped) matches,
        then applies translations preserving inline structure.
        """
        found = False
        # Pattern B: O(1) exact match on all paragraphs
        p = all_paragraph_text_map.get(source_normalized)
        if p is not None:
            found = self._backfill_split_runs(p, source_normalized, target_text)
            if found:
                return found

        # Fallback: sequential scan for paragraphs not in the exact-match index
        for p in root.xpath("//w:p", namespaces=WORD_NS_MAP):
            text_runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
            text_content = "".join(t.text or "" for t in text_runs)

            if source_normalized in text_content:
                found = self._backfill_split_runs(p, source_normalized, target_text)
                if found:
                    break

        return found

    def _backfill_split_runs(
        self,
        paragraph: etree._Element,
        source_normalized: str,
        target_text: str,
    ) -> bool:
        """Backfill text that may be split across multiple <w:t> runs.

        Finds the first run containing source_normalized, replaces it with
        target_text, and clears subsequent runs. Strips XLIFF bx/ex tags and
        applies proper DOCX run formatting.

        E2E-07 fix: when exact match fails, try fuzzy matching if the lengths
        are close (within 5 chars). This handles cases where the XLIFF source
        contains content that doesn't exactly match the DOCX paragraph (e.g.
        missing middle content that was moved to a separate paragraph by the DOCX
        editor's paragraph-splitting algorithm).
        """
        runs = paragraph.xpath(".//w:t", namespaces=WORD_NS_MAP)
        concat = "".join(r.text or "" for r in runs)

        match_pos = concat.find(source_normalized)
        if match_pos >= 0:
            # Exact match — use it
            pass
        elif len(source_normalized) > 3 and abs(len(source_normalized) - len(concat)) <= 5:
            # E2E-07 fix: fuzzy match when lengths are close
            # Try to find the best substring match using sequence similarity
            best_ratio = 0.0
            best_pos = -1
            step = max(1, min(10, len(source_normalized) // 4))
            # Try matching at different windows of concat
            for start in range(0, max(1, len(concat) - len(source_normalized) + 10), step):
                window = concat[start:start + len(source_normalized) + 5]
                import difflib
                ratio = difflib.SequenceMatcher(None, source_normalized, window[:len(source_normalized)]).ratio()
                if ratio > best_ratio and ratio >= 0.85:
                    best_ratio = ratio
                    best_pos = start
            if best_pos >= 0:
                match_pos = best_pos
                logger.debug("E2E-07 fuzzy match: ratio=%.2f pos=%d", best_ratio, best_pos)
        else:
            return False

        if match_pos < 0:
            return False

        target_run_idx = None
        for i, r in enumerate(runs):
            run_text = r.text or ""
            run_start = concat.find(run_text, match_pos) if run_text else -1
            if run_start <= match_pos < run_start + len(run_text) or run_start < 0:
                target_run_idx = i
                break

        if target_run_idx is None:
            return False

        formatted_runs = self._build_formatted_runs(target_text)
        if formatted_runs:
            target_run = runs[target_run_idx]
            parent = target_run.getparent()
            if parent is not None:
                insert_pos = list(parent).index(target_run)
                for fr in formatted_runs:
                    parent.insert(insert_pos, fr)
                    insert_pos += 1
                target_run.text = ""
                for j in range(target_run_idx + 1, len(runs)):
                    runs[j].text = ""
            return True

        runs[target_run_idx].text = target_text
        for j in range(target_run_idx + 1, len(runs)):
            runs[j].text = ""
        return True

    def _build_formatted_runs(self, target_text: str) -> list[etree._Element]:
        """Parse target_text with XLIFF bx/ex tags and build DOCX w:r elements.

        Args:
            target_text: Target text potentially containing <bx.../> and <ex.../> tags.

        Returns:
            List of w:r elements with appropriate rPr formatting, or empty list
            if no formatting tags found.
        """
        W = f"{{{W_NS}}}"

        bx_re = re.compile(r'<bx[^>]*\sid="([^"]*)"[^>]*\stype="([^"]*)"[^>]*/>', re.IGNORECASE)
        ex_re = re.compile(r'<ex[^>]*\sid="([^"]*)"[^>]*/>', re.IGNORECASE)

        segments: list[tuple[str, dict[str, str]]] = []
        open_formats: dict[str, str] = {}
        remaining = target_text

        while remaining:
            bx_match = bx_re.search(remaining)
            ex_match = ex_re.search(remaining)

            if not bx_match and not ex_match:
                if remaining:
                    segments.append((remaining, dict(open_formats)))
                break

            earliest = None
            if bx_match:
                earliest = (bx_match.start(), bx_match, "bx")
            if ex_match:
                ex_start = ex_match.start()
                if earliest is None or ex_start < earliest[0]:
                    earliest = (ex_start, ex_match, "ex")

            if earliest is None:
                segments.append((remaining, dict(open_formats)))
                break

            tag_pos, match_obj, tag_type = earliest

            if tag_pos > 0:
                segments.append((remaining[:tag_pos], dict(open_formats)))

            if tag_type == "bx":
                elem_id, elem_type = match_obj.group(1), match_obj.group(2).lower()
                for fmt in (s.strip() for s in elem_type.split(',')):
                    if fmt:
                        open_formats[f"{elem_id}::{fmt}"] = fmt
            else:
                elem_id = match_obj.group(1)
                keys_to_remove = [k for k in open_formats if k.startswith(f"{elem_id}::")]
                for k in keys_to_remove:
                    del open_formats[k]

            remaining = remaining[match_obj.end():]

        # Fast path: no bx/ex in target_text → plain text, no formatting to parse.
        # Lets callers use the simpler plain-text replacement path instead of
        # inserting a new <w:r> alongside the existing one.
        if not ('<bx' in target_text.lower() or '<ex' in target_text.lower()):
            return []

        if not segments:
            clean = re.sub(r'<bx[^>]*/>|<ex[^>]*/>', '', target_text)
            if clean == target_text:
                return []
            segments = [(clean, {})]

        runs = []
        for text, active_formats in segments:
            if not text:
                continue
            r = etree.Element(f"{W}r")

            has_formatting = any(
                fmt in ("bold", "italic", "underline", "double-underline", "single-underline", "strike")
                for fmt in active_formats.values()
            )
            if has_formatting:
                rpr = etree.SubElement(r, f"{W}rPr")
                if "bold" in active_formats.values():
                    etree.SubElement(rpr, f"{W}b")
                if "italic" in active_formats.values():
                    etree.SubElement(rpr, f"{W}i")
                for fmt_type, fmt_val in active_formats.items():
                    if fmt_val == "double-underline":
                        u_elem = etree.SubElement(rpr, f"{W}u")
                        u_elem.set(f"{W}val", "double")
                    elif fmt_val == "single-underline":
                        u_elem = etree.SubElement(rpr, f"{W}u")
                        u_elem.set(f"{W}val", "single")
                    elif fmt_val == "underline":
                        u_elem = etree.SubElement(rpr, f"{W}u")
                        u_elem.set(f"{W}val", "single")
                    if fmt_val == "strike":
                        etree.SubElement(rpr, f"{W}strike")

            t = etree.SubElement(r, f"{W}t")
            t.text = text

            runs.append(r)

        return runs

    def _apply_inline_formatting_to_run(
        self,
        t_elem: etree._Element,
        inline_elements: list[InlineElement],
        target_text: str,
    ) -> None:
        """Apply inline formatting to a text run element.

        Args:
            t_elem: The w:t element to apply formatting to.
            inline_elements: List of inline elements to apply.
            target_text: The target text for formatting.
        """
        # Get parent w:r element
        parent = t_elem.getparent()
        if parent is None:
            return

        r_parent = parent.getparent()
        if r_parent is None:
            r_parent = parent

        # Insert rPr (run properties) before the w:t element
        rpr = etree.SubElement(r_parent, f"{{{self.docx_applier.W_NS}}}rPr")

        for elem in inline_elements:
            if elem.type == "close":
                continue

            tag_name = self.docx_applier.TYPE_TO_TAG.get(elem.type.lower(), elem.type)
            if tag_name:
                prop_elem = etree.SubElement(rpr, f"{{{self.docx_applier.W_NS}}}{tag_name}")

                # Handle special properties like underline with val attribute
                if elem.type.lower() in ("underline", "double-underline", "single-underline"):
                    val = "single" if elem.type.lower() == "underline" else "double"
                    prop_elem.set(f"{{{self.docx_applier.W_NS}}}val", val)

    def inject_images(
        self,
        skeleton_path: Path | str,
        images: list[ImagePlacement],
        output_path: Path | str,
    ) -> tuple[list[ImagePlacement], list[ImagePlacement]]:
        """Inject images into DOCX skeleton.

        Routes each image by position type:
          - is_floating=True, paragraph_index=None -> wp:anchor (page-positioned)
          - paragraph_index=int                    -> wp:inline (paragraph-anchored)
          - otherwise                              -> orphaned (logged)

        Args:
            skeleton_path: Path to skeleton DOCX file.
            images: List of ImagePlacement objects from OPP.
            output_path: Path to write the output DOCX.

        Returns:
            Tuple of (injected_images, orphaned_images).
        """
        skeleton_path = Path(skeleton_path)
        output_path = Path(output_path)

        orphaned: list[ImagePlacement] = []
        injected: list[ImagePlacement] = []

        floating = [
            img for img in images
            if getattr(img, "is_floating", False) and img.paragraph_index is None
        ]
        positioned = [
            img for img in images
            if img.paragraph_index is not None and not getattr(img, "is_floating", False)
        ]
        unpositioned = [
            img for img in images
            if img.paragraph_index is None and not getattr(img, "is_floating", False)
        ]

        if unpositioned:
            for img in unpositioned:
                logger.warning(
                    "Image has no paragraph_index, appending to end: mime_type=%s",
                    img.mime_type,
                )
            orphaned.extend(unpositioned)

        if not positioned and not floating:
            if images:
                self.skeleton_loader.load_skeleton(str(skeleton_path))
                self.skeleton_loader.repack_docx(str(output_path))
            return (injected, orphaned)

        try:
            skeleton_data = self.skeleton_loader.load_skeleton(str(skeleton_path))
        except Exception as e:
            logger.error("Failed to load skeleton for image injection: %s", e)
            orphaned.extend(images)
            return (injected, orphaned)

        document_xml = skeleton_data.get("xml")
        if document_xml is None:
            logger.warning(
                "Skeleton has no 'xml' key (cross-format?). Skipping image injection."
            )
            orphaned.extend(images)
            return (injected, orphaned)
        files = skeleton_data["files"]
        namelist = list(files.keys())

        root = etree.fromstring(document_xml.encode("utf-8"))
        paragraphs = root.xpath("//w:p", namespaces=WORD_NS_MAP)

        img_by_para: dict[int, list[ImagePlacement]] = {}
        for img in positioned:
            idx = img.paragraph_index
            assert idx is not None, "positioned images must have paragraph_index"
            if idx not in img_by_para:
                img_by_para[idx] = []
            img_by_para[idx].append(img)

        for para_idx, imgs in img_by_para.items():
            if para_idx < 0 or para_idx >= len(paragraphs):
                logger.warning(
                    "paragraph_index %d out of range, %d paragraphs available",
                    para_idx,
                    len(paragraphs),
                )
                orphaned.extend(imgs)
                continue

            para = paragraphs[para_idx]
            next_r = None
            for child in para:
                if child.tag == f"{{{W_NS}}}r":
                    next_r = child
                    break

            for i, img in enumerate(imgs):
                try:
                    img_bytes = self._get_image_bytes(img)
                    width, height = self._get_image_dimensions(img, img_bytes)

                    # Dedup (TURNKEY-IMG-01): the skeleton already preserves
                    # the original document.xml (including its inline drawings).
                    # OPP's images.json lists every image with its original
                    # paragraph_index, so without this check ORF re-injects
                    # duplicates of inline drawings that are already in the
                    # skeleton. Mirrors the floating-image dedup at 1448-1467.
                    if self._paragraph_already_has_drawing(root, width, height):
                        logger.debug(
                            "Skipping duplicate inline image at paragraph %d "
                            "with cx=%d, cy=%d (already present in skeleton)",
                            para_idx, width, height,
                        )
                        injected.append(img)  # count as "injected" to mirror floating path
                        continue

                    rId = self._add_image_to_zip(img_bytes, img.mime_type, files, namelist)
                    drawing_xml = self._create_drawing_xml(rId, width, height)
                    drawing_elem = etree.fromstring(drawing_xml)

                    if next_r is not None:
                        insert_pos = list(para).index(next_r)
                        para.insert(insert_pos + i, drawing_elem)
                    else:
                        para.append(drawing_elem)

                    injected.append(img)
                    logger.debug(
                        "Injected image at paragraph %d: rId=%s, mime=%s",
                        para_idx,
                        rId,
                        img.mime_type,
                    )
                except Exception as e:
                    logger.error("Failed to inject image: %s", e)
                    orphaned.append(img)

        for img in floating:
            try:
                if self._inject_floating_image(root, img, files, namelist):
                    injected.append(img)
                    logger.debug(
                        "Injected floating image: rId=embedded, H=%d EMU, V=%d EMU",
                        img.wp_anchor_h,
                        img.wp_anchor_v,
                    )
                else:
                    orphaned.append(img)
            except Exception as e:
                logger.error("Failed to inject floating image: %s", e)
                orphaned.append(img)

        new_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + etree.tostring(root, encoding="unicode")
        )

        try:
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in files.items():
                    ct = self.skeleton_loader.compress_types.get(name, zipfile.ZIP_STORED)
                    data_to_write = data if name != "word/document.xml" else new_xml.encode("utf-8")
                    if ct == zipfile.ZIP_STORED:
                        zf.writestr(name, data_to_write, compress_type=zipfile.ZIP_STORED)
                    else:
                        zf.writestr(name, data_to_write)
        except Exception as e:
            logger.error("Failed to repack DOCX with images: %s", e)
            return (injected, orphaned + [img for img in positioned if img not in injected])

        if orphaned:
            logger.warning(
                "%d images could not be positioned and were not injected",
                len(orphaned),
            )

        return (injected, orphaned)

    def _paragraph_already_has_drawing(
        self,
        root: etree._Element,
        cx: int,
        cy: int,
    ) -> bool:
        """True iff ``root`` already contains a ``<w:drawing>`` with matching extent.

        Mirrors the floating-image dedup at 1448-1467: that one iterates
        ``root.iter('{wp}anchor')`` (whole document) and skips when the
        positionH/V match. This one iterates the whole document looking for a
        ``<wp:extent>`` with matching cx/cy, because:

        * The skeleton preserves the original document.xml — every inline
          drawing OPP records is already in the skeleton.
        * OPP's paragraph_index doesn't reliably match ORF's
          ``root.xpath('//w:p')`` enumeration (off-by-one observed on the
          Haier DOCX: OPP says 6, skeleton places IM 16 at paragraph 7).

        A global extent match is the only safe dedup key that works regardless
        of the paragraph-index mismatch.
        """
        for extent in root.iter(f"{{{WP_NS}}}extent"):
            try:
                existing_cx = int(extent.get("cx", "-1"))
                existing_cy = int(extent.get("cy", "-1"))
            except (ValueError, TypeError):
                continue
            if existing_cx == cx and existing_cy == cy:
                return True
        return False

    def _inject_floating_image(
        self,
        root: etree._Element,
        img: ImagePlacement,
        files: dict[str, bytes],
        namelist: list[str],
    ) -> bool:
        """Inject a single floating image as a w:drawing > wp:anchor.

        Floating images are page-positioned via wp:posOffset, so we attach the
        drawing to the first w:p in the body (Word treats anchors attached at
        the body level as page-relative). H/V coordinates are read from the
        image record (already in EMU, supplied by OPP).

        Args:
            root: The <w:document> element being mutated in place.
            img: ImagePlacement with is_floating=True and wp_anchor_h/v set.
            files: Mutable ZIP-file-bytes map (media + rels updated in place).
            namelist: Mutable list of ZIP member names (media added in place).

        Returns:
            True on successful injection, False on any failure (caller will
            route the image to orphaned).
        """
        img_bytes = self._get_image_bytes(img)
        rId = self._add_image_to_zip(img_bytes, img.mime_type, files, namelist)
        width, height = self._get_image_dimensions(img, img_bytes)

        anchor_xml = self._create_floating_anchor_xml(
            rId=rId,
            cx=width,
            cy=height,
            pos_h=img.wp_anchor_h,
            pos_v=img.wp_anchor_v,
            relative_h=getattr(img, "wp_anchor_relative_h", "page") or "page",
            relative_v=getattr(img, "wp_anchor_relative_v", "page") or "page",
        )
        anchor_elem = etree.fromstring(anchor_xml)

        body = root.find(f"{{{W_NS}}}body")
        if body is None:
            return False

        first_para = body.find(f"{{{W_NS}}}p")
        if first_para is None:
            return False

        first_para.insert(0, anchor_elem)
        return True

    def _create_floating_anchor_xml(
        self,
        rId: str,
        cx: int,
        cy: int,
        pos_h: int,
        pos_v: int,
        relative_h: str = "page",
        relative_v: str = "page",
    ) -> str:
        """Build a w:drawing > wp:anchor element string for a floating image.

        Layout follows the OOXML DrawingML anchor schema:
          distT/distB/distL/distR = text-wrap margins (EMU, 0 here = free)
          simplePos=0              = use positionH/V, not simplePos
          relativeHeight            = z-ordering hint (large = below)
          behindDoc=0, locked=0, layoutInCell=1, allowOverlap=1 = common defaults

        C13 fix: relative_h/relative_v are validated against the OOXML
        allowlist and serialized via etree.SubElement + .set() (which
        XML-escapes attribute values) instead of f-string interpolation.
        """
        # Validate against allowlist BEFORE serializing.
        if relative_h not in ALLOWED_RELATIVE_FROM:
            raise ValueError(
                f"relative_h {relative_h!r} is not in the OOXML allowlist "
                f"{sorted(ALLOWED_RELATIVE_FROM)}"
            )
        if relative_v not in ALLOWED_RELATIVE_FROM:
            raise ValueError(
                f"relative_v {relative_v!r} is not in the OOXML allowlist "
                f"{sorted(ALLOWED_RELATIVE_FROM)}"
            )

        # Numeric coercion for pos_h/pos_v so non-integer strings can't slip
        # through as text content either.
        pos_h_int = int(pos_h)
        pos_v_int = int(pos_v)

        # Build via etree so attribute values are XML-escaped. .set() will
        # escape the relative_h/v values even though they're already
        # allowlisted — defense in depth.
        NSMAP = {
            "w": W_NS,
            "wp": WP_NS,
            "a": A_NS,
            "pic": PIC_NS,
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }
        drawing = etree.Element(f"{{{W_NS}}}drawing", nsmap=NSMAP)
        anchor = etree.SubElement(
            drawing,
            f"{{{WP_NS}}}anchor",
            attrib={
                "distT": "0",
                "distB": "0",
                "distL": "114300",
                "distR": "114300",
                "simplePos": "0",
                "relativeHeight": "251659264",
                "behindDoc": "0",
                "locked": "0",
                "layoutInCell": "1",
                "allowOverlap": "1",
            },
        )
        etree.SubElement(
            anchor, f"{{{WP_NS}}}simplePos", x="0", y="0"
        )
        pos_h_elem = etree.SubElement(
            anchor, f"{{{WP_NS}}}positionH", relativeFrom=relative_h,
        )
        etree.SubElement(pos_h_elem, f"{{{WP_NS}}}posOffset").text = str(pos_h_int)
        pos_v_elem = etree.SubElement(
            anchor, f"{{{WP_NS}}}positionV", relativeFrom=relative_v,
        )
        etree.SubElement(pos_v_elem, f"{{{WP_NS}}}posOffset").text = str(pos_v_int)
        etree.SubElement(
            anchor, f"{{{WP_NS}}}extent", cx=str(cx), cy=str(cy)
        )
        etree.SubElement(
            anchor, f"{{{WP_NS}}}effectExtent", l="0", t="0", r="0", b="0"
        )
        etree.SubElement(anchor, f"{{{WP_NS}}}wrapNone")
        etree.SubElement(anchor, f"{{{WP_NS}}}docPr", id="1", name="Picture")
        cNvGraphicFramePr = etree.SubElement(
            anchor, f"{{{WP_NS}}}cNvGraphicFramePr"
        )
        etree.SubElement(
            cNvGraphicFramePr, f"{{{A_NS}}}graphicFrameLocks", noChangeAspect="1"
        )
        graphic = etree.SubElement(anchor, f"{{{A_NS}}}graphic")
        graphicData = etree.SubElement(
            graphic, f"{{{A_NS}}}graphicData",
            uri="http://schemas.openxmlformats.org/drawingml/2006/picture",
        )
        pic_pic = etree.SubElement(graphicData, f"{{{PIC_NS}}}pic")
        nvPicPr = etree.SubElement(pic_pic, f"{{{PIC_NS}}}nvPicPr")
        etree.SubElement(nvPicPr, f"{{{PIC_NS}}}cNvPr", id="1", name="image")
        etree.SubElement(nvPicPr, f"{{{PIC_NS}}}cNvPicPr")
        blipFill = etree.SubElement(pic_pic, f"{{{PIC_NS}}}blipFill")
        etree.SubElement(
            blipFill, f"{{{A_NS}}}blip",
            attrib={"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed": rId},
        )
        stretch = etree.SubElement(blipFill, f"{{{A_NS}}}stretch")
        etree.SubElement(stretch, f"{{{A_NS}}}fillRect")
        spPr = etree.SubElement(pic_pic, f"{{{PIC_NS}}}spPr")
        xfrm = etree.SubElement(spPr, f"{{{A_NS}}}xfrm")
        etree.SubElement(xfrm, f"{{{A_NS}}}off", x="0", y="0")
        etree.SubElement(xfrm, f"{{{A_NS}}}ext", cx=str(cx), cy=str(cy))
        prstGeom = etree.SubElement(spPr, f"{{{A_NS}}}prstGeom", prst="rect")
        etree.SubElement(prstGeom, f"{{{A_NS}}}avLst")

        return etree.tostring(drawing, encoding="unicode")

    def _get_image_bytes(self, img: ImagePlacement) -> bytes:
        if img.data_base64:
            return base64.b64decode(img.data_base64)
        if img.file_path:
            # C4 fix (defense in depth): validate file_path before reading.
            from orf.mcp.security import PathValidator
            valid, err = PathValidator.validate(img.file_path)
            if not valid:
                raise ValueError(f"file_path rejected: {err}")
            return Path(img.file_path).read_bytes()
        raise ValueError("ImagePlacement must have data_base64 or file_path")

    def _get_image_dimensions(
        self, img: ImagePlacement, img_bytes: bytes
    ) -> tuple[int, int]:
        if img.width is not None and img.height is not None:
            return (img.width, img.height)

        try:
            from PIL import Image
            from io import BytesIO

            img_obj = Image.open(BytesIO(img_bytes))
            w, h = img_obj.size
            return (w, h)
        except (OSError, ValueError):
            logger.exception("Failed to get image dimensions, using default 200000x150000")
            return (200000, 150000)

    def _add_image_to_zip(
        self,
        img_bytes: bytes,
        mime_type: str,
        files: dict[str, bytes],
        namelist: list[str],
    ) -> str:
        ext_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/webp": ".webp",
        }
        ext = ext_map.get(mime_type, ".png")
        md5_hash = hashlib.md5(img_bytes).hexdigest()
        dedup_name = f"image_{md5_hash[:8]}{ext}"

        existing_rid = None
        for name, data in files.items():
            if name.startswith("word/media/") and data == img_bytes:
                existing_rid = name
                break

        if existing_rid:
            # existing_rid is like "word/media/image8.png"
            # Look up the REAL relationship ID from document.xml.rels
            rels_file = files.get("word/_rels/document.xml.rels")
            if rels_file:
                try:
                    rels_root = etree.fromstring(rels_file)
                    media_filename = existing_rid.replace("word/media/", "")
                    for rel in rels_root:
                        target = rel.get("Target", "")
                        if target.replace("\\", "/").endswith(media_filename):
                            return rel.get("Id")
                except Exception:
                    pass
            # Fallback: return the old fake rid (better than crashing)
            return existing_rid.replace("word/media/", "rId")

        media_name = f"word/media/{dedup_name}"
        files[media_name] = img_bytes
        namelist.append(media_name)

        rels_name = "word/_rels/document.xml.rels"
        if rels_name in files:
            rels_xml = files[rels_name].decode("utf-8")
            root = etree.fromstring(rels_xml.encode("utf-8"))

            max_rid = 0
            for rel in root.xpath("//*"):
                rid = rel.get("Id", "")
                if rid.startswith("rId"):
                    try:
                        num = int(rid[3:])
                        if num > max_rid:
                            max_rid = num
                    except ValueError:
                        pass

            new_rid = f"rId{max_rid + 1}"
            rels_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
            new_rel = etree.SubElement(root, f"{{{rels_ns}}}Relationship")
            new_rel.set("Id", new_rid)
            new_rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
            new_rel.set("Target", f"media/{dedup_name}")

            files[rels_name] = etree.tostring(root, encoding="unicode").encode("utf-8")

            return new_rid
        else:
            return "rId1"

    def _create_drawing_xml(
        self,
        rId: str,
        cx: int,
        cy: int,
    ) -> str:
        drawing = f'''
<w:drawing xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
           xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
           xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
             distT="0" distB="0" distL="0" distR="0">
    <wp:extent cx="{cx}" cy="{cy}"/>
    <wp:docPr id="1" name="Picture"/>
    <wp:cNvGraphicFramePr>
      <a:graphicFrameLocks noChangeAspect="1"/>
    </wp:cNvGraphicFramePr>
    <a:graphic>
      <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
        <pic:pic>
          <pic:nvPicPr>
            <pic:cNvPr id="1" name="image"/>
            <pic:cNvPicPr/>
          </pic:nvPicPr>
          <pic:blipFill>
            <a:blip r:embed="{rId}"/>
            <a:stretch>
              <a:fillRect/>
            </a:stretch>
          </pic:blipFill>
          <pic:spPr>
            <a:xfrm>
              <a:off x="0" y="0"/>
              <a:ext cx="{cx}" cy="{cy}"/>
            </a:xfrm>
            <a:prstGeom prst="rect">
              <a:avLst/>
            </a:prstGeom>
          </pic:spPr>
        </pic:pic>
      </a:graphicData>
    </a:graphic>
  </wp:inline>
</w:drawing>'''
        return drawing