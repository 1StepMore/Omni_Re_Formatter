"""XLIFF to PDF conversion: composes xliff2html + html2pdf.

Pipeline:
    skeleton.html (from OPP) -> XLIFF2HTMLConverter -> translated.html
    translated.html -> HTML2PDFConverter -> output.pdf
"""

from __future__ import annotations

from pathlib import Path

from orf.converters.base import BaseConverter, ConversionResult
from orf.converters.options import ConverterOptions
from orf.logging import get_logger

logger = get_logger("channel.xliff2pdf")


class XLIFF2PDFConverter(BaseConverter):
    """Apply XLIFF translation to PDF via HTML intermediate.

    Requires ``skeleton_html`` in options: the raw HTML from OPP extraction
    (with ``data-trans-unit-id`` attributes) — NOT derived from the PDF.
    """

    @property
    def supported_format(self) -> str:
        return "PDF"

    def validate_input(self, input_path: Path | str) -> bool:
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() == ".pdf"

    def convert(  # type: ignore[override]
        self,
        input_path: Path | str,
        xliff_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        input_path = Path(input_path)
        xliff_path = Path(xliff_path)
        output_path = Path(output_path)
        opts = options or ConverterOptions()

        if not opts.skeleton_html:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[
                    "skeleton_html is required for XLIFF->PDF conversion. "
                    "Provide --skeleton-html pointing to OPP's extracted HTML."
                ],
            )

        skeleton_html = Path(opts.skeleton_html)
        if not skeleton_html.exists():
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"skeleton_html not found: {skeleton_html}"],
            )

        from orf.channels.xliff2html import XLIFF2HTMLConverter
        from orf.channels.html2pdf import HTML2PDFConverter

        translated_html = output_path.with_suffix(".html")

        html_converter = XLIFF2HTMLConverter()
        html_result = html_converter.convert(skeleton_html, xliff_path, translated_html, options=options)
        if not html_result.success:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"XLIFF->HTML failed: {html_result.errors}"],
            )

        pdf_converter = HTML2PDFConverter()
        pdf_result = pdf_converter.convert(translated_html, output_path, options=options)
        if not pdf_result.success:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"HTML->PDF failed: {pdf_result.errors}"],
            )

        return ConversionResult(
            output_path=output_path,
            success=True,
            metadata={
                "tool": "orf.xliff2pdf",
                "skeleton_html": str(skeleton_html),
                "translated_html": str(translated_html),
            },
        )
