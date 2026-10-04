"""XLIFF to HTML conversion channel with inline formatting preservation."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Any
import zipfile

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


def _read_html_template(template: Path, encoding: str) -> str:
    """Return the HTML document from a bare ``.html`` file or a skeleton zip.

    OPP packages the HTML skeleton as ``<stem>.skeleton.zip`` holding a single
    top-level HTML entry (e2e-test-suite#92), so a zip arriving here must be
    unpacked rather than read as text. Entry selection deliberately mirrors
    ``FormatDetector.detect_from_skeleton`` — top-level (no ``/``) ``.html``/
    ``.htm`` entries only — so this converter and the format gate agree on what
    counts as an HTML skeleton.
    """
    if template.suffix.lower() != ".zip":
        return template.read_text(encoding=encoding)

    with zipfile.ZipFile(template, "r") as zf:
        candidates = [
            name for name in zf.namelist()
            if name.lower().endswith((".html", ".htm")) and "/" not in name
        ]
        if not candidates:
            raise ValueError(
                f"Skeleton zip {template.name} contains no top-level HTML entry"
            )
        return zf.read(sorted(candidates)[0]).decode(encoding)


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

        Accepts a bare ``.html``/``.htm`` document and also ``.zip``, because
        OPP packages its HTML skeleton as ``<stem>.skeleton.zip`` (a top-level
        ``index.html`` entry) since e2e-test-suite#92. For a zip, the archive's
        HTML-ness is verified upstream by the ``--format`` extension check plus
        ``FormatDetector.detect_from_skeleton``, so accepting the extension here
        does not weaken format validation.

        Args:
            input_path: Path to the HTML template file, or its skeleton zip

        Returns:
            True if the input file exists and has .html/.htm/.zip extension
        """
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() in (
            ".html",
            ".htm",
            ".zip",
        )

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
            html_content = _read_html_template(html_template, opts.encoding)
        except Exception as e:
            logger.warning("Failed to read HTML template: %s", e, exc_info=True)
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
            xliff_content = xliff_path.read_text(encoding=opts.encoding)
        except Exception as e:
            logger.warning("Failed to read XLIFF file: %s", e, exc_info=True)
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Failed to read XLIFF file: {e}"],
            )

        # 3. Parse XLIFF and extract translations
        try:
            from orf.channels.xliff2html.parser import parse_xliff

            translations = parse_xliff(xliff_content)
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
            logger.warning("Failed to write output HTML: %s", e, exc_info=True)
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

        Delegates to orf.channels.xliff2html.parser.parse_xliff.
        """
        from orf.channels.xliff2html.parser import parse_xliff

        return parse_xliff(xliff_content)

    def _strip_xliff_inline_tags(self, text: str) -> str:
        """Remove XLIFF inline formatting tags from text.

        Delegates to orf.channels.xliff2html.parser.strip_xliff_inline_tags.
        """
        from orf.channels.xliff2html.parser import strip_xliff_inline_tags

        return strip_xliff_inline_tags(text)

    def _apply_translations_and_formatting(
        self,
        html_content: str,
        translations: dict[str, str],
        xliff_content: str,
        options: ConverterOptions | None = None,
    ) -> str:
        """Apply translations (and optionally inline formatting) via lxml DOM.

        Delegates to orf.channels.xliff2html.writer.apply_translations_and_formatting.
        """
        from orf.channels.xliff2html.writer import apply_translations_and_formatting

        return apply_translations_and_formatting(
            html_content,
            translations,
            xliff_content,
            self.inline_parser,
            self.html_applier,
            _INLINE_TRANSLATION_SENTINEL,
            _PRESERVED_CHILD_TAGS,
            self._strip_xliff_inline_tags,
            options,
        )

    def _parse_html_fragment(self, fragment: str) -> list[Any]:
        """Parse an HTML fragment string into a list of lxml child elements.

        Delegates to orf.channels.xliff2html.writer.parse_html_fragment.
        """
        from orf.channels.xliff2html.writer import parse_html_fragment

        return parse_html_fragment(fragment)

    def _wrap_translation_with_xliff_inline(
        self,
        xliff_content: str,
        unit_id: str,
    ) -> Optional[str]:
        """Wrap translation with XLIFF inline tags.

        Delegates to orf.channels.xliff2html.writer.wrap_translation_with_xliff_inline.
        """
        from orf.channels.xliff2html.writer import wrap_translation_with_xliff_inline

        return wrap_translation_with_xliff_inline(
            xliff_content, unit_id, _INLINE_TRANSLATION_SENTINEL
        )

    def _inject_translations_into_dom(
        self,
        root: Any,
        translations: dict[str, Any],
    ) -> None:
        """Inject translations into the DOM.

        Delegates to orf.channels.xliff2html.writer.inject_translations_into_dom.
        """
        from orf.channels.xliff2html.writer import inject_translations_into_dom

        inject_translations_into_dom(root, translations, _PRESERVED_CHILD_TAGS)

    def _backfill_by_text_match(
        self,
        root: Any,
        translations: dict[str, str],
        xliff_content: str,
    ) -> int:
        """Fallback text-matching translation.

        Delegates to orf.channels.xliff2html.writer.backfill_by_text_match.
        """
        from orf.channels.xliff2html.writer import backfill_by_text_match

        return backfill_by_text_match(
            root, translations, xliff_content, self._strip_xliff_inline_tags
        )

    def inject_images(
        self,
        html_path: Path | str,
        images: list[ImagePlacement],
        output_path: Path | str,
    ) -> tuple[list[ImagePlacement], list[ImagePlacement]]:
        """Inject images into HTML at specified DOM element positions.

        Delegates to orf.channels.xliff2html.images.inject_images_into_html.
        """
        from orf.channels.xliff2html.images import inject_images_into_html

        return inject_images_into_html(
            html_path, images, output_path, get_image_bytes_fn=self._get_image_bytes
        )

    def _get_image_bytes(self, img: ImagePlacement) -> bytes:
        """Extract image bytes from an ImagePlacement.

        Delegates to orf.channels.xliff2html.images.get_image_bytes.
        """
        from orf.channels.xliff2html.images import get_image_bytes

        return get_image_bytes(img)

    def _create_data_uri(self, img_bytes: bytes, mime_type: str) -> str:
        """Create a data URI from image bytes and MIME type.

        Delegates to orf.channels.xliff2html.images.create_data_uri.
        """
        from orf.channels.xliff2html.images import create_data_uri

        return create_data_uri(img_bytes, mime_type)
