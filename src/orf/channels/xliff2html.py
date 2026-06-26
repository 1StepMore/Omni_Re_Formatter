"""XLIFF to HTML conversion channel with inline formatting preservation."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Optional, Any

from bs4 import BeautifulSoup
from lxml import etree, html as lxml_html

from orf.converters.base import BaseConverter, ConversionResult
from orf.mcp.schemas import ImagePlacement
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.skeleton.inline_formatting import XLIFFInlineParser, EPUBHTMLInlineApplier
from orf.error_handlers.conversion_error import XLIFFParseError, InlineFormattingError
from orf.logging import get_logger
from orf.converters.options import ConverterOptions

logger = get_logger("channel.xliff2html")

# Sentinel substituted into the synthetic XLIFF fragment so the translated
# text can be inserted at the right position after the inline-tag applier
# runs. NULL bytes guarantee no real translation will collide.
_INLINE_TRANSLATION_SENTINEL = "\x00TRANSLATED_TEXT\x00"

# Tags that are always preserved when injecting translations into the DOM.
# These are structural elements that carry content independent of the text
# being translated and should never be removed during backfill.
_PRESERVED_CHILD_TAGS = frozenset({
    "img", "br", "hr", "input", "video", "audio", "source", "a",
})


class XLIFF2HTMLConverter(BaseConverter):
    """XLIFF to HTML backfill converter with inline formatting preservation.

    Converts XLIFF translation files back to HTML by:
    1. Loading the original HTML template
    2. Parsing the XLIFF file for translations
    3. Locating the target node via the ``data-trans-unit-id`` attribute and
       injecting the translated text via lxml DOM manipulation
    4. Optionally applying inline formatting (bold, italic, underline, strike)
       to the translated text
    5. Saving the result as HTML

    Translation contract:
        Translators mark translatable nodes with ``data-trans-unit-id="<id>"``
        matching the ``id`` attribute of the corresponding ``<trans-unit>`` in
        the XLIFF file. The converter finds each marked node and replaces its
        text content with the translated target, preserving the tag and any
        child elements. Literal ``[<unit_id>]`` substring placeholders are no
        longer required (and are silently ignored if present).

    Inline formatting mapping:
        - bold -> <strong>
        - italic -> <em>
        - underline -> <u>
        - strike -> <s>
    """

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
    ):
        """Initialize the XLIFF to HTML converter.

        Args:
            manifest: OPP manifest.json metadata
            frontmatter: OL YAML frontmatter metadata
        """
        super().__init__(manifest, frontmatter)
        self.inline_parser = XLIFFInlineParser()
        self.html_applier = EPUBHTMLInlineApplier()

    @property
    def supported_format(self) -> str:
        """Supported output format."""
        return "HTML"

    def validate_input(self, input_path: Path | str) -> bool:
        """Validate that the HTML template file exists and is readable.

        Args:
            input_path: Path to the HTML template file

        Returns:
            True if the input file exists and has .html/.htm extension
        """
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() in (".html", ".htm")

    def convert(  # type: ignore[override]
        self,
        input_path: Path | str,
        xliff_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        """Convert XLIFF translation to HTML with inline formatting preserved.

        Args:
            input_path: Path to the original HTML template file
            xliff_path: Path to the XLIFF translation file
            output_path: Path for the output HTML file
            options: Converter options (preserve_inline, etc.)

        Returns:
            ConversionResult with success status and any warnings/errors
        """
        html_template = Path(input_path)
        opts = options or ConverterOptions()
        xliff_path = Path(xliff_path)
        output_path = Path(output_path)

        # 1. Validate and load HTML template
        if not html_template.exists():
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"HTML template not found: {html_template}"],
            )

        try:
            html_content = html_template.read_text(
                encoding=opts.encoding
            )
        except Exception as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to read HTML template: {e}"],
            )

        # 2. Validate and parse XLIFF
        if not xliff_path.exists():
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"XLIFF file not found: {xliff_path}"],
            )

        try:
            xliff_content = xliff_path.read_text(
                encoding=opts.encoding
            )
        except Exception as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to read XLIFF file: {e}"],
            )

        # 3. Parse XLIFF and extract translations
        try:
            translations = self._parse_xliff(xliff_content)
        except XLIFFParseError as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[str(e)],
            )

        # 4. Apply translations and formatting to HTML
        result_content = html_content  # Default to original if processing fails
        warnings_list: list[str] = []
        try:
            result_content = self._apply_translations_and_formatting(
                html_content, translations, xliff_content, options
            )
        except InlineFormattingError as e:
            warnings_list.append(str(e))
            # Continue with original content if formatting fails

        # 5. Save output HTML
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(result_content, encoding="utf-8")
        except Exception as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to write output HTML: {e}"],
            )

        return ConversionResult(
            output_path=output_path,
            success=True,
            warnings=warnings_list,
            metadata={
                "source_format": "XLIFF",
                "target_format": "HTML",
                "template": str(html_template),
                "xliff": str(xliff_path),
            },
        )

    def _parse_xliff(self, xliff_content: str) -> dict[str, str]:
        """Parse XLIFF content and extract translation units.

        Args:
            xliff_content: The XLIFF file content as string

        Returns:
            Dictionary mapping segment IDs to translated text

        Raises:
            XLIFFParseError: If XLIFF content is malformed
        """
        translations = {}

        # XLIFF trans-unit pattern: <trans-unit id="...">
        # with <source>...</source> and <target>...</target>
        trans_unit_pattern = re.compile(
            r'<trans-unit[^>]+id="([^"]+)"[^>]*>(.*?)</trans-unit>',
            re.DOTALL | re.IGNORECASE
        )

        source_pattern = re.compile(r'<source[^>]*>(.*?)</source>', re.DOTALL | re.IGNORECASE)
        target_pattern = re.compile(r'<target[^>]*>(.*?)</target>', re.DOTALL | re.IGNORECASE)

        for match in trans_unit_pattern.finditer(xliff_content):
            unit_id = match.group(1)
            unit_content = match.group(2)

            # Extract target (translated) text
            target_match = target_pattern.search(unit_content)
            if target_match:
                # Strip XLIFF inline tags for the translation
                target_text = self._strip_xliff_inline_tags(target_match.group(1))
                translations[unit_id] = target_text
            else:
                # Fall back to source if no target
                source_match = source_pattern.search(unit_content)
                if source_match:
                    source_text = self._strip_xliff_inline_tags(source_match.group(1))
                    translations[unit_id] = source_text

        return translations

    def _strip_xliff_inline_tags(self, text: str) -> str:
        """Remove XLIFF inline formatting tags from text.

        Args:
            text: Text containing <bx/> and <ex/> tags

        Returns:
            Plain text with inline tags removed
        """
        # Remove <bx .../> tags
        text = re.sub(r'<bx[^>]*/>', '', text, flags=re.IGNORECASE)
        # Remove <ex .../> tags
        text = re.sub(r'<ex[^>]*/>', '', text, flags=re.IGNORECASE)
        return text

    def _apply_translations_and_formatting(
        self,
        html_content: str,
        translations: dict[str, str],
        xliff_content: str,
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

        Literal ``[unit_id]`` placeholders in the template are no longer
        required; the DOM-driven path locates targets by attribute and
        replaces node text directly. Any literal brackets that happen to
        appear in the source are left untouched.

        Args:
            html_content: The original HTML template content
            translations: Dictionary of translation units
            xliff_content: The full XLIFF content for inline tag processing
            **options: Additional options like ``preserve_inline``

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
            raise InlineFormattingError(tag="html_template", context_str=f"Failed to parse HTML template: {e}")

        # Each payload is either a plain string (set as node.text) or a list
        # of lxml elements parsed from inline-formatted HTML (appended as
        # children). Inline-formatted payloads are needed because lxml's
        # ``.text`` setter HTML-escapes its argument — so a formatted string
        # like ``"<strong>翻译</strong>"`` would render as escaped text.
        if preserve_inline:
            translations_to_inject: dict[str, Any] = {}
            for unit_id, translated_text in translations.items():
                inline_wrapped = self._wrap_translation_with_xliff_inline(
                    xliff_content, unit_id
                )
                if inline_wrapped is not None:
                    formatted = self.html_applier.convert_xliff_to_html(
                        inline_wrapped
                    )
                    formatted = formatted.replace(
                        _INLINE_TRANSLATION_SENTINEL, translated_text
                    )
                    translations_to_inject[unit_id] = self._parse_html_fragment(
                        formatted
                    )
                else:
                    translations_to_inject[unit_id] = translated_text
        else:
            translations_to_inject = dict(translations)

        self._inject_translations_into_dom(root, translations_to_inject)

        # If no data-trans-unit-id nodes matched the translations, fall back
        # to text-matching on DOM text nodes (supports HTML that lacks the
        # attribute — e.g. smoke-test input, hand-crafted HTML templates).
        matched_count = sum(
            1 for node in root.xpath("//*[@data-trans-unit-id]")
            if node.get("data-trans-unit-id") in translations
        )
        if matched_count == 0 and translations:
            self._backfill_by_text_match(root, translations, xliff_content)

        result = lxml_html.tostring(root, encoding="unicode", method="html")
        # lxml's stub marks the tostring return as `str | bytes`; with
        # encoding="unicode" it is always str at runtime.
        assert isinstance(result, str)
        return result

    def _parse_html_fragment(self, fragment: str) -> list[Any]:
        """Parse an HTML fragment string into a list of lxml child elements.

        Used to turn the inline-formatter's HTML output (e.g.
        ``"<strong>粗体</strong>"``) into a list of elements that the DOM
        injector can append to a target node without HTML-escaping.
        """
        wrapper = lxml_html.fragment_fromstring(fragment, create_parent=True)
        return list(wrapper)

    def _wrap_translation_with_xliff_inline(
        self,
        xliff_content: str,
        unit_id: str,
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
            r'<target[^>]*>(.*?)</target>', unit_body, re.DOTALL | re.IGNORECASE
        )
        source_match = re.search(
            r'<source[^>]*>(.*?)</source>', unit_body, re.DOTALL | re.IGNORECASE
        )
        body_match = target_match or source_match
        if not body_match:
            return None

        body_text = body_match.group(1)
        if not re.search(r'<(bx|ex)\b', body_text, re.IGNORECASE):
            return None

        # Inline tags present: split into [text, tag, text, tag, ...] and
        # swap each text segment for the sentinel, preserving the
        # tag positions so the applier produces correctly-wrapped HTML.
        segments = re.split(r'(<[^>]+>)', body_text)
        for i, seg in enumerate(segments):
            if seg and not seg.startswith('<'):
                segments[i] = _INLINE_TRANSLATION_SENTINEL
        new_body = ''.join(segments)

        return (
            f'<trans-unit id="{unit_id}">'
            f'<source>{new_body}</source>'
            f'<target>{new_body}</target>'
            f'</trans-unit>'
        )

    def _inject_translations_into_dom(
        self,
        root: Any,
        translations: dict[str, Any],
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

        for node in root.xpath('//*[@data-trans-unit-id]'):
            unit_id = node.get("data-trans-unit-id")
            if unit_id is None or unit_id not in translations:
                continue
            payload = translations[unit_id]
            if isinstance(payload, str):
                node.text = payload
            else:
                for child in list(node):
                    if child.tag not in _PRESERVED_CHILD_TAGS:
                        node.remove(child)
                for element in payload:
                    node.append(element)

    def _backfill_by_text_match(
        self,
        root: Any,
        translations: dict[str, str],
        xliff_content: str,
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
                src_text = self._strip_xliff_inline_tags(src_match.group(1))
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

    def inject_images(
        self,
        html_path: Path | str,
        images: list[ImagePlacement],
        output_path: Path | str,
    ) -> tuple[list[ImagePlacement], list[ImagePlacement]]:
        """Inject images into HTML at specified DOM element positions.

        Args:
            html_path: Path to HTML file.
            images: List of ImagePlacement objects from OPP.
            output_path: Path to write the output HTML.

        Returns:
            Tuple of (injected_images, orphaned_images).
        """
        html_path = Path(html_path)
        output_path = Path(output_path)

        orphaned: list[ImagePlacement] = []
        injected: list[ImagePlacement] = []

        positioned = [img for img in images if img.element_index is not None]
        unpositioned = [img for img in images if img.element_index is None]

        if unpositioned:
            for img in unpositioned:
                logger.warning(
                    "Image has no element_index, appending to end: mime_type=%s",
                    img.mime_type,
                )
            orphaned.extend(unpositioned)

        if not positioned:
            if images and html_path.exists():
                html_path.read_text(encoding="utf-8")
            return (injected, orphaned)

        try:
            html_content = html_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Failed to read HTML for image injection: %s", e)
            orphaned.extend(images)
            return (injected, orphaned)

        soup = BeautifulSoup(html_content, "html.parser")
        img_tags = soup.find_all("img")

        img_by_idx: dict[int, list[ImagePlacement]] = {}
        for img in positioned:
            idx = img.element_index
            assert idx is not None, "positioned images must have element_index"
            if idx not in img_by_idx:
                img_by_idx[idx] = []
            img_by_idx[idx].append(img)

        for elem_idx, imgs in img_by_idx.items():
            if elem_idx < 0 or elem_idx >= len(img_tags):
                logger.warning(
                    "element_index %d out of range, %d img tags available",
                    elem_idx,
                    len(img_tags),
                )
                orphaned.extend(imgs)
                continue

            target_tag = img_tags[elem_idx]
            for img in imgs:
                try:
                    img_bytes = self._get_image_bytes(img)
                    src = self._create_data_uri(img_bytes, img.mime_type)
                    new_tag = soup.new_tag("img", src=src)
                    if img.width:
                        new_tag["width"] = img.width
                    if img.height:
                        new_tag["height"] = img.height
                    target_tag.insert_after(new_tag)
                    target_tag = new_tag
                    injected.append(img)
                    logger.debug(
                        "Injected image at element %d: mime=%s",
                        elem_idx,
                        img.mime_type,
                    )
                except Exception as e:
                    logger.error("Failed to inject image: %s", e)
                    orphaned.append(img)

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(str(soup), encoding="utf-8")
        except Exception as e:
            logger.error("Failed to write HTML with images: %s", e)
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
            # C4 fix (defense in depth): validate file_path before reading.
            from orf.mcp.security import PathValidator
            valid, err = PathValidator.validate(img.file_path)
            if not valid:
                raise ValueError(f"file_path rejected: {err}")
            return Path(img.file_path).read_bytes()
        raise ValueError("ImagePlacement must have data_base64 or file_path")

    def _create_data_uri(self, img_bytes: bytes, mime_type: str) -> str:
        import base64

        b64 = base64.b64encode(img_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{b64}"