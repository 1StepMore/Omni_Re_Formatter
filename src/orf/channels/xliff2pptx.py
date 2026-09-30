"""XLIFF to PPTX conversion channel with inline formatting preservation."""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from orf.converters.base import BaseConverter, ConversionResult
from orf.error_handlers.conversion_error import XLIFFParseError, InlineFormattingError
from orf.logging import get_logger
from orf.mcp.schemas import ImagePlacement
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.parsers.manifest import Manifest
from orf.skeleton.inline_formatting import (
    InlineElement,
    PPTXInlineApplier,
    XLIFFInlineParser,
)
from orf.skeleton.skeleton_loader import SkeletonLoader
from orf.converters.options import ConverterOptions

logger = get_logger("channel.xliff2pptx")

# PPTX namespaces
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
A_PREFIX = f"{{{A_NS}}}"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
P_PREFIX = f"{{{P_NS}}}"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Open Packaging Conventions (OPC) relationships namespace and the two parts
# that define slide order.  ``p:sldIdLst`` in presentation.xml lists slides in
# presentation order; each ``p:sldId`` carries an unbraced ``r:id`` that must be
# resolved through presentation.xml.rels to its slide part.
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_PRESENTATION_PART = "ppt/presentation.xml"
_PRESENTATION_RELS_PART = "ppt/_rels/presentation.xml.rels"
_SLIDE_PREFIX = "ppt/slides/slide"
_SLIDE_SUFFIX = ".xml"

# Issue #58: OPP emits one trans-unit per PPTX table cell with resname
# ``table_{t}_r{r}_c{c}`` (raw node indices: t = table index accumulated across
# slides in sldIdLst order, r = direct ``a:tr`` index, c = direct ``a:tc``
# index).  OPP#80 Wave 0B: a future OPP release may add an optional
# ``_para{p}`` suffix (0-based paragraph index within the cell); the group is
# OPTIONAL so a bare ``table_{t}_r{r}_c{c}`` still matches with group(4) ==
# None and takes the exact same code path as before.  Deliberately DUPLICATED
# rather than imported: the twin lives in the sibling channel
# ``xliff2docx/parser.py:31`` (and ``xliff2html/writer.py:318`` repeats it
# too), so importing across channels would invert the dependency direction.
# Keep the one-line pattern in sync with those twins.
_TABLE_RESNAME_RE = re.compile(r"^table_(\d+)_r(\d+)_c(\d+)(?:_para(\d+))?$")

# XLIFF namespaces (multi-version support)
XLIFF_NS_1_2 = "urn:oasis:names:tc:xliff:document:1.2"
XLIFF_NS_1_1 = "urn:oasis:names:tc:xliff:document:1.1"
XLIFF_NS_2_0 = "urn:oasis:names:tc:xliff:document:2.0"
XLIFF_NS_MAP_1_2 = {"xliff": XLIFF_NS_1_2}
XLIFF_NS_MAP_1_1 = {"xliff": XLIFF_NS_1_1}
XLIFF_NS_MAP_2_0 = {"xliff": XLIFF_NS_2_0}
# Backwards-compatible default: 1.2 (matches xliff2docx sibling)
XLIFF_NS = XLIFF_NS_1_2
XLIFF_NS_MAP = XLIFF_NS_MAP_1_2


class XLIFF2PPTXConverter(BaseConverter):
    """XLIFF→PPTX backfill with inline formatting preservation.

    Loads a PPTX skeleton (PPTX is a ZIP), parses translated XLIFF segments,
    and applies translations with inline formatting back into the PPTX slides.
    """

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
    ):
        """Initialize the XLIFF to PPTX converter.

        Args:
            manifest: OPP manifest.json metadata.
            frontmatter: OL YAML frontmatter metadata.
        """
        super().__init__(manifest, frontmatter)
        self.skeleton_loader = SkeletonLoader()
        self.inline_parser = XLIFFInlineParser()
        self.pptx_applier = PPTXInlineApplier()

    @property
    def supported_format(self) -> str:
        """Return the supported output format."""
        return "PPTX"

    def validate_input(self, input_path: Path | str) -> bool:
        """Validate that the input PPTX skeleton exists.

        Args:
            input_path: Path to the PPTX skeleton file.

        Returns:
            True if valid PPTX file exists.
        """
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() in (".pptx", ".zip")

    def convert(  # type: ignore[override]
        self,
        input_path: Path | str,
        xliff_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        """Convert XLIFF translation back to PPTX with inline formatting.

        Args:
            input_path: Path to the PPTX skeleton file (ZIP archive).
            xliff_path: Path to the XLIFF translation file.
            output_path: Path for the output PPTX file.
            options: Converter options (slide_mapping, etc.).

        Returns:
            ConversionResult with success status and output path.
        """
        pptx_skeleton = Path(input_path)
        xliff_path = Path(xliff_path)
        output_path = Path(output_path)

        warnings: list[str] = []

        # 1. Load PPTX skeleton (PPTX is a ZIP)
        try:
            skeleton_data = self._load_pptx_skeleton(pptx_skeleton)
            slide_files = skeleton_data["files"]
            logger.info(f"Loaded PPTX skeleton with {len(slide_files)} files")
        except Exception as e:
            logger.warning("Failed to load PPTX skeleton: %s", e, exc_info=True)
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to load PPTX skeleton: {e}"],
            )

        # 2. Parse XLIFF
        try:
            xliff_data = self._parse_xliff(xliff_path)
            logger.info(f"Parsed XLIFF with {len(xliff_data['units'])} translation units")
        except XLIFFParseError as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to parse XLIFF: {e}"],
            )

        modified_slides: dict[str, bytes] = {}
        # 3. Apply translations to slides
        try:
            modified_slides = self._apply_translations_to_slides(
                slide_files,
                xliff_data,
                options,
            )
        except InlineFormattingError as e:
            warnings.append(f"Inline formatting issue: {e}")
            # Continue with best-effort
            modified_slides = slide_files

        # 4. Repack as PPTX
        try:
            self._repack_pptx(output_path, modified_slides, slide_files)
            logger.info(f"Repacked PPTX to {output_path}")
        except Exception as e:
            logger.warning("Failed to repack PPTX: %s", e, exc_info=True)
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to repack PPTX: {e}"],
            )

        return ConversionResult(
            output_path=output_path,
            success=True,
            warnings=warnings,
            metadata={
                "slides_processed": len(modified_slides),
                "units_translated": len(xliff_data["units"]),
            },
        )

    def _load_pptx_skeleton(self, pptx_path: Path) -> dict[str, Any]:
        """Load PPTX skeleton file and extract its contents.

        PPTX is a ZIP archive with slides stored in ppt/slides/slideN.xml.

        Args:
            pptx_path: Path to the PPTX file.

        Returns:
            dict with keys: files (dict of filename -> bytes), bytes (bytes)
        """
        files: dict[str, bytes] = {}
        with zipfile.ZipFile(str(pptx_path), "r") as zf:
            for name in zf.namelist():
                SkeletonLoader._validate_zip_entry_name(name)
            for name in zf.namelist():
                files[name] = zf.read(name)

        return {"files": files, "bytes": b""}

    def _parse_xliff(self, xliff_path: Path) -> dict[str, list[dict[str, object]]]:
        """Parse XLIFF file and extract translation units.

        Supports XLIFF 1.1, 1.2, and 2.0. The version is auto-detected from
        the root element's namespace, with a 1.2 → 1.1 → 2.0 fallback chain
        for malformed files where the namespace is undeclared.

        Args:
            xliff_path: Path to the XLIFF file.

        Returns:
            dict with 'units' list of translation units.

        Raises:
            XLIFFParseError: If XLIFF cannot be parsed.
        """
        try:
            tree = etree.parse(str(xliff_path))
            root = tree.getroot()
        except etree.XMLSyntaxError as e:
            raise XLIFFParseError(
                str(xliff_path),
                f"XML syntax error: {e}",
            )

        units: list[dict[str, object]] = []

        units = self._parse_xliff_trans_units(root, XLIFF_NS_MAP_1_2)
        if not units:
            units = self._parse_xliff_trans_units(root, XLIFF_NS_MAP_1_1)
        if not units:
            units = self._parse_xliff_2_0_units(root)

        return {"units": units}

    def _parse_xliff_trans_units(
        self, root: etree._Element, ns_map: dict[str, str]
    ) -> list[dict[str, object]]:
        """Parse XLIFF 1.x trans-unit elements using the given namespace map.

        Args:
            root: Parsed XML root element.
            ns_map: Namespace map (e.g. XLIFF_NS_MAP_1_2 or _1_1).

        Returns:
            List of trans-unit dicts.
        """
        units: list[dict[str, object]] = []
        for unit in root.xpath("//xliff:trans-unit", namespaces=ns_map):
            unit_id = unit.get("id")
            source_el = unit.find("xliff:source", namespaces=ns_map)
            target_el = unit.find("xliff:target", namespaces=ns_map)

            source_text = self._get_element_text(source_el) if source_el is not None else ""
            target_text = self._get_element_text(target_el) if target_el is not None else ""

            inline_elements: list[InlineElement] = []
            if target_text:
                inline_elements = self.inline_parser.parse_from_segment(target_text)

            units.append({
                "id": unit_id,
                "source": source_text,
                "target": target_text,
                "inline_elements": inline_elements,
                "resname": unit.get("resname"),
            })
        return units

    def _parse_xliff_2_0_units(
        self, root: etree._Element
    ) -> list[dict[str, object]]:
        """Parse XLIFF 2.0 <unit>/<segment> elements.

        Each <segment> inside a <unit> becomes one translation record. If a
        <unit> has no <segment>, the whole <unit> is treated as one record.

        XLIFF 2.0 has no ``resname`` attribute, so positional ``table_*``
        units are only produced for XLIFF 1.x; a 2.0 file always backfills
        through the text-matching path.

        Args:
            root: Parsed XML root element.

        Returns:
            List of unit dicts.
        """
        units: list[dict[str, object]] = []
        ns_map = XLIFF_NS_MAP_2_0
        for unit in root.xpath("//xliff:unit", namespaces=ns_map):
            unit_id = unit.get("id")
            segments = unit.findall("xliff:segment", namespaces=ns_map)
            if segments:
                for seg in segments:
                    seg_id = seg.get("id") or unit_id
                    source_el = seg.find("xliff:source", namespaces=ns_map)
                    target_el = seg.find("xliff:target", namespaces=ns_map)
                    source_text = (
                        self._get_element_text(source_el) if source_el is not None else ""
                    )
                    target_text = (
                        self._get_element_text(target_el) if target_el is not None else ""
                    )
                    inline_elements: list[InlineElement] = self._parse_inline(target_text)
                    units.append({
                        "id": seg_id,
                        "source": source_text,
                        "target": target_text,
                        "inline_elements": inline_elements,
                    })
            else:
                source_el = unit.find("xliff:source", namespaces=ns_map)
                target_el = unit.find("xliff:target", namespaces=ns_map)
                source_text = (
                    self._get_element_text(source_el) if source_el is not None else ""
                )
                target_text = (
                    self._get_element_text(target_el) if target_el is not None else ""
                )
                inline_elements = self._parse_inline(target_text)
                units.append({
                    "id": unit_id,
                    "source": source_text,
                    "target": target_text,
                    "inline_elements": inline_elements,
                })
        return units

    def _parse_inline(self, target_text: str) -> list[InlineElement]:
        """Parse inline elements from a target string.

        Args:
            target_text: Translated target text.

        Returns:
            List of inline elements (empty if target_text is empty).
        """
        if not target_text:
            return []
        return self.inline_parser.parse_from_segment(target_text)


    def _get_element_text(self, element: etree._Element) -> str:
        """Get concatenated text content from an element.

        Args:
            element: lxml element.

        Returns:
            Text content (may be empty string).
        """
        if element is None:
            return ""
        parts = []
        if element.text:
            parts.append(element.text)
        for child in element:
            parts.append(self._get_element_text(child))
            if child.tail:
                parts.append(child.tail)
        return "".join(parts)

    def _apply_translations_to_slides(
        self,
        slide_files: dict[str, bytes],
        xliff_data: dict[str, list[dict[str, object]]],
        options: ConverterOptions | None = None,
    ) -> dict[str, bytes]:
        """Apply translation units to PPTX slide XML files.

        Args:
            slide_files: Dict of filename -> file content bytes.
            xliff_data: Parsed XLIFF data with translation units.
            options: Additional options.

        Returns:
            Modified slide files dict.
        """
        modified = dict(slide_files)

        table_units: list[tuple[int, int, int, int | None, dict[str, object]]] = []
        trans_map: dict[str, Any] = {}
        for unit in xliff_data["units"]:
            table_cell = self._parse_table_resname(unit.get("resname"))
            if table_cell is not None:
                # Trap #1: table units are applied ONLY positionally.  If they
                # stayed in trans_map the text pass would write a cell target
                # into any other run that happens to share the source text.
                table_units.append((*table_cell, unit))
                continue
            source = str(unit["source"])
            target = str(unit["target"])
            if source and target:
                trans_map[source] = {
                    "target": target,
                    "inline_elements": unit["inline_elements"],
                }

        slide_roots: list[tuple[str, etree._Element]] = []
        total_tables = 0
        for filename in self._ordered_slide_filenames(modified):
            if filename not in modified:
                continue
            if not (
                filename.startswith(_SLIDE_PREFIX)
                and filename.endswith(_SLIDE_SUFFIX)
            ):
                continue
            root = etree.fromstring(modified[filename])
            slide_roots.append((filename, root))
            total_tables += len(list(root.iter(f"{A_PREFIX}tbl")))

        if table_units:
            for table_t, row, col, _para, _unit in table_units:
                if table_t >= total_tables:
                    logger.warning(
                        "resname table_%d_r%d_c%d out of range (have %d tables)",
                        table_t, row, col, total_tables,
                    )

        t_offset = 0
        for filename, root in slide_roots:
            n_tables = len(list(root.iter(f"{A_PREFIX}tbl")))
            changed = self._apply_table_units_to_root(
                root, table_units, t_offset, n_tables
            )
            if self._apply_translation_to_slide_root(root, trans_map):
                changed = True
            if changed:
                modified[filename] = etree.tostring(
                    root,
                    encoding="utf-8",
                    xml_declaration=True,
                )
            t_offset += n_tables

        return modified

    def _ordered_slide_filenames(self, files: dict[str, bytes]) -> list[str]:
        """Resolve slide part names into presentation order.

        Reads ``p:sldIdLst`` in ``ppt/presentation.xml`` and resolves each
        ``p:sldId`` ``r:id`` through ``ppt/_rels/presentation.xml.rels``, so the
        global ``table_{t}`` accumulator matches OPP's ``enumerate(prs.slides)``
        (a numeric filename sort would not: a valid pptx can list
        ``slide10.xml`` before ``slide2.xml``, and ``sldIdLst`` order can itself
        disagree with filename order).

        Duplicate ``p:sldId`` entries are kept in order (a part referenced twice
        is counted twice, matching OPP).  An unresolvable ``r:id`` warns and
        consumes no table index.  Orphan slide parts not referenced by
        ``sldIdLst`` are IGNORED - unlike the previous name-based iteration - so
        OPP and ORF never disagree about which parts exist.
        """
        presentation = files.get(_PRESENTATION_PART)
        rels = files.get(_PRESENTATION_RELS_PART)
        if presentation is None or rels is None:
            logger.warning(
                "%s or %s missing; falling back to ZIP-name slide order",
                _PRESENTATION_PART,
                _PRESENTATION_RELS_PART,
            )
            return self._name_ordered_slide_filenames(files)

        try:
            pres_root = etree.fromstring(presentation)
            rels_root = etree.fromstring(rels)
        except etree.XMLSyntaxError as e:
            logger.warning(
                "Could not parse presentation.xml or its rels (%s); "
                "falling back to ZIP-name slide order",
                e,
            )
            return self._name_ordered_slide_filenames(files)

        rid_to_target = {
            rel.get("Id"): rel.get("Target")
            for rel in rels_root.findall(f"{{{_REL_NS}}}Relationship")
            if rel.get("Id") and rel.get("Target")
        }

        ordered: list[str] = []
        for sld in pres_root.xpath(".//p:sldIdLst/p:sldId", namespaces={"p": P_NS}):
            rid = sld.get(f"{{{R_NS}}}id")
            target = rid_to_target.get(rid) if rid else None
            if target is None:
                logger.warning(
                    "sldId r:id=%r has no resolvable relationship; skipping "
                    "(no table index consumed)",
                    rid,
                )
                continue
            part = self._resolve_rels_target(target)
            if part not in files:
                logger.warning(
                    "sldId r:id=%r resolves to missing part %r; skipping",
                    rid,
                    part,
                )
                continue
            ordered.append(part)

        if not ordered:
            logger.warning(
                "sldIdLst resolved no existing slide parts; falling back to "
                "ZIP-name slide order",
            )
            return self._name_ordered_slide_filenames(files)
        return ordered

    def _name_ordered_slide_filenames(self, files: dict[str, bytes]) -> list[str]:
        return [
            name
            for name in files
            if name.startswith(_SLIDE_PREFIX) and name.endswith(_SLIDE_SUFFIX)
        ]

    def _resolve_rels_target(self, target: str) -> str:
        if target.startswith("/"):
            return posixpath.normpath(target.lstrip("/"))
        return posixpath.normpath(posixpath.join("ppt", target))

    def _parse_table_resname(
        self, resname: object
    ) -> tuple[int, int, int, int | None] | None:
        if not isinstance(resname, str):
            return None
        match = _TABLE_RESNAME_RE.match(resname)
        if match is None:
            return None
        para_str = match.group(4)
        return (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(para_str) if para_str is not None else None,
        )

    def _apply_table_units_to_root(
        self,
        root: etree._Element,
        table_units: list[tuple[int, int, int, int | None, dict[str, object]]],
        t_offset: int,
        n_tables: int,
    ) -> bool:
        """Write positional table units belonging to one slide, in place.

        Cells resolve by raw direct-child indices (``a:tbl`` -> ``a:tr`` ->
        ``a:tc``), exactly as OPP numbers them.  The target is distributed
        across the origin cell's own ``a:p``/``a:r`` runs; ``hMerge``/
        ``vMerge`` covered siblings have no unit and are left untouched.
        Out-of-range coordinates warn and skip.
        """
        tables = list(root.iter(f"{A_PREFIX}tbl"))
        changed = False
        for table_t, row, col, _para, unit in table_units:
            local = table_t - t_offset
            if not (0 <= local < n_tables):
                continue
            rows = tables[local].findall(f"{A_PREFIX}tr")
            if not (0 <= row < len(rows)):
                logger.warning(
                    "resname table_%d_r%d_c%d out of range (have %d rows)",
                    table_t, row, col, len(rows),
                )
                continue
            cells = rows[row].findall(f"{A_PREFIX}tc")
            if not (0 <= col < len(cells)):
                logger.warning(
                    "resname table_%d_r%d_c%d out of range (have %d cells)",
                    table_t, row, col, len(cells),
                )
                continue
            text_elements: list[etree._Element] = []
            for p in cells[col].iter(f"{A_PREFIX}p"):
                for r_element in p.findall(f"{A_PREFIX}r"):
                    text_elements.extend(r_element.findall(f"{A_PREFIX}t"))
            if not text_elements:
                logger.warning(
                    "resname table_%d_r%d_c%d has no runs to backfill",
                    table_t, row, col,
                )
                continue
            self._distribute_text_across_pptx_runs(
                text_elements, str(unit["target"])
            )
            changed = True
        return changed

    def _distribute_text_across_pptx_runs(
        self,
        text_elements: list[etree._Element],
        target_text: str,
    ) -> None:
        """Distribute target_text across a cell's flat ``a:t`` list.

        Mirrors ``xliff2docx.matcher._distribute_text_across_runs``: split on
        newlines, one line per run; extra lines append to the last run; tail
        runs are cleared so no source text is left behind.
        """
        lines = target_text.split("\n")
        for i, line in enumerate(lines):
            if i < len(text_elements):
                text_elements[i].text = line
            else:
                last = text_elements[-1]
                last.text = (last.text or "") + "\n" + line
        for element in text_elements[len(lines):]:
            element.text = ""

    def _apply_translation_to_slide_root(
        self,
        root: etree._Element,
        trans_map: dict[str, Any],
    ) -> bool:
        """Apply translations to a parsed slide root element (mutate in place).

        The caller parses each slide once, passes the root here, and serializes
        the root once at the end. Simple text replacement mutates ``t.text`` in
        place; inline formatting goes through the string-based
        ``PPTXInlineApplier`` and the resulting XML is re-parsed into the
        same root.

        Args:
            root: The parsed slide XML root element (will be mutated).
            trans_map: Mapping from source text to translation data.

        Returns:
            True if any changes were made to the root.
        """
        changed = False

        for r_element in root.iter(f"{A_PREFIX}r"):
            t_elements = list(r_element.iter(f"{A_PREFIX}t"))
            if not t_elements:
                continue

            run_text = ""
            for t in t_elements:
                if t.text:
                    run_text += t.text

            if run_text in trans_map:
                trans_data = trans_map[run_text]
                target_text = trans_data["target"]
                inline_elements = trans_data["inline_elements"]

                if inline_elements:
                    xml_content = etree.tostring(root, encoding="unicode")
                    formatted_xml = self.pptx_applier.apply_formatting(
                        xml_content,
                        inline_elements,
                        target_text,
                    )
                    if formatted_xml != xml_content:
                        new_root = etree.fromstring(formatted_xml.encode("utf-8"))
                        for child in list(root):
                            root.remove(child)
                        for child in list(new_root):
                            root.append(child)
                        changed = True

                for i, t in enumerate(t_elements):
                    t.text = target_text if i == 0 else ""
                changed = True

        return changed

    def _repack_pptx(
        self,
        output_path: Path,
        modified_files: dict[str, bytes],
        original_files: dict[str, bytes],
    ) -> None:
        """Repack modified files into a PPTX archive.

        Args:
            output_path: Path to write the output PPTX.
            modified_files: Dict of modified filename -> content.
            original_files: Original files dict for unchanged entries.
        """
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Write all files, using modified version if available
            for name, data in original_files.items():
                if name in modified_files:
                    zf.writestr(name, modified_files[name])
                else:
                    zf.writestr(name, data)

    def inject_images(
        self,
        skeleton_path: Path | str,
        images: list[ImagePlacement],
        output_path: Path | str,
    ) -> tuple[list[ImagePlacement], list[ImagePlacement]]:
        """Inject images into PPTX skeleton at specified slide positions.

        Args:
            skeleton_path: Path to skeleton PPTX file.
            images: List of ImagePlacement objects from OPP.
            output_path: Path to write the output PPTX.

        Returns:
            Tuple of (injected_images, orphaned_images).
        """
        skeleton_path = Path(skeleton_path)
        output_path = Path(output_path)

        orphaned: list[ImagePlacement] = []
        injected: list[ImagePlacement] = []

        positioned = [img for img in images if img.slide_index is not None]
        unpositioned = [img for img in images if img.slide_index is None]

        if unpositioned:
            for img in unpositioned:
                logger.warning(
                    "Image has no slide_index, appending to end: mime_type=%s",
                    img.mime_type,
                )
            orphaned.extend(unpositioned)

        if not positioned:
            if images:
                with zipfile.ZipFile(skeleton_path, "r") as zf_in:
                    for item in zf_in.namelist():
                        SkeletonLoader._validate_zip_entry_name(item)
                    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
                        for item in zf_in.namelist():
                            zf_out.writestr(item, zf_in.read(item))
            return (injected, orphaned)

        try:
            skeleton_data = self._load_pptx_skeleton(skeleton_path)
            files = skeleton_data["files"]
        except Exception as e:
            logger.error("Failed to load PPTX skeleton for image injection: %s", e)
            orphaned.extend(images)
            return (injected, orphaned)

        img_by_slide: dict[int, list[ImagePlacement]] = {}
        for img in positioned:
            idx = img.slide_index
            assert idx is not None, "positioned images must have slide_index"
            if idx not in img_by_slide:
                img_by_slide[idx] = []
            img_by_slide[idx].append(img)

        slide_numbers = sorted(img_by_slide.keys())

        for slide_num in slide_numbers:
            slide_file_name = f"ppt/slides/slide{slide_num + 1}.xml"
            if slide_file_name not in files:
                logger.warning(
                    "Slide %d not found in skeleton, skipping images",
                    slide_num,
                )
                orphaned.extend(img_by_slide[slide_num])
                continue

            slide_xml = files[slide_file_name].decode("utf-8")

            for i, img in enumerate(img_by_slide[slide_num]):
                try:
                    img_bytes = self._get_image_bytes(img)
                    rId = self._add_image_to_pptx(img_bytes, img.mime_type, files, slide_num)
                    width, height = self._get_image_dimensions(img, img_bytes)
                    pic_xml = self._create_pic_xml(rId, width, height, slide_num)
                    pic_elem = etree.fromstring(pic_xml)

                    root = etree.fromstring(slide_xml.encode("utf-8"))
                    sp_tree = root.find(f".//{P_PREFIX}spTree")
                    if sp_tree is not None:
                        sp_tree.append(pic_elem)
                        slide_xml = etree.tostring(root, encoding="unicode", xml_declaration=True)
                        files[slide_file_name] = slide_xml.encode("utf-8")
                        injected.append(img)
                        logger.debug(
                            "Injected image at slide %d: rId=%s, mime=%s",
                            slide_num,
                            rId,
                            img.mime_type,
                        )
                    else:
                        orphaned.append(img)
                except Exception as e:
                    logger.error("Failed to inject image into slide %d: %s", slide_num, e)
                    orphaned.append(img)

        self._repack_pptx(output_path, files, files)

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
            logger.exception("Failed to get image dimensions, using default 914400x685800")
            return (914400, 685800)

    def _add_image_to_pptx(
        self,
        img_bytes: bytes,
        mime_type: str,
        files: dict[str, bytes],
        slide_num: int,
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
        media_name = f"ppt/media/{dedup_name}"

        existing_name = None
        for name, data in files.items():
            if name.startswith("ppt/media/") and data == img_bytes:
                existing_name = name
                break

        if existing_name:
            rid = existing_name.replace("ppt/media/", "rId")
            return rid

        files[media_name] = img_bytes

        slide_rels_name = f"ppt/slides/_rels/slide{slide_num + 1}.xml.rels"
        if slide_rels_name not in files:
            rels_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/slide0.png"/></Relationships>'
            files[slide_rels_name] = rels_xml.encode("utf-8")

        rels_xml = files[slide_rels_name].decode("utf-8")
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
        new_rel.set(
            "Type",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        )
        new_rel.set("Target", f"media/{dedup_name}")

        files[slide_rels_name] = etree.tostring(root, encoding="unicode").encode("utf-8")

        return new_rid

    def _create_pic_xml(
        self,
        rId: str,
        cx: int,
        cy: int,
        slide_num: int,
    ) -> str:
        x = 457200
        y = 457200
        pic = f'''<p:pic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:nvPicPr>
    <p:cNvPr id="1" name="Picture" descr="image"/>
    <p:cNvPicPr>
      <a:picLocks noChangeAspect="1"/>
    </p:cNvPicPr>
    <p:nvPr/>
  </p:nvPicPr>
  <p:blipFill>
    <a:blip r:embed="{rId}"/>
    <a:stretch>
      <a:fillRect/>
    </a:stretch>
  </p:blipFill>
  <p:spPr>
    <a:xfrm>
      <a:off x="{x}" y="{y}"/>
      <a:ext cx="{cx}" cy="{cy}"/>
    </a:xfrm>
    <a:prstGeom prst="rect">
      <a:avLst/>
    </a:prstGeom>
  </p:spPr>
</p:pic>'''
        return pic