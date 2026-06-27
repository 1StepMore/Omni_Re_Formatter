"""HTML to PDF conversion channel using WeasyPrint."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from orf.converters.base import BaseConverter, ConversionResult
from orf.converters.options import ConverterOptions
from orf.logging import get_logger

logger = get_logger("channel.html2pdf")


class HTML2PDFConverter(BaseConverter):
    """Convert HTML directly to PDF via WeasyPrint (no intermediate MD step)."""

    @property
    def supported_format(self) -> str:
        return "PDF"

    def validate_input(self, input_path: Path | str) -> bool:
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() in (".html", ".htm")

    def convert(
        self,
        input_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        input_path = Path(input_path)
        output_path = Path(output_path)

        if not self.validate_input(input_path):
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Invalid input file: {input_path}"],
            )

        if importlib.util.find_spec("weasyprint") is None:
            logger.error("WeasyPrint is not installed.")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=["WeasyPrint not installed. Install with: pip install weasyprint"],
            )

        from weasyprint import CSS, HTML

        try:
            logger.info(f"WeasyPrint: Converting {input_path} -> {output_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_kwargs: dict = {}
            opts = options or ConverterOptions()
            if opts.css:
                pdf_kwargs["stylesheets"] = [CSS(string=opts.css)]
            HTML(str(input_path)).write_pdf(str(output_path), **pdf_kwargs)
            logger.debug(f"PDF written to: {output_path}")
            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={"tool": "weasyprint", "engine": "html2pdf"},
            )
        except Exception as e:
            logger.error(f"WeasyPrint HTML->PDF failed: {e}")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"WeasyPrint HTML->PDF failed: {e}"],
            )
