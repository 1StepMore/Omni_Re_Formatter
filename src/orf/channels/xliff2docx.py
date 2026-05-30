"""XLIFF→DOCX backfill with inline formatting preservation."""

from __future__ import annotations

import base64
import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import unescape as _xml_unescape

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
            target_text = (
                "".join(target_el.itertext()) if target_el is not None else ""
            )

            # Extract inline elements from source
            source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
            inline_elements = self._extract_inline_elements(source_xml)

            trans_units.append({
                "id": tu_id,
                "source": source_text,
                "target": target_text,
                "inline_elements": inline_elements,
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
                target_text = (
                    "".join(target_el.itertext()) if target_el is not None else ""
                )

                source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                inline_elements = self._extract_inline_elements(source_xml)

                trans_units.append({
                    "id": tu_id,
                    "source": source_text,
                    "target": target_text,
                    "inline_elements": inline_elements,
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
                        target_text = (
                            "".join(target_el.itertext()) if target_el is not None else ""
                        )

                        source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                        inline_elements = self._extract_inline_elements(source_xml)

                        trans_units.append({
                            "id": seg_id,
                            "source": source_text,
                            "target": target_text,
                            "inline_elements": inline_elements,
                        })
                else:
                    # No segments, treat whole unit as one trans-unit
                    source_el = unit.find("xliff:source", namespaces=XLIFF_NS_MAP_2_0)
                    target_el = unit.find("xliff:target", namespaces=XLIFF_NS_MAP_2_0)

                    source_text = (
                        "".join(source_el.itertext()) if source_el is not None else ""
                    )
                    target_text = (
                        "".join(target_el.itertext()) if target_el is not None else ""
                    )

                    source_xml = etree.tostring(source_el, encoding="unicode") if source_el is not None else ""
                    inline_elements = self._extract_inline_elements(source_xml)

                    trans_units.append({
                        "id": unit_id,
                        "source": source_text,
                        "target": target_text,
                        "inline_elements": inline_elements,
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
        input_skeleton: Path | str,
        xliff_path: Path | str,
        output_path: Path | str,
        **options: Any,
    ) -> ConversionResult:
        """Convert XLIFF + skeleton to DOCX.

        Args:
            input_skeleton: Path to skeleton DOCX file.
            xliff_path: Path to XLIFF translation file.
            output_path: Path to output DOCX file.
            **options: Additional options (segment_mapping, etc.)

        Returns:
            ConversionResult with output path and metadata.
        """
        input_skeleton = Path(input_skeleton)
        xliff_path = Path(xliff_path)
        output_path = Path(output_path)

        warnings: list[str] = []
        inline_elements_applied = 0

        # 1. Load skeleton (original DOCX ZIP)
        try:
            skeleton_data = self.skeleton_loader.load_skeleton(str(input_skeleton))
            document_xml = skeleton_data["xml"]
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

        # 3. For each trans-unit, backfill target text
        for tu in trans_units:
            tu_id = tu["id"]
            target_text = tu.get("target", "")
            inline_elements = tu.get("inline_elements", [])

            if not target_text:
                # Skip empty translations
                continue

            try:
                document_xml = self._backfill_translation(
                    document_xml,
                    tu["source"],
                    target_text,
                    inline_elements,
                )
            except Exception as e:
                logger.warning(f"Failed to backfill trans-unit {tu_id}: {e}")
                warnings.append(f"Failed to backfill unit {tu_id}: {e}")

        # 4. Repack as DOCX
        try:
            self.skeleton_loader.repack_docx(str(output_path), document_xml)
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
        document_xml: str,
        source_text: str,
        target_text: str,
        inline_elements: list[InlineElement],
    ) -> str:
        """Backfill a single translation into document XML.

        Args:
            document_xml: The document.xml content.
            source_text: Original source text (for finding location).
            target_text: Translated target text.
            inline_elements: Inline formatting elements.

        Returns:
            Modified document_xml string (always returns modified xml, even if no match).
        """
        if not source_text and not target_text:
            return document_xml

        root = etree.fromstring(document_xml.encode("utf-8"))

        # E2E-06b fix: unescape XML entities in target_text before any processing
        # LiteLLM may return &lt;bx ... &gt; instead of <bx ...>
        target_text = _xml_unescape(target_text, {'&quot;': '"'})

        found = False
        source_normalized = re.sub(r'<[^>]+>', '', source_text) if source_text else ""

        if inline_elements:
            found = self._backfill_with_inline_elements(
                root, source_normalized, target_text, inline_elements
            )
        else:
            for t_elem in root.xpath("//w:t", namespaces=WORD_NS_MAP):
                if t_elem.text and source_normalized in t_elem.text:
                    found = True
                    t_elem.text = target_text

        if not found:
            paragraphs = root.xpath("//w:p", namespaces=WORD_NS_MAP)
            for p in paragraphs:
                text_runs = [t.text for t in p.xpath(".//w:t", namespaces=WORD_NS_MAP) if t.text]
                concat_text = "".join(text_runs)
                if source_normalized in concat_text:
                    found = self._backfill_split_runs(p, source_normalized, target_text)
                    break

        new_xml = etree.tostring(root, encoding="unicode", xml_declaration=False)
        return new_xml

    def _backfill_with_inline_elements(
        self,
        root: etree._Element,
        source_normalized: str,
        target_text: str,
        inline_elements: list[InlineElement],
    ) -> bool:
        """Backfill when source has inline formatting tags.

        Finds paragraphs where the source text (with inline tags stripped) matches,
        then applies translations preserving inline structure.
        """
        found = False
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
        """
        runs = paragraph.xpath(".//w:t", namespaces=WORD_NS_MAP)
        concat = "".join(r.text or "" for r in runs)

        if source_normalized not in concat:
            return False

        pos = concat.find(source_normalized)
        target_run_idx = None
        for i, r in enumerate(runs):
            run_text = r.text or ""
            run_start = concat.find(run_text, pos) if run_text else -1
            if run_start <= pos < run_start + len(run_text) or run_start < 0:
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

        # E2E-06b fix: unescape XML entities in target_text before writing
        # LiteLLM may return &lt;bx ... &gt; instead of <bx ...>
        safe_target = _xml_unescape(target_text, {'&quot;': '"'})
        runs[target_run_idx].text = safe_target
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
                open_formats[elem_id] = elem_type
            else:
                elem_id = match_obj.group(1)
                if elem_id in open_formats:
                    del open_formats[elem_id]

            remaining = remaining[match_obj.end():]

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
            t = etree.SubElement(r, f"{W}t")
            t.text = text

            has_formatting = any(
                fmt in ("bold", "italic", "underline", "double-underline", "single-underline")
                for fmt in active_formats.values()
            )
            if has_formatting:
                rpr = etree.SubElement(r, f"{W}rPr")
                if "bold" in active_formats.values():
                    etree.SubElement(rpr, f"{W}b")
                if "italic" in active_formats.values():
                    etree.SubElement(rpr, f"{W}i")
                for fmt_type, fmt_val in active_formats.items():
                    if fmt_type in ("underline", "double-underline", "single-underline"):
                        u_elem = etree.SubElement(rpr, f"{W}u")
                        if fmt_val == "double-underline":
                            u_elem.set(f"{W}val", "double")
                        elif fmt_val == "single-underline":
                            u_elem.set(f"{W}val", "single")
                        else:
                            u_elem.set(f"{W}val", "single")

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
        """Inject images into DOCX skeleton at specified paragraph positions.

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

        positioned = [img for img in images if img.paragraph_index is not None]
        unpositioned = [img for img in images if img.paragraph_index is None]

        if unpositioned:
            for img in unpositioned:
                logger.warning(
                    "Image has no paragraph_index, appending to end: mime_type=%s",
                    img.mime_type,
                )
            orphaned.extend(unpositioned)

        if not positioned:
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

        document_xml = skeleton_data["xml"]
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
                    rId = self._add_image_to_zip(img_bytes, img.mime_type, files, namelist)
                    width, height = self._get_image_dimensions(img, img_bytes)
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

        new_xml = etree.tostring(root, encoding="unicode", xml_declaration=False)

        files_copy = dict(files)
        files_copy["word/document.xml"] = new_xml.encode("utf-8")

        try:
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in files_copy.items():
                    zf.writestr(name, data)
        except Exception as e:
            logger.error("Failed to repack DOCX with images: %s", e)
            return (injected, orphaned + [img for img in positioned if img not in injected])

        if orphaned:
            logger.warning(
                "%d images could not be positioned and were not injected",
                len(orphaned),
            )

        return (injected, orphaned)

    def _get_image_bytes(self, img: ImagePlacement) -> bytes:
        if img.data_base64:
            return base64.b64decode(img.data_base64)
        if img.file_path:
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
        except Exception:
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
           xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
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