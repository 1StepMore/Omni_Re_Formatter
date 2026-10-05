"""XLIFF to XLSX backfill channel.

OPP packages an XLSX skeleton as ``<stem>.skeleton.zip`` holding the original
workbook bytes verbatim plus one archive-root sidecar, ``xliff_map.json``
(Omni_Pre_Processor#92), which records which spreadsheet cells were offered for
translation::

    {"version": 1,
     "sheets": {"<sheet>": [{"id": <XLIFF trans-unit id>,
                             "row": <1-based spreadsheet row>,
                             "cells": [{"ref": "A1", "translatable": true}, ...]}]}}

A trans-unit's ``<source>``/``<target>`` is one whole spreadsheet row joined by
``" | "`` — one segment per cell, blanks included, in column order — so the
segment index *is* the column index and ``cells[i]`` owns segment ``i``. Ids
have gaps (a sheet-header paragraph consumes one without recording a row), so
rows are always looked up by id, never by position.

This channel rewrites only the cell values OPP marked ``translatable: true``,
in the untouched skeleton workbook, with openpyxl. Everything else survives
because it is never touched: formulas (OPP marks a formula cell non-translatable
because its extracted text is the *cached* value), merged-range non-anchor
cells, numbers, dates, booleans, Excel error literals and blanks, plus the
styles, merged ranges, number formats, column widths and every sheet.

Fidelity: the openpyxl round trip is not full-fidelity OOXML. Charts, images,
pivot tables, data validation and part of the conditional formatting are
dropped on save — an accepted cost recorded in ACCEPTED_GAPS.md, not a silent
one. The tests assert what is preserved so the loss stays visible.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from orf.converters.base import (
    BaseConverter,
    ConversionResult,
    ErrorDetail,
    WarningDetail,
)
from orf.converters.options import ConverterOptions
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.logging import get_logger

logger = get_logger("channel.xliff2xlsx")

# Archive-root sidecar written by OPP's XLSXExtractor. The skeleton zip is the
# original workbook plus this single entry; nothing else is added or rewritten.
_XLIFF_MAP_ENTRY = "xliff_map.json"

# The only sidecar schema this channel understands (OPP's XLIFF_MAP_VERSION).
_XLIFF_MAP_VERSION = 1

# OPP builds a row's <source> as ``" | ".join(cell texts).strip()``. The trailing
# ``.strip()`` removes the padding whitespace at the row's edges, leaving a bare
# ``"|"`` whenever the first or last cell is blank (``"| Name | Qty"``,
# ``"a | b |"``, ``"|  |  |  |"``). ``\s*\|\s*`` is the exact inverse of that
# strip and keeps the segments aligned with the cells; splitting on a literal
# ``" | "`` would shift every segment after a blank edge cell onto the wrong
# cell, so the whitespace is part of the separator contract, not formatting.
_ROW_SEPARATOR = re.compile(r"\s*\|\s*")

# A cell reference as openpyxl writes it: column letters then a row number.
_CELL_REF = re.compile(r"^[A-Za-z]{1,3}[0-9]+$")


@dataclass(frozen=True)
class _CellMapping:
    """One cell of a mapped row, as the sidecar describes it."""

    ref: str
    translatable: bool


@dataclass(frozen=True)
class _RowMapping:
    """One spreadsheet row and the trans-unit that carries its text."""

    unit_id: str
    row: int
    cells: tuple[_CellMapping, ...]


@dataclass(frozen=True)
class _PlannedWrite:
    """A single resolved cell write."""

    sheet: str
    ref: str
    text: str


def _split_row(text: str) -> list[str]:
    """Split a trans-unit's row text into its per-cell segments."""
    return _ROW_SEPARATOR.split(text)


def _local_name(element: etree._Element) -> str:
    """Namespace-agnostic tag name (XLIFF 1.2 and 2.0 differ only here)."""
    return etree.QName(element).localname


def _element_text(element: etree._Element | None) -> str | None:
    """Full text of a ``<source>``/``<target>``, inline runs included.

    ``findtext`` would return only the element's own text and drop the tails of
    its inline children, truncating a row like ``Name | <bx id="1"/>Widget<ex/> |
    Qty`` to ``"Name | "``. ``itertext`` walks children and tails, and the empty
    ``<bx/>``/``<ex/>`` inline elements contribute no text of their own.
    """
    if element is None:
        return None
    return "".join(element.itertext())


def _parse_xliff_rows(path: Path) -> dict[str, tuple[str, str | None]]:
    """Return ``{trans-unit id: (source, target or None)}`` from an XLIFF file.

    Namespace-agnostic and XXE-hardened, like the JSON channel's parser. The
    file's own XML declaration governs the encoding (lxml honours it, and falls
    back to UTF-8), so :attr:`ConverterOptions.encoding` does not apply here.

    A unit without a ``<target>`` maps to ``None``: OPP writes the XLIFF before
    anything is translated, and an untranslated unit must leave its cells
    exactly as the skeleton holds them rather than write the English source
    back over them.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.parse(str(path), parser)
    units: dict[str, tuple[str, str | None]] = {}
    for element in tree.getroot().iter():
        if not isinstance(element.tag, str) or _local_name(element) != "trans-unit":
            continue
        unit_id = element.get("id")
        if unit_id is None:
            continue
        source: str | None = None
        target: str | None = None
        for child in element:
            if not isinstance(child.tag, str):
                continue
            name = _local_name(child)
            if name == "source" and source is None:
                source = _element_text(child)
            elif name == "target" and target is None:
                target = _element_text(child)
        if source is None:
            # No source text means no row text to correlate against; a mapped
            # row pointing here is reported as a missing trans-unit.
            continue
        units[unit_id] = (source, target)
    return units


def _parse_map(payload: Any, source_name: str) -> dict[str, list[_RowMapping]]:
    """Validate the sidecar's whole shape once, up front.

    Everything the write-back later trusts — version, sheet names, ids, cell
    refs and translatable flags — is checked here, so the write loop stays
    linear and a malformed map surfaces as one clear error instead of a
    KeyError in the middle of a save.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"{_XLIFF_MAP_ENTRY} in {source_name} is not a JSON object"
        )
    version = payload.get("version")
    if version != _XLIFF_MAP_VERSION:
        raise ValueError(
            f"{_XLIFF_MAP_ENTRY} in {source_name} declares version {version!r}; "
            f"this ORF build understands version {_XLIFF_MAP_VERSION}"
        )
    sheets = payload.get("sheets")
    if not isinstance(sheets, dict):
        raise ValueError(
            f"{_XLIFF_MAP_ENTRY} in {source_name} has no 'sheets' object"
        )

    parsed: dict[str, list[_RowMapping]] = {}
    for sheet_name, rows in sheets.items():
        if not isinstance(rows, list):
            raise ValueError(
                f"{_XLIFF_MAP_ENTRY}: sheet {sheet_name!r} is not a list of rows"
            )
        parsed[sheet_name] = [
            _parse_row(entry, index, sheet_name)
            for index, entry in enumerate(rows)
        ]
    return parsed


def _parse_row(entry: Any, index: int, sheet_name: str) -> _RowMapping:
    """Validate one mapped row of the sidecar."""
    where = f"{_XLIFF_MAP_ENTRY}: sheet {sheet_name!r} entry {index}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where} is not an object")
    unit_id = entry.get("id")
    if unit_id is None:
        raise ValueError(f"{where} has no trans-unit id")
    row = entry.get("row")
    if not isinstance(row, int) or isinstance(row, bool):
        raise ValueError(
            f"{where} (trans-unit {unit_id}) has no integer row number"
        )
    raw_cells = entry.get("cells")
    if not isinstance(raw_cells, list) or not raw_cells:
        raise ValueError(
            f"{where} (trans-unit {unit_id}) lists no cells"
        )

    cells: list[_CellMapping] = []
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, dict):
            raise ValueError(
                f"{where} (trans-unit {unit_id}) has a non-object cell entry"
            )
        ref = raw_cell.get("ref")
        if not isinstance(ref, str) or not _CELL_REF.match(ref):
            raise ValueError(
                f"{where} (trans-unit {unit_id}) has an invalid cell ref {ref!r}"
            )
        translatable = raw_cell.get("translatable")
        if not isinstance(translatable, bool):
            raise ValueError(
                f"{where} (trans-unit {unit_id}) cell {ref} has a non-boolean "
                f"translatable flag: {translatable!r}"
            )
        cells.append(_CellMapping(ref=ref.upper(), translatable=translatable))
    return _RowMapping(
        unit_id=str(unit_id), row=row, cells=tuple(cells)
    )


def _read_xlsx_skeleton(template: Path) -> tuple[bytes, dict[str, list[_RowMapping]]]:
    """Return the workbook bytes and the cell map carried by an XLSX skeleton.

    Mirrors ``xliff2html._read_html_template``'s shape — the input is OPP's
    skeleton, unpacked here rather than by the caller — with one difference the
    format forces: XLSX only ever arrives as the zip, because a bare workbook
    carries no cell map and therefore nothing to correlate a translation with.
    Unlike the HTML skeleton there is also no entry to choose between, so the
    workbook is every entry except the sidecar and the sidecar itself is never
    handed to openpyxl: it is not SpreadsheetML and is not declared in
    ``[Content_Types].xml``.
    """
    if template.suffix.lower() != ".zip":
        raise ValueError(
            f"{template.name} is a bare workbook, not an XLSX skeleton: XLSX "
            f"backfill needs OPP's <stem>.skeleton.zip, whose "
            f"{_XLIFF_MAP_ENTRY} sidecar says which cells were offered for "
            f"translation. Without it no cell can be written without guessing — "
            f"re-extract with `opp <file>.xlsx --target-format both` and pass "
            f"the skeleton."
        )
    with zipfile.ZipFile(template, "r") as archive:
        names = archive.namelist()
        if _XLIFF_MAP_ENTRY not in names:
            listed = ", ".join(sorted(names)[:3]) or "nothing"
            raise ValueError(
                f"Skeleton zip {template.name} holds no {_XLIFF_MAP_ENTRY} "
                f"sidecar (it holds {listed}), so it is not an XLSX skeleton from "
                f"OPP. Use the XLSX skeleton OPP writes next to the XLIFF."
            )
        try:
            payload = json.loads(archive.read(_XLIFF_MAP_ENTRY).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"{_XLIFF_MAP_ENTRY} in {template.name} is not valid JSON: {exc}"
            ) from exc
        workbook = io.BytesIO()
        with zipfile.ZipFile(workbook, "w", zipfile.ZIP_DEFLATED) as rebuilt:
            for info in archive.infolist():
                if info.filename == _XLIFF_MAP_ENTRY:
                    continue
                rebuilt.writestr(info, archive.read(info))
    return workbook.getvalue(), _parse_map(payload, template.name)


def _plan_writes(
    sheet_map: dict[str, list[_RowMapping]],
    units: dict[str, tuple[str, str | None]],
    sheet_names: list[str],
) -> tuple[list[_PlannedWrite], int]:
    """Correlate every mapped row with its trans-unit and resolve the writes.

    Returns ``(writes, untranslated_units)``. Raises ``ValueError`` when the map
    and the XLIFF disagree: a mapped trans-unit that the XLIFF does not have, a
    source that does not split into exactly one segment per cell, or a target
    that did not keep the ``" | "`` row structure. Each of those makes a cell
    index unreliable, and writing anyway would put a translation into the wrong
    cell, so the whole conversion fails and no workbook is produced.

    ``translatable: false`` is the other half of the rule: the producer marks a
    cell non-translatable when its extracted text is not prose — a formula's
    cached value, a merged range's non-anchor cell, a number, date, boolean,
    Excel error literal or blank — and the plan simply never emits a write for
    those, so the skeleton keeps the original value, formula included.
    """
    writes: list[_PlannedWrite] = []
    untranslated = 0
    for sheet_name, rows in sheet_map.items():
        if sheet_name not in sheet_names:
            raise ValueError(
                f"{_XLIFF_MAP_ENTRY} maps sheet {sheet_name!r}, which is not in "
                f"the skeleton workbook (sheets: {', '.join(sheet_names) or 'none'})"
            )
        for mapped in rows:
            where = (
                f"sheet {sheet_name!r} row {mapped.row} "
                f"(trans-unit {mapped.unit_id})"
            )
            unit = units.get(mapped.unit_id)
            if unit is None:
                raise ValueError(
                    f"{where} is mapped to a cell set, but the XLIFF has no "
                    f"trans-unit with id {mapped.unit_id!r}: ORF cannot tell "
                    f"whether the unit was dropped or lost, so no cell is written"
                )
            source, target = unit
            if target is None:
                untranslated += 1
                continue

            source_segments = _split_row(source)
            if len(source_segments) != len(mapped.cells):
                raise ValueError(
                    f"{where}: the source splits into {len(source_segments)} "
                    f"' | ' segment(s) but the map lists {len(mapped.cells)} "
                    f"cell(s), so the segments cannot be matched to cells by "
                    f"position (a cell whose own text contains ' | ' looks like "
                    f"this). Nothing is written."
                )
            target_segments = _split_row(target)
            if len(target_segments) != len(source_segments):
                raise ValueError(
                    f"{where}: the target has {len(target_segments)} ' | ' "
                    f"segment(s) but the source has {len(source_segments)}; the "
                    f"translation did not preserve the row's cell structure. "
                    f"Nothing is written."
                )
            for cell, text in zip(mapped.cells, target_segments):
                if cell.translatable:
                    writes.append(
                        _PlannedWrite(sheet=sheet_name, ref=cell.ref, text=text)
                    )
    return writes, untranslated


class XLIFF2XLSXConverter(BaseConverter):
    """XLIFF to XLSX backfill converter.

    Reads OPP's XLSX skeleton (the original workbook plus its
    ``xliff_map.json`` sidecar) and writes each translation into the cells the
    sidecar marks translatable, leaving every other cell exactly as the
    skeleton holds it.

    Translation contract:
        Each ``<trans-unit>`` in the XLIFF carries one spreadsheet row's text,
        one ``" | "``-joined segment per cell in column order. The trans-unit id
        is looked up in the sidecar's ``id`` field (ids have gaps), and segment
        ``i`` is written into ``cells[i]["ref"]`` when that cell is
        ``translatable``.
    """

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
    ) -> None:
        """Initialize the XLIFF to XLSX converter.

        Args:
            manifest: OPP manifest.json metadata
            frontmatter: OL YAML frontmatter metadata
        """
        super().__init__(manifest, frontmatter)

    @property
    def supported_format(self) -> str:
        """Supported output format."""
        return "XLSX"

    def validate_input(self, input_path: Path | str) -> bool:
        """Validate that the skeleton file exists and has a usable extension.

        Accepts a bare ``.xlsx`` and a ``.zip`` skeleton. A zip is accepted on
        its extension because the ``--format`` extension check plus
        ``FormatDetector.detect_from_skeleton`` have already established that
        the archive really is an XLSX skeleton; whether it carries the
        ``xliff_map.json`` sidecar is decided in :meth:`convert`, where the
        message can name the sidecar.

        Args:
            input_path: Path to the XLSX workbook or its skeleton zip

        Returns:
            True if the input exists and ends in .xlsx or .zip
        """
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() in (
            ".xlsx",
            ".zip",
        )

    def convert(  # type: ignore[override]
        self,
        input_path: Path | str,
        xliff_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        """Write the XLIFF translations into the XLSX skeleton's cells.

        Args:
            input_path: Path to OPP's ``<stem>.skeleton.zip``
            xliff_path: Path to the translated XLIFF file
            output_path: Path for the output XLSX file
            options: Converter options (unused: the encoding comes from the
                XLIFF's own XML declaration)

        Returns:
            ConversionResult. A map/XLIFF disagreement is reported as an error
            and no workbook is written.
        """
        skeleton = Path(input_path)
        xliff = Path(xliff_path)
        output = Path(output_path)

        def failure(code: str, message: str) -> ConversionResult:
            return ConversionResult(
                output_path=output,
                success=False,
                errors=[ErrorDetail(code=code, message=message)],
            )

        try:
            workbook_bytes, sheet_map = _read_xlsx_skeleton(skeleton)
        except Exception as e:
            logger.warning(
                "Failed to read XLSX skeleton %s: %s", skeleton, e, exc_info=True
            )
            return failure(
                "XLSX_SKELETON_ERROR", f"Failed to read XLSX skeleton: {e}"
            )

        if not xliff.exists():
            return failure(
                "XLIFF_NOT_FOUND", f"XLIFF file not found: {xliff}"
            )
        try:
            units = _parse_xliff_rows(xliff)
        except Exception as e:
            logger.warning(
                "Failed to parse XLIFF %s: %s", xliff, e, exc_info=True
            )
            return failure(
                "XLIFF_PARSE_ERROR", f"Failed to parse XLIFF: {e}"
            )

        try:
            workbook = _load_workbook(workbook_bytes)
        except ImportError as e:
            return failure("MISSING_DEPENDENCY", str(e))
        except Exception as e:
            logger.warning(
                "Failed to open the skeleton workbook %s: %s", skeleton, e,
                exc_info=True,
            )
            return failure(
                "XLSX_WORKBOOK_ERROR",
                f"Failed to open the skeleton workbook: {e}",
            )

        try:
            writes, untranslated = _plan_writes(
                sheet_map, units, list(workbook.sheetnames)
            )
        except ValueError as e:
            logger.warning("XLSX backfill plan rejected: %s", e)
            return failure(
                "XLSX_MAP_MISMATCH",
                f"Cannot map the translation onto the skeleton: {e}",
            )

        for planned in writes:
            workbook[planned.sheet][planned.ref].value = planned.text

        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            workbook.save(output)
        except Exception as e:
            logger.warning("Failed to write output XLSX: %s", e, exc_info=True)
            return failure(
                "XLSX_WRITE_ERROR", f"Failed to write output XLSX: {e}"
            )

        logger.info(
            "XLSX backfill wrote %d cell(s) across %d sheet(s) "
            "(%d mapped row(s) untranslated)",
            len(writes),
            len({planned.sheet for planned in writes}),
            untranslated,
        )
        warnings: list[WarningDetail] = []
        if untranslated:
            warnings.append(WarningDetail(
                code="ROWS_UNTRANSLATED",
                message=(
                    f"{untranslated} mapped row(s) have no <target> in the XLIFF; "
                    f"their cells keep the original text"
                ),
            ))
        return ConversionResult(
            output_path=output,
            success=True,
            warnings=warnings,
            metadata={
                "source_format": "XLIFF",
                "target_format": "XLSX",
                "skeleton": str(skeleton),
                "xliff": str(xliff),
                "cells_written": len(writes),
                "rows_untranslated": untranslated,
            },
        )


def _load_workbook(workbook_bytes: bytes) -> Any:
    """Open the skeleton's workbook for cell-value rewriting.

    ``data_only=False`` is required, not incidental: with ``data_only=True``
    openpyxl loads each formula cell's *cached* value and writes that value
    back on save, which would silently turn every formula in the workbook into
    a literal number the moment a single cell is translated.
    """
    try:
        import openpyxl
    except ImportError as e:
        raise ImportError(
            "XLSX backfill needs openpyxl: pip install "
            "'omni-re-formatter[office]'"
        ) from e
    return openpyxl.load_workbook(
        io.BytesIO(workbook_bytes), data_only=False
    )