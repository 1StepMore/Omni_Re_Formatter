"""HTML writing functions for the XLIFF2HTML converter."""

from __future__ import annotations

import re
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


_TABLE_RESNAME_RE = re.compile(r"^table_(\d+)_r(\d+)_c(\d+)$")
_TABLE_TRANS_UNIT_RE = re.compile(
    r"<trans-unit\b([^>]*)>(.*?)</trans-unit>", re.DOTALL | re.IGNORECASE
)
_ATTRIBUTE_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def backfill_table_cells(
    root: Any,
    translations: dict[str, str],
    xliff_content: str,
) -> int:
    """Issue A: write ``table_{t}_r{r}_c{c}`` translations positionally.

    Scans the raw XLIFF for trans-units whose ``resname`` matches
    ``table_{t}_r{r}_c{c}`` (attribute order agnostic) and resolves the
    target cell with the same raw-node walk OPP uses:

    * ``root.iter("table")`` → ``t``-th ``<table>``
    * its direct ``<tr>`` children → ``r``-th
    * its direct ``<td>``/``<th>`` children → ``c``-th

    The unit's translation (already produced by ``parse_xliff``) is stripped
    of XLIFF inline tags and set as the cell's sole text node.  This runs
    after the primary ``data-trans-unit-id`` injection and
    ``backfill_by_text_match`` so it only corrects what those paths cannot
    (a cell text repeated across cells).  It is a no-op when the XLIFF has no
    ``table_...`` resname and never raises (missing table/row/cell → skip).

    Returns:
        The number of cells written.
    """
    from orf.channels.xliff2html.parser import strip_xliff_inline_tags

    written = 0
    tables = list(root.iter("table"))

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

        table_index, row_index, col_index = (
            int(coord_match.group(1)),
            int(coord_match.group(2)),
            int(coord_match.group(3)),
        )
        try:
            if not (0 <= table_index < len(tables)):
                logger.debug("table_%s: table out of range", resname)
                continue
            rows = [
                child for child in tables[table_index]
                if isinstance(child.tag, str) and child.tag.lower() == "tr"
            ]
            if not (0 <= row_index < len(rows)):
                logger.debug("table_%s: row out of range", resname)
                continue
            cells = [
                child for child in rows[row_index]
                if isinstance(child.tag, str) and child.tag.lower() in ("td", "th")
            ]
            if not (0 <= col_index < len(cells)):
                logger.debug("table_%s: cell out of range", resname)
                continue
            cell = cells[col_index]
            for child in list(cell):
                cell.remove(child)
            cell.text = strip_xliff_inline_tags(translations[unit_id])
            written += 1
        except Exception as e:  # pragma: no cover - defensive, never raise
            logger.debug("table_%s: backfill skipped: %s", resname, e)

    if written:
        logger.debug("Positionally backfilled %d table cell(s)", written)
    return written
