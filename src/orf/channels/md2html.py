"""Markdown to HTML conversion channel using the Python markdown library (pandoc-independent)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import markdown

from orf.converters.base import BaseConverter, ConversionResult
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.logging import get_logger
from orf.converters.options import ConverterOptions

logger = get_logger("channel.md2html")


class MD2HTMLConverter(BaseConverter):
    """Markdown to HTML converter using the Python markdown library (pandoc-independent)."""

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
        css: Optional[Path | str] = None,
    ) -> None:
        super().__init__(manifest, frontmatter)
        self.css = Path(css) if css else None

    @property
    def supported_format(self) -> str:
        return "HTML"

    def validate_input(self, input_path: Path | str) -> bool:
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() == ".md"

    def convert(
        self,
        input_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        input_path = Path(input_path)
        output_path = Path(output_path)
        opts = options or ConverterOptions()

        if not self.validate_input(input_path):
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Invalid input file: {input_path}"],
            )

        try:
            md_content = input_path.read_text(encoding="utf-8")

            # Strip YAML frontmatter before markdown conversion
            # (the --- delimiters are interpreted as <hr> by the markdown lib)
            from orf.parsers.frontmatter import strip_frontmatter
            md_content = strip_frontmatter(md_content)

            extensions = ["extra", "codehilite", "toc", "tables", "fenced_code"]
            md = markdown.Markdown(extensions=extensions)
            body_html = md.convert(md_content)

            css = opts.css or self.css
            css_link = f'<link rel="stylesheet" href="{css}">' if css else ""

            html = (
                '<!DOCTYPE html>\n'
                '<html lang="en">\n'
                '<head>\n'
                '<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
                '<title>Converted Document</title>\n'
                f'{css_link}\n'
                '<style>\n'
                'body {\n'
                '    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;\n'
                '    max-width: 48rem;\n'
                '    margin: 0 auto;\n'
                '    padding: 2rem;\n'
                '    line-height: 1.6;\n'
                '    color: #333;\n'
                '}\n'
                'pre { background: #f4f4f4; padding: 1rem; border-radius: 4px; overflow-x: auto; }\n'
                'code { font-family: "Fira Code", "Cascadia Code", monospace; font-size: 0.9em; }\n'
                'table { border-collapse: collapse; width: 100%; }\n'
                'th, td { border: 1px solid #ddd; padding: 0.5rem; text-align: left; }\n'
                'th { background: #f4f4f4; }\n'
                '</style>\n'
                '</head>\n'
                '<body>\n'
                f'{body_html}\n'
                '</body>\n'
                '</html>\n'
            )

            output_path.write_text(html, encoding="utf-8")

            logger.info(f"Converted {input_path} to HTML using markdown library")

            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={"tool": "markdown", "extensions": extensions},
            )

        except Exception as e:
            logger.error(f"HTML conversion failed: {e}")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"HTML conversion error: {e}"],
            )

    def inject_images(
        self,
        skeleton_path: Path | str,
        images: list,
        output_path: Path | str,
    ) -> tuple[list, list]:
        logger.warning(
            "MD2HTML does not support inject_images via paragraph_index. "
            "Images in MD are handled by Pandoc automatically."
        )
        return ([], images)