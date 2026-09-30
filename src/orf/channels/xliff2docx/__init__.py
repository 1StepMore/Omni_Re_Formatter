"""XLIFF→DOCX backfill with inline formatting preservation.

Package split from the original monolithic ``xliff2docx.py``.
The ``XLIFF2DOCXConverter`` class is defined here, with heavy-lifting
delegated to submodule functions for parsing, matching, writing,
styling, and image injection.
"""

from __future__ import annotations

import re
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

from ._ns import (
    W_NS,
    WORD_NS_MAP,
)

logger = get_logger("channel.xliff2docx")


# ── Re-export shared names for backward compat ──────────────────────
from ._ns import FUZZY_MATCH_THRESHOLD, A_NS, PIC_NS, WP_NS  # noqa: E402
from .matcher import (  # noqa: E402
    PROCESS_WHOLE_CELL,
    SKIP_BARE_IN_PARA_CELL,
    SKIP_DUPLICATE_PARA,
    _distribute_text_across_runs,
    build_table_cell_plan,
)
from .parser import _strip_wrapper, _strip_inline_tags  # noqa: E402

__all__ = [
    "XLIFF2DOCXConverter",
    "XLIFFTransUnitData",
    "InlineElementData",
    "FUZZY_MATCH_THRESHOLD",
    "A_NS",
    "PIC_NS",
    "W_NS",
    "WP_NS",
    "_strip_wrapper",
    "_strip_inline_tags",
    "_distribute_text_across_runs",
]


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

    # ── Parser delegation ───────────────────────────────────────────

    def _parse_xliff(self, xliff_path: Path | str) -> list[dict[str, Any]]:
        """Parse XLIFF file and extract trans-units.

        Delegates to ``orf.channels.xliff2docx.parser.parse_xliff``.
        """
        from orf.channels.xliff2docx.parser import parse_xliff

        return parse_xliff(xliff_path, self.inline_parser)

    def _extract_inline_elements(
        self, source_xml: str
    ) -> list[InlineElement]:
        """Extract inline elements from XLIFF source XML.

        Delegates to ``orf.channels.xliff2docx.parser.extract_inline_elements_from_xml``.
        """
        from orf.channels.xliff2docx.parser import extract_inline_elements_from_xml

        return extract_inline_elements_from_xml(source_xml, self.inline_parser)

    # ── Matcher delegation ──────────────────────────────────────────

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
        """Backfill a single translation into the parsed document root.

        Delegates to ``orf.channels.xliff2docx.matcher.backfill_translation``.
        """
        from orf.channels.xliff2docx.matcher import backfill_translation

        return backfill_translation(
            root, body_paragraphs,
            source_text, target_text,
            inline_elements,
            wt_text_map, body_paragraph_text_map, all_paragraph_text_map,
            self._build_formatted_runs,
            _strip_inline_tags,
        )

    def _fuzzy_backfill(
        self,
        root: etree._Element,
        source_normalized: str,
        target_text: str,
        threshold: float = FUZZY_MATCH_THRESHOLD,
    ) -> bool:
        """Apply target_text via fuzzy matching.

        Delegates to ``orf.channels.xliff2docx.matcher.fuzzy_backfill``.
        """
        from orf.channels.xliff2docx.matcher import fuzzy_backfill

        return fuzzy_backfill(root, source_normalized, target_text, threshold)

    def _backfill_by_position(
        self,
        root: etree._Element,
        body_paragraphs: list[etree._Element],
        para_index: int,
        target_text: str,
    ) -> bool:
        """Phase B.2: position-based backfill via ``resname="para_index_N"``.

        Delegates to ``orf.channels.xliff2docx.matcher.backfill_by_position``.
        """
        from orf.channels.xliff2docx.matcher import backfill_by_position

        return backfill_by_position(
            body_paragraphs, para_index, target_text,
            self._build_formatted_runs,
            _strip_inline_tags,
        )

    def _collect_all_paragraphs(
        self, root: etree._Element
    ) -> list[etree._Element]:
        """Collect ALL ``w:p`` elements in OPP extraction order.

        Delegates to ``orf.channels.xliff2docx.matcher.collect_all_paragraphs``.
        """
        from orf.channels.xliff2docx.matcher import collect_all_paragraphs

        return collect_all_paragraphs(root)

    def _backfill_by_non_body_position(
        self,
        paragraphs: list[etree._Element],
        para_idx: int,
        target_text: str,
    ) -> bool:
        """Phase B.3: position-based backfill via ``resname="non_body_N"``.

        Delegates to ``orf.channels.xliff2docx.matcher.backfill_by_non_body_position``.
        """
        from orf.channels.xliff2docx.matcher import backfill_by_non_body_position

        return backfill_by_non_body_position(
            paragraphs, para_idx, target_text,
            self._build_formatted_runs,
            _strip_inline_tags,
        )

    def _backfill_by_table_cell(
        self,
        root: etree._Element,
        table_index: int,
        row: int,
        col: int,
        target_text: str,
        para: int | None = None,
    ) -> bool:
        """Issue A: positional backfill via ``resname="table_{t}_r{r}_c{c}"``.

        OPP#80 Wave 1: ``para`` (when not None) scopes the write to a single
        paragraph of the cell; ``None`` keeps the legacy whole-cell behavior.

        Delegates to ``orf.channels.xliff2docx.matcher.backfill_by_table_cell``.
        """
        from orf.channels.xliff2docx.matcher import backfill_by_table_cell

        return backfill_by_table_cell(
            root, table_index, row, col, target_text,
            self._build_formatted_runs,
            _strip_inline_tags,
            para,
        )

    def _backfill_fallback_textboxes(
        self,
        root: etree._Element,
        chinese_to_target: dict[str, str],
    ) -> None:
        """Apply translations to ``<mc:Fallback>`` textbox paragraphs.

        Delegates to ``orf.channels.xliff2docx.matcher.backfill_fallback_textboxes``.
        """
        from orf.channels.xliff2docx.matcher import backfill_fallback_textboxes

        backfill_fallback_textboxes(root, chinese_to_target)

    def _fallback_backfill(
        self, root: etree._Element, target_text: str
    ) -> bool:
        """Last-resort fallback: log a warning and skip.

        Delegates to ``orf.channels.xliff2docx.matcher.fallback_backfill``.
        """
        from orf.channels.xliff2docx.matcher import fallback_backfill

        return fallback_backfill(target_text)

    def _backfill_with_inline_elements(
        self,
        root: etree._Element,
        source_normalized: str,
        target_text: str,
        inline_elements: list[InlineElement],
        all_paragraph_text_map: dict[str, etree._Element],
    ) -> bool:
        """Backfill when source has inline formatting tags.

        Delegates to ``orf.channels.xliff2docx.matcher.backfill_with_inline_elements``.
        """
        from orf.channels.xliff2docx.matcher import backfill_with_inline_elements

        return backfill_with_inline_elements(
            root, source_normalized, target_text,
            inline_elements, all_paragraph_text_map,
            self._build_formatted_runs,
        )

    def _backfill_split_runs(
        self,
        paragraph: etree._Element,
        source_normalized: str,
        target_text: str,
    ) -> bool:
        """Backfill text split across multiple ``<w:t>`` runs.

        Delegates to ``orf.channels.xliff2docx.matcher.backfill_split_runs``.
        """
        from orf.channels.xliff2docx.matcher import backfill_split_runs

        return backfill_split_runs(
            paragraph, source_normalized, target_text,
            self._build_formatted_runs,
        )

    # ── Writer delegation ───────────────────────────────────────────

    def _build_formatted_runs(
        self, target_text: str
    ) -> list[etree._Element]:
        """Parse target text with bx/ex tags and build DOCX ``w:r`` elements.

        Delegates to ``orf.channels.xliff2docx.writer.build_formatted_runs``.
        """
        from orf.channels.xliff2docx.writer import build_formatted_runs

        return build_formatted_runs(target_text)

    def _apply_inline_formatting_to_run(
        self,
        t_elem: etree._Element,
        inline_elements: list[InlineElement],
        target_text: str,
    ) -> None:
        """Apply inline formatting to a text run element.

        Delegates to ``orf.channels.xliff2docx.styles.apply_inline_formatting_to_run``.

        NOTE(ORF#33): This per-run formatting method is now USED by
        the converter via ``DOCXInlineApplier.apply_formatting``
        (document-level search-and-apply in post-processing Phase B.4).
        The per-run variant is retained as a utility for future
        fine-grained formatting control; the main fix path applies
        source inline formatting at the document level after all
        backfill writes are complete, mirroring the PPTX pattern
        (``xliff2pptx.py:427-440``).
        """
        from orf.channels.xliff2docx.styles import apply_inline_formatting_to_run

        return apply_inline_formatting_to_run(
            t_elem, inline_elements, target_text, self.docx_applier,
        )

    def _apply_source_formatting(
        self,
        root: etree._Element,
        inline_elements: list[InlineElement],
        target_text: str,
    ) -> tuple[etree._Element, bool]:
        """ORF#33: apply source-side inline formatting when LLM dropped tags.

        Mirrors the PPTX post-processing pattern at
        ``xliff2pptx.py:427-440``: when ``inline_elements`` is non-empty
        AND the LLM-emitted target text has no literal ``<bx>``/``<ex>``
        tags, call ``DOCXInlineApplier.apply_formatting`` to add the
        source-side ``<w:rPr>`` formatting to the runs that now hold
        the translated text.

        The ``<bx>``/``<ex>`` guard makes this mutually exclusive with
        System A (``build_formatted_runs``): when the LLM preserves
        formatting tags, System A already produced formatted runs; we
        skip here to avoid double-applying ``<w:rPr>``.

        Returns (possibly replaced root, True if formatting was applied).
        """
        if not inline_elements:
            return root, False
        if re.search(r'<bx|<ex', target_text, re.IGNORECASE):
            return root, False
        xml_str = etree.tostring(root, encoding="unicode")
        formatted_xml = self.docx_applier.apply_formatting(
            xml_str, inline_elements, target_text,
        )
        if formatted_xml != xml_str:
            return etree.fromstring(formatted_xml.encode("utf-8")), True
        return root, False

    def _rebuild_indexes(
        self,
        root: etree._Element,
    ) -> tuple[
        list[etree._Element],
        list[etree._Element],
        dict[str, etree._Element],
        dict[str, etree._Element],
        dict[str, etree._Element],
    ]:
        """FIX-#9: rebuild all paragraph lookup tables from a fresh root.

        When ``_apply_source_formatting`` returns a brand-new lxml tree
        (the ``etree.fromstring()`` path), all the lookup references
        previously built (body_paragraphs, all_paragraphs, wt_text_map,
        body_paragraph_text_map, all_paragraph_text_map) point to elements
        in the discarded old tree. Subsequent backfills modify stale
        elements that never make it into the output document.

        This helper recomputes all 5 lookup references from a fresh
        ``root`` so the next backfill iteration operates on the current
        tree state.

        Returns (body_paragraphs, all_paragraphs, wt_text_map,
        body_paragraph_text_map, all_paragraph_text_map).
        """
        from orf.channels.xliff2docx.matcher import collect_all_paragraphs

        body = root.find("w:body", WORD_NS_MAP)
        body_paragraphs = body.xpath("./w:p", namespaces=WORD_NS_MAP) if body is not None else []
        all_paragraphs = collect_all_paragraphs(root)

        wt_text_map: dict[str, etree._Element] = {}
        for t_elem in root.xpath("//w:t", namespaces=WORD_NS_MAP):
            if t_elem.text:
                t_norm = re.sub(r"\s+", " ", t_elem.text).strip()
                if t_norm:
                    wt_text_map[t_norm] = t_elem

        body_paragraph_text_map: dict[str, etree._Element] = {}
        for p in body_paragraphs:
            text_runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
            text_content = "".join(t.text or "" for t in text_runs)
            normalized = re.sub(r"\s+", " ", text_content).strip()
            if normalized:
                body_paragraph_text_map[normalized] = p

        all_paragraph_text_map: dict[str, etree._Element] = {}
        for p in root.xpath("//w:p", namespaces=WORD_NS_MAP):
            text_runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
            text_content = "".join(t.text or "" for t in text_runs)
            normalized = re.sub(r"\s+", " ", text_content).strip()
            if normalized:
                all_paragraph_text_map[normalized] = p

        return (
            body_paragraphs,
            all_paragraphs,
            wt_text_map,
            body_paragraph_text_map,
            all_paragraph_text_map,
        )

    # ── Image delegation ────────────────────────────────────────────

    def inject_images(
        self,
        skeleton_path: Path | str,
        images: list[ImagePlacement],
        output_path: Path | str,
    ) -> tuple[list[ImagePlacement], list[ImagePlacement]]:
        """Inject images into DOCX skeleton.

        Delegates to ``orf.channels.xliff2docx.images.inject_images_into_docx``.
        """
        from orf.channels.xliff2docx.images import inject_images_into_docx

        return inject_images_into_docx(
            skeleton_path, images, output_path,
            self.skeleton_loader,
            get_image_bytes_fn=self._get_image_bytes,
            get_image_dimensions_fn=self._get_image_dimensions,
        )

    def _paragraph_already_has_drawing(
        self,
        root: etree._Element,
        cx: int,
        cy: int,
    ) -> bool:
        """True if ``root`` already contains a ``<w:drawing>`` with matching extent.

        Delegates to ``orf.channels.xliff2docx.images.paragraph_already_has_drawing``.
        """
        from orf.channels.xliff2docx.images import paragraph_already_has_drawing

        return paragraph_already_has_drawing(root, cx, cy)

    def _inject_floating_image(
        self,
        root: etree._Element,
        img: ImagePlacement,
        files: dict[str, bytes],
        namelist: list[str],
    ) -> bool:
        """Inject a single floating image as ``w:drawing > wp:anchor``.

        Delegates to ``orf.channels.xliff2docx.images.inject_floating_image``.
        """
        from orf.channels.xliff2docx.images import inject_floating_image

        return inject_floating_image(
            root, img, files, namelist,
            self._get_image_bytes,
            self._get_image_dimensions,
        )

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
        """Build ``w:drawing > wp:anchor`` element string for a floating image.

        Delegates to ``orf.channels.xliff2docx.images.create_floating_anchor_xml``.
        """
        from orf.channels.xliff2docx.images import create_floating_anchor_xml

        return create_floating_anchor_xml(
            rId, cx, cy, pos_h, pos_v, relative_h, relative_v,
        )

    def _get_image_bytes(self, img: ImagePlacement) -> bytes:
        """Extract image bytes from an ImagePlacement.

        Delegates to ``orf.channels.xliff2docx.images.get_image_bytes``.
        """
        from orf.channels.xliff2docx.images import get_image_bytes

        return get_image_bytes(img)

    def _get_image_dimensions(
        self, img: ImagePlacement, img_bytes: bytes
    ) -> tuple[int, int]:
        """Get image dimensions from ImagePlacement or via PIL.

        Delegates to ``orf.channels.xliff2docx.images.get_image_dimensions``.
        """
        from orf.channels.xliff2docx.images import get_image_dimensions

        return get_image_dimensions(img, img_bytes)

    def _add_image_to_zip(
        self,
        img_bytes: bytes,
        mime_type: str,
        files: dict[str, bytes],
        namelist: list[str],
    ) -> str:
        """Add an image to the DOCX ZIP's ``word/media/`` and update relationships.

        Delegates to ``orf.channels.xliff2docx.images.add_image_to_zip``.
        """
        from orf.channels.xliff2docx.images import add_image_to_zip

        return add_image_to_zip(img_bytes, mime_type, files, namelist)

    def _create_drawing_xml(
        self,
        rId: str,
        cx: int,
        cy: int,
    ) -> str:
        """Create a ``<w:drawing>`` XML string with ``<wp:inline>``.

        Delegates to ``orf.channels.xliff2docx.images.create_drawing_xml``.
        """
        from orf.channels.xliff2docx.images import create_drawing_xml

        return create_drawing_xml(rId, cx, cy)

    # ── Main convert method (orchestration, kept here) ──────────────

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
            logger.warning("Failed to load skeleton: %s", e, exc_info=True)
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
            # 3. For each trans-unit, backfill target text.
            total = len(trans_units)
            log_every = max(50, total // 20) if total else 1
            root = etree.fromstring(document_xml.encode("utf-8"))
            body = root.find("w:body", WORD_NS_MAP)
            body_paragraphs = body.xpath("./w:p", namespaces=WORD_NS_MAP)

            # Phase B.3 setup: build flat paragraph list matching OPP's extraction order
            all_paragraphs = self._collect_all_paragraphs(root)
            body_paragraphs = body.xpath("./w:p", namespaces=WORD_NS_MAP)

            # ── O(P+M) lookup indexes: built ONCE before the trans-unit loop ──
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

            # Pattern B: exact match on all paragraphs
            all_paragraph_text_map: dict[str, etree._Element] = {}
            for p in root.xpath("//w:p", namespaces=WORD_NS_MAP):
                text_runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
                text_content = "".join(t.text or "" for t in text_runs)
                normalized = re.sub(r"\s+", " ", text_content).strip()
                if normalized:
                    all_paragraph_text_map[normalized] = p

            # Build a mapping from original Chinese text → target text
            chinese_to_target: dict[str, str] = {}

            # OPP#80 Wave 1: classify table-cell units once per conversion
            # (whole-cell vs per-paragraph; mixed/duplicate resolution).
            table_cell_plan = build_table_cell_plan(trans_units)

            for idx, tu in enumerate(trans_units, 1):
                tu_id = tu["id"]
                target_text = tu.get("target", "")
                inline_elements = tu.get("inline_elements", [])

                if not target_text:
                    continue

                # Phase B.2: position-based lookup via resname="para_index_N"
                if tu.get("para_index") is not None:
                    try:
                        self._backfill_by_position(
                            root, body_paragraphs,
                            tu["para_index"],
                            target_text,
                        )
                        # ORF#33: apply source inline formatting if LLM dropped bx/ex
                        root, _applied = self._apply_source_formatting(
                            root, inline_elements, target_text,
                        )
                        if _applied and root is not None:
                            # FIX-#9: rebuild lookup indexes from the new root
                            (body_paragraphs, all_paragraphs,
                             wt_text_map, body_paragraph_text_map,
                             all_paragraph_text_map) = self._rebuild_indexes(root)
                        continue
                    except Exception as e:
                        logger.warning(
                            f"Position-based backfill failed for unit {tu_id} "
                            f"at index {tu['para_index']}: {e}; "
                            "falling back to text matching"
                        )

                # Phase B.3: non_body_N position-based lookup
                non_body_idx = tu.get("non_body_index")
                if non_body_idx is not None:
                    if 0 <= non_body_idx < len(all_paragraphs):
                        # Bug #37: removed idx >= 9 guard — with textbox dedup
                        # removed from collect_all_paragraphs(), non_body indices
                        # now correctly align with OPP's paragraph list.
                        para_txbx = all_paragraphs[non_body_idx]
                        orig_zh = "".join(
                            t.text or "" for t in para_txbx.iter(
                                f"{{{W_NS}}}t"
                            )
                        ).strip()
                        if orig_zh and orig_zh not in chinese_to_target:
                            chinese_to_target[orig_zh] = target_text
                        self._backfill_by_non_body_position(
                            all_paragraphs,
                            non_body_idx,
                            target_text,
                        )
                        # ORF#33: apply source inline formatting if LLM dropped bx/ex
                        root, _applied = self._apply_source_formatting(
                            root, inline_elements, target_text,
                        )
                        if _applied and root is not None:
                            # FIX-#9: rebuild lookup indexes from the new root
                            (body_paragraphs, all_paragraphs,
                             wt_text_map, body_paragraph_text_map,
                             all_paragraph_text_map) = self._rebuild_indexes(root)
                        continue
                    else:
                        logger.warning(
                            f"non_body index {non_body_idx} out of range for "
                            f"unit {tu_id} (have {len(all_paragraphs)} total "
                            f"paragraphs); falling back to text matching"
                        )

                # Issue A / OPP#80 Wave 1: table_{t}_r{r}_c{c}[_para{p}]
                # positional lookup. Bare units keep the legacy whole-cell
                # write-back; ``_para{p}`` units write exactly one paragraph.
                table_cell = tu.get("table_cell")
                if table_cell is not None:
                    table_index, row, col, para = table_cell
                    disposition = table_cell_plan.get(
                        idx - 1, PROCESS_WHOLE_CELL
                    )
                    if disposition == SKIP_BARE_IN_PARA_CELL:
                        logger.warning(
                            "Skipping bare table_cell (%d, %d, %d) for unit "
                            "%s: the cell has per-paragraph (_para) units, "
                            "so whole-cell write-back would clobber them. "
                            "Original source text preserved.",
                            table_index, row, col, tu_id,
                        )
                        continue
                    if disposition == SKIP_DUPLICATE_PARA:
                        logger.warning(
                            "Skipping duplicate table_cell_para %s for unit "
                            "%s: that paragraph was already written by an "
                            "earlier unit.",
                            table_cell, tu_id,
                        )
                        continue
                    try:
                        cell_applied = self._backfill_by_table_cell(
                            root,
                            table_index,
                            row,
                            col,
                            target_text,
                            para,
                        )
                    except Exception as e:
                        logger.warning(
                            f"Table-cell backfill failed for unit {tu_id} "
                            f"at {table_cell}: {e}; falling back to text matching"
                        )
                        cell_applied = False
                    if cell_applied:
                        # ORF#33: apply source inline formatting if LLM dropped bx/ex
                        root, _applied = self._apply_source_formatting(
                            root, inline_elements, target_text,
                        )
                        if _applied and root is not None:
                            # FIX-#9: rebuild lookup indexes from the new root
                            (body_paragraphs, all_paragraphs,
                             wt_text_map, body_paragraph_text_map,
                             all_paragraph_text_map) = self._rebuild_indexes(root)
                        continue
                    if para is not None:
                        # Paragraph-scoped writes never fall back to text
                        # matching: a missed paragraph stays untouched rather
                        # than being guessed at (CONTRACT.md §Table Cell Coords).
                        logger.warning(
                            f"table_cell_para {table_cell} could not be "
                            f"written for unit {tu_id}; skipping (no "
                            f"text-match fallback for _para units)"
                        )
                        continue
                    logger.warning(
                        f"table_cell {table_cell} out of range or missing for "
                        f"unit {tu_id}; falling back to text matching"
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
                    # ORF#33: apply source inline formatting if LLM dropped bx/ex
                    root, _applied = self._apply_source_formatting(
                        root, inline_elements, target_text,
                    )
                    if _applied and root is not None:
                        # FIX-#9: rebuild lookup indexes from the new root
                        (body_paragraphs, all_paragraphs,
                         wt_text_map, body_paragraph_text_map,
                         all_paragraph_text_map) = self._rebuild_indexes(root)
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
            logger.warning("Failed to repack DOCX: %s", e, exc_info=True)
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
