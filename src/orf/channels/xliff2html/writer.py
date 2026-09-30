"""HTML writing functions for the XLIFF2HTML converter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from lxml import etree, html as lxml_html

from orf.converters.options import ConverterOptions
from orf.error_handlers.conversion_error import InlineFormattingError
from orf.logging import get_logger

logger = get_logger("channel.xliff2html.writer")


def apply_translations_and_formatting(
    html_content: str,
    translations: dict[str, str],
    xliff_content: str,
    inline_parser: Any,
    html_applier: Any,
    inline_translation_sentinel: str,
    preserved_child_tags: frozenset,
    strip_xliff_inline_tags_fn: Any,
    options: ConverterOptions | None = None,
) -> str:
    """Apply translations (and optionally inline formatting) via lxml DOM.

    The HTML template is parsed with the lxml HTML parser. For each
    translation unit, the target node is located by its
    ``data-trans-unit-id="<unit_id>"`` attribute and its text content is
    replaced with the translated text. When ``preserve_inline`` is true,
    the inline-formatting pipeline is applied to the *translated text*
    before injection so that ``<strong>`` / ``<em>`` / ``<u>`` / ``<s>``
    wraps the correct content.

    Args:
        html_content: The original HTML template content
        translations: Dictionary of translation units
        xliff_content: The full XLIFF content for inline tag processing
        inline_parser: XLIFFInlineParser instance
        html_applier: EPUBHTMLInlineApplier instance
        inline_translation_sentinel: Sentinel marker for translation insertion
        preserved_child_tags: Set of child tag names to preserve
        strip_xliff_inline_tags_fn: Function to strip XLIFF inline tags
        options: Converter options

    Returns:
        HTML content with translations and formatting applied

    Raises:
        InlineFormattingError: If inline formatting cannot be applied
    """
    opts = options or ConverterOptions()
    preserve_inline = opts.preserve_inline

    try:
        root = lxml_html.fromstring(html_content)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as e:
        raise InlineFormattingError(
            tag="html_template", context_str=f"Failed to parse HTML template: {e}"
        )

    # Each payload is either a plain string (set as node.text) or a list
    # of lxml elements parsed from inline-formatted HTML (appended as
    # children). Inline-formatted payloads are needed because lxml's
    # ``.text`` setter HTML-escapes its argument — so a formatted string
    # like ``"<strong>翻译</strong>"`` would render as escaped text.
    if preserve_inline:
        translations_to_inject: dict[str, Any] = {}
        for unit_id, translated_text in translations.items():
            inline_wrapped = wrap_translation_with_xliff_inline(
                xliff_content, unit_id, inline_translation_sentinel
            )
            if inline_wrapped is not None:
                formatted = html_applier.convert_xliff_to_html(inline_wrapped)
                formatted = formatted.replace(
                    inline_translation_sentinel, translated_text
                )
                translations_to_inject[unit_id] = parse_html_fragment(formatted)
            else:
                translations_to_inject[unit_id] = translated_text
    else:
        translations_to_inject = dict(translations)

    inject_translations_into_dom(root, translations_to_inject, preserved_child_tags)

    # If no data-trans-unit-id nodes matched the translations, fall back
    # to text-matching on DOM text nodes (supports HTML that lacks the
    # attribute — e.g. smoke-test input, hand-crafted HTML templates).
    matched_count = sum(
        1 for node in root.xpath("//*[@data-trans-unit-id]")
        if node.get("data-trans-unit-id") in translations
    )
    if matched_count == 0 and translations:
        backfill_by_text_match(root, translations, xliff_content, strip_xliff_inline_tags_fn)

    # Issue A: positional table-cell corrections run last so they only ever
    # apply what the attribute/text-match paths cannot (repeated cell text).
    backfill_table_cells(root, translations, xliff_content)

    result = lxml_html.tostring(root, encoding="unicode", method="html")
    # lxml's stub marks the tostring return as ``str | bytes``; with
    # encoding="unicode" it is always str at runtime.
    assert isinstance(result, str)
    return result


def parse_html_fragment(fragment: str) -> list[Any]:
    """Parse an HTML fragment string into a list of lxml child elements.

    Used to turn the inline-formatter's HTML output (e.g.
    ``"<strong>粗体</strong>"``) into a list of elements that the DOM
    injector can append to a target node without HTML-escaping.
    """
    wrapper = lxml_html.fragment_fromstring(fragment, create_parent=True)
    return list(wrapper)


def wrap_translation_with_xliff_inline(
    xliff_content: str,
    unit_id: str,
    inline_translation_sentinel: str,
) -> Optional[str]:
    """If the unit's target has inline ``<bx>``/``<ex>`` tags, return a
    synthetic ``<trans-unit>`` fragment whose source text is replaced
    with the ``_INLINE_TRANSLATION_SENTINEL`` placeholder, so the
    existing XLIFF→HTML pipeline produces e.g. ``<strong>翻译</strong>``.

    Returns ``None`` when the unit is missing from ``xliff_content`` OR
    has no inline tags (the caller uses the plain translated text).
    """
    unit_pattern = re.compile(
        rf'<trans-unit[^>]+id="{re.escape(unit_id)}"[^>]*>(.*?)</trans-unit>',
        re.DOTALL | re.IGNORECASE,
    )
    match = unit_pattern.search(xliff_content)
    if not match:
        return None

    unit_body = match.group(1)
    target_match = re.search(
        r"<target[^>]*>(.*?)</target>", unit_body, re.DOTALL | re.IGNORECASE
    )
    source_match = re.search(
        r"<source[^>]*>(.*?)</source>", unit_body, re.DOTALL | re.IGNORECASE
    )
    body_match = target_match or source_match
    if not body_match:
        return None

    body_text = body_match.group(1)
    if not re.search(r"<(bx|ex)\b", body_text, re.IGNORECASE):
        return None

    # Inline tags present: split into [text, tag, text, tag, ...] and
    # swap each text segment for the sentinel, preserving the
    # tag positions so the applier produces correctly-wrapped HTML.
    segments = re.split(r"(<[^>]+>)", body_text)
    for i, seg in enumerate(segments):
        if seg and not seg.startswith("<"):
            segments[i] = inline_translation_sentinel
    new_body = "".join(segments)

    return (
        f'<trans-unit id="{unit_id}">'
        f"<source>{new_body}</source>"
        f"<target>{new_body}</target>"
        f"</trans-unit>"
    )


def inject_translations_into_dom(
    root: Any,
    translations: dict[str, Any],
    preserved_child_tags: frozenset,
) -> None:
    """Replace the text/children of every ``data-trans-unit-id`` node.

    Each value in ``translations`` is either a plain ``str`` (set as the
    node's ``.text``, preserving all child elements) or a list of lxml
    elements parsed from inline-formatted HTML (appended as children
    of the target node after removing disposable formatting children).
    Structural child elements (<img>, <a>, <br>, etc.) defined in
    ``_PRESERVED_CHILD_TAGS`` are always preserved. Sibling elements and
    attributes are preserved verbatim. Any literal ``[unit_id]`` substring
    in the template is left untouched — it is no longer part of the
    contract.
    """
    if not translations:
        return

    for node in root.xpath("//*[@data-trans-unit-id]"):
        unit_id = node.get("data-trans-unit-id")
        if unit_id is None or unit_id not in translations:
            continue
        payload = translations[unit_id]
        if isinstance(payload, str):
            node.text = payload
        else:
            for child in list(node):
                if child.tag not in preserved_child_tags:
                    node.remove(child)
            for element in payload:
                node.append(element)


def backfill_by_text_match(
    root: Any,
    translations: dict[str, str],
    xliff_content: str,
    strip_xliff_inline_tags_fn: Any,
) -> int:
    """Fallback: match XLIFF source text against DOM text nodes and replace.

    Used when the HTML template lacks ``data-trans-unit-id`` attributes
    (hand-crafted HTML, smoke-test fixtures).  The primary attribute-based
    path runs first; this method only triggers when zero nodes matched.

    The method runs two passes:

    1. **Per-node pass** — checks each ``element.text`` and
       ``child.tail`` individually against the XLIFF source strings.
    2. **text_content() fallback** — for elements with nested inline
       markup (e.g. ``<li><strong>foo</strong> — bar</li>``), the
       per-node pass may miss fragments because OPP flattened the text
       into per-fragment trans-units while the per-node loop only
       checked the first-level text/tail.  The fallback concatenates
       all descendant text via ``text_content()`` and, when the full
       string matches a source key, walks the element's text/tail
       fragments again with ``dict.get()`` to pick up any that the
       first pass missed.

    Returns the number of text nodes that were replaced.
    """
    trans_unit_pattern = re.compile(
        r'<trans-unit[^>]+id="([^"]+)"[^>]*>(.*?)</trans-unit>',
        re.DOTALL | re.IGNORECASE,
    )
    source_pattern = re.compile(
        r"<source[^>]*>(.*?)</source>", re.DOTALL | re.IGNORECASE
    )

    source_to_target: dict[str, str] = {}
    for match in trans_unit_pattern.finditer(xliff_content):
        unit_id = match.group(1)
        if unit_id not in translations:
            continue
        unit_content = match.group(2)
        src_match = source_pattern.search(unit_content)
        if src_match:
            src_text = strip_xliff_inline_tags_fn(src_match.group(1))
            source_to_target[src_text] = translations[unit_id]
            # Also store stripped key so .strip()ed DOM text matches
            stripped = src_text.strip()
            if stripped != src_text:
                source_to_target[stripped] = translations[unit_id]

    if not source_to_target:
        return 0

    replaced = 0
    for element in root.iter():
        if element.text and element.text.strip() in source_to_target:
            element.text = source_to_target[element.text.strip()]
            replaced += 1
        for child in element:
            if child.tail and child.tail.strip() in source_to_target:
                child.tail = source_to_target[child.tail.strip()]
                replaced += 1

    # Pass 2: text_content() fallback for nested markup
    # (e.g. <li><strong>foo</strong> — bar</li>)
    for element in root.iter():
        if element.text and element.text.strip() in source_to_target:
            continue
        if any(
            child.tail and child.tail.strip() in source_to_target
            for child in element
        ):
            continue

        # HtmlElement has text_content(); plain etree elements don't
        try:
            full_text = element.text_content().strip()
        except AttributeError:
            full_text = "".join(element.itertext()).strip()

        if not full_text or full_text not in source_to_target:
            continue

        logger.debug(
            "text_content() fallback: <%s> full_text=%r",
            element.tag,
            full_text[:80],
        )
        if element.text and element.text.strip():
            fragment = element.text.strip()
            if fragment in source_to_target:
                element.text = source_to_target[fragment]
                replaced += 1
        for child in element:
            if child.tail and child.tail.strip():
                fragment = child.tail.strip()
                if fragment in source_to_target:
                    child.tail = source_to_target[fragment]
                    replaced += 1

    if replaced:
        logger.debug(
            "Text-matched %d translation(s) (no data-trans-unit-id attributes found)",
            replaced,
        )
    return replaced


# Issue A: OPP emits ``table_{t}_r{r}_c{c}`` resnames.  OPP#80 Wave 0B adds an
# optional ``_para{p}`` suffix (0-based cell paragraph index); the group is
# OPTIONAL so a bare resname still matches with group(4) == None.  Deliberately
# duplicated in the sibling channels (xliff2docx/parser.py:31,
# xliff2pptx.py:55); keep in sync.
_TABLE_RESNAME_RE = re.compile(r"^table_(\d+)_r(\d+)_c(\d+)(?:_para(\d+))?$")
_TABLE_TRANS_UNIT_RE = re.compile(
    r"<trans-unit\b([^>]*)>(.*?)</trans-unit>", re.DOTALL | re.IGNORECASE
)
_ATTRIBUTE_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')

# OPP#80 Wave 1 — HTML block model.  A cell's direct children form an ordered
# sequence of "paragraph groups": a direct-child ELEMENT whose tag is one of
# these is its own group (case a); a maximal run of consecutive non-block
# direct children (text nodes, <br>, <b>, <span>, <img>, ...) is one inline
# group (case b).  ``_para{p}`` is the 0-based index into that group list.
# OPP mirrors this exact enumeration -- leading ``.text``, each element
# followed by its ``.tail`` -- INCLUDING whitespace-only text groups, so the
# two sides never disagree on an index.  Deliberately local to this channel;
# the sibling channels own their own model (xliff2docx / xliff2pptx).
_HTML_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "li", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "table", "figure", "figcaption", "address", "dt", "dd", "hr",
})


@dataclass(slots=True)
class _CellChild:
    """One direct child node of a table cell, in document order.

    A text node is represented by the owning element plus the attribute that
    holds it (``"text"`` for the cell's leading text, ``"tail"`` for text
    after a child element).  An element node carries ``attr=""`` and keeps
    the element itself.  Comments / processing instructions (non-``str``
    tags) are element nodes too; they are never block tags, so they join the
    surrounding inline run.
    """

    element: Any
    attr: str

    @property
    def is_element(self) -> bool:
        return self.attr == ""

    @property
    def is_block(self) -> bool:
        return (
            self.is_element
            and isinstance(self.element.tag, str)
            and self.element.tag.lower() in _HTML_BLOCK_TAGS
        )


@dataclass(slots=True)
class _TableUnit:
    """One positional table unit parsed from the raw XLIFF."""

    table: int
    row: int
    col: int
    para: Optional[int]
    unit_id: str
    translation: str


@dataclass(slots=True)
class _CellPlan:
    """Resolved write plan for one cell (see ``plan_table_cell_writes``)."""

    key: tuple[int, int, int]
    mode: str  # "bare" | "para"
    units: list[_TableUnit]  # units to write, in XLIFF document order
    skipped: list[_TableUnit]  # bare + duplicate-para units dropped from a para cell


def plan_table_cell_writes(units: list[_TableUnit]) -> list[_CellPlan]:
    """Group table units per cell and resolve each cell's write mode.

    Contract (CONTRACT.md §1.1, "Table Cell Coordinates"):

    * A cell carrying ANY ``_para{p}`` unit is written **per-paragraph**.  A
      bare unit targeting the same cell MUST be skipped with a warning and
      MUST NOT fall back to whole-cell writing -- a whole-cell write would
      clear the per-paragraph writes already applied.
    * Duplicate ``(t, r, c, para)`` keeps the FIRST; later duplicates are
      dropped into ``skipped`` so the caller can warn.
    * A cell with only bare units stays **legacy**: every unit is kept in
      XLIFF order and the caller writes the whole cell once per unit (last
      one wins), byte-for-byte the pre-OPP#80 behaviour.

    This helper is pure -- no DOM access, no logging.  Out-of-range
    paragraphs are detected by the caller against the rendered cell's own
    group list, because only the DOM knows how many groups exist.

    Returns:
        One plan per cell, in first-seen cell order; each plan's ``units``
        preserve XLIFF document order.
    """
    grouped: dict[tuple[int, int, int], list[_TableUnit]] = {}
    order: list[tuple[int, int, int]] = []
    for unit in units:
        key = (unit.table, unit.row, unit.col)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(unit)

    plans: list[_CellPlan] = []
    for key in order:
        cell_units = grouped[key]
        if not any(unit.para is not None for unit in cell_units):
            plans.append(_CellPlan(key, "bare", list(cell_units), []))
            continue
        write_units: list[_TableUnit] = []
        skipped: list[_TableUnit] = []
        seen_paras: set[int] = set()
        for unit in cell_units:
            if unit.para is None:
                skipped.append(unit)
                continue
            if unit.para in seen_paras:
                skipped.append(unit)
                continue
            seen_paras.add(unit.para)
            write_units.append(unit)
        plans.append(_CellPlan(key, "para", write_units, skipped))
    return plans


def _cell_paragraph_groups(cell: Any) -> list[list[_CellChild]]:
    """Enumerate a cell's paragraph groups in document order (see model above).

    The child sequence interleaves text with elements: the cell's leading
    ``.text`` (when present), then every direct child element followed by its
    ``.tail`` (when present).  Whitespace-only text nodes are kept, so the
    index of every following group matches OPP's enumeration exactly.
    """
    nodes: list[_CellChild] = []
    if cell.text is not None:
        nodes.append(_CellChild(cell, "text"))
    for child in cell:
        nodes.append(_CellChild(child, ""))
        if child.tail is not None:
            nodes.append(_CellChild(child, "tail"))

    groups: list[list[_CellChild]] = []
    inline_run: list[_CellChild] = []
    for node in nodes:
        if node.is_block:
            if inline_run:
                groups.append(inline_run)
                inline_run = []
            groups.append([node])
        else:
            inline_run.append(node)
    if inline_run:
        groups.append(inline_run)
    return groups


def _node_text(node: _CellChild) -> str:
    """Return the rendered text a direct child node currently carries."""
    if node.is_element:
        return "".join(node.element.itertext())
    return getattr(node.element, node.attr) or ""


def _write_inline_run(run: list[_CellChild], translation: str) -> None:
    """Replace an inline run's text, leaving every node outside it untouched.

    The run's first text-bearing node receives the translation; the remaining
    nodes of the run are removed (elements) or blanked (text).  Inline markup
    inside the run is intentionally sacrificed -- a documented v1 limitation
    of OPP#80; ``<br>`` preservation is explicitly out of scope.
    """
    target = next((node for node in run if _node_text(node).strip()), run[0])
    for node in run:
        if node is target:
            continue
        if node.is_element:
            parent = node.element.getparent()
            if parent is not None:
                parent.remove(node.element)
        else:
            setattr(node.element, node.attr, None)
    if target.is_element:
        for child in list(target.element):
            target.element.remove(child)
        target.element.text = translation
    else:
        setattr(target.element, target.attr, translation)


def _write_paragraph_group(cell: Any, para: int, translation: str) -> bool:
    """Write ``translation`` into paragraph group ``para`` of ``cell``.

    A block-element group replaces ONLY that element's own content (its
    siblings stay byte-identical).  An inline group replaces only the run's
    text.  An out-of-range ``para`` warns and returns ``False`` -- it never
    guesses a paragraph and never raises.
    """
    groups = _cell_paragraph_groups(cell)
    if not (0 <= para < len(groups)):
        logger.warning(
            "table cell para index %d out of range (cell has %d paragraph "
            "group(s)); skipped",
            para,
            len(groups),
        )
        return False
    group = groups[para]
    if len(group) == 1 and group[0].is_block:
        element = group[0].element
        for child in list(element):
            element.remove(child)
        element.text = translation
        return True
    _write_inline_run(group, translation)
    return True


def _resolve_table_cell(
    tables: list[Any], table_index: int, row_index: int, col_index: int
) -> Any | None:
    """Resolve ``table_{t}_r{r}_c{c}`` with OPP's raw direct-child walk.

    * ``root.iter("table")`` → ``t``-th ``<table>``
    * its direct ``<tr>`` children → ``r``-th
    * its direct ``<td>``/``<th>`` children → ``c``-th

    Any index out of range logs at debug and returns ``None`` (never raises).
    """
    if not (0 <= table_index < len(tables)):
        logger.debug("table_%d: table out of range", table_index)
        return None
    rows = [
        child for child in tables[table_index]
        if isinstance(child.tag, str) and child.tag.lower() == "tr"
    ]
    if not (0 <= row_index < len(rows)):
        logger.debug("table_%d: row %d out of range", table_index, row_index)
        return None
    cells = [
        child for child in rows[row_index]
        if isinstance(child.tag, str) and child.tag.lower() in ("td", "th")
    ]
    if not (0 <= col_index < len(cells)):
        logger.debug(
            "table_%d_r%d: cell %d out of range", table_index, row_index, col_index
        )
        return None
    return cells[col_index]


def backfill_table_cells(
    root: Any,
    translations: dict[str, str],
    xliff_content: str,
) -> int:
    """Issue A + OPP#80: positional table-cell backfill (bare and per-paragraph).

    Scans the raw XLIFF for trans-units whose ``resname`` matches
    ``table_{t}_r{r}_c{c}`` (whole cell) or ``table_{t}_r{r}_c{c}_para{p}``
    (one paragraph group of the cell; attribute order agnostic) and resolves
    the target cell with the same raw-node walk OPP uses:

    * ``root.iter("table")`` → ``t``-th ``<table>``
    * its direct ``<tr>`` children → ``r``-th
    * its direct ``<td>``/``<th>`` children → ``c``-th

    A BARE unit keeps the legacy whole-cell write (remove every child, set the
    stripped translation as the cell's sole text node) -- unchanged output.
    A ``_para{p}`` unit writes ONLY paragraph group ``p`` of the cell, leaving
    sibling block elements byte-identical (see ``_cell_paragraph_groups`` for
    the block model and ``plan_table_cell_writes`` for the mixed/duplicate
    contract).  This runs after the primary ``data-trans-unit-id`` injection
    and ``backfill_by_text_match`` so it only corrects what those paths cannot
    (a cell text repeated across cells).  It is a no-op when the XLIFF has no
    ``table_...`` resname and never raises (missing table/row/cell or
    out-of-range paragraph → warn/skip).

    Returns:
        The number of cells (bare) / paragraph groups (per-paragraph) written.
    """
    from orf.channels.xliff2html.parser import strip_xliff_inline_tags

    tables = list(root.iter("table"))
    units: list[_TableUnit] = []
    for tu_match in _TABLE_TRANS_UNIT_RE.finditer(xliff_content):
        attrs = dict(_ATTRIBUTE_RE.findall(tu_match.group(1)))
        resname = attrs.get("resname")
        if not resname:
            continue
        coord_match = _TABLE_RESNAME_RE.match(resname)
        if not coord_match:
            continue
        unit_id = attrs.get("id")
        if unit_id is None or unit_id not in translations:
            continue
        para_str = coord_match.group(4)
        units.append(
            _TableUnit(
                table=int(coord_match.group(1)),
                row=int(coord_match.group(2)),
                col=int(coord_match.group(3)),
                para=int(para_str) if para_str is not None else None,
                unit_id=unit_id,
                translation=translations[unit_id],
            )
        )

    written = 0
    for plan in plan_table_cell_writes(units):
        table_index, row_index, col_index = plan.key
        try:
            cell = _resolve_table_cell(tables, table_index, row_index, col_index)
            if cell is None:
                continue

            if plan.mode == "bare":
                # LEGACY path, byte-for-byte: remove every child, flat text.
                # Kept separate from the paragraph-scoped path on purpose.
                for unit in plan.units:
                    for child in list(cell):
                        cell.remove(child)
                    cell.text = strip_xliff_inline_tags(unit.translation)
                    written += 1
                continue

            # PER-PARAGRAPH path (CONTRACT.md §1.1).
            for unit in plan.skipped:
                if unit.para is None:
                    logger.warning(
                        "table_%d_r%d_c%d: bare unit %s skipped -- cell is "
                        "per-paragraph; a whole-cell write would clear sibling "
                        "paragraphs",
                        table_index, row_index, col_index, unit.unit_id,
                    )
                else:
                    logger.warning(
                        "table_%d_r%d_c%d_para%d: duplicate paragraph unit %s "
                        "skipped (first wins)",
                        table_index, row_index, col_index, unit.para, unit.unit_id,
                    )
            for unit in plan.units:
                assert unit.para is not None
                if _write_paragraph_group(
                    cell, unit.para, strip_xliff_inline_tags(unit.translation)
                ):
                    written += 1
        except Exception as e:  # pragma: no cover - defensive, never raise
            logger.debug(
                "table_%d_r%d_c%d: backfill skipped: %s",
                table_index, row_index, col_index, e,
            )

    if written:
        logger.debug("Positionally backfilled %d table cell(s)", written)
    return written
