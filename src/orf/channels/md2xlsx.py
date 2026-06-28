"""Markdown to XLSX conversion channel."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from openpyxl import Workbook

from orf.converters.base import BaseConverter, ConversionResult
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.logging import get_logger
from orf.converters.options import ConverterOptions

logger = get_logger("channel.md2xlsx")


class MD2XLSXConverter(BaseConverter):
    """Markdown to XLSX converter using openpyxl."""

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
    ) -> None:
        super().__init__(manifest, frontmatter)

    @property
    def supported_format(self) -> str:
        return "XLSX"

    def validate_input(self, input_path: Path | str) -> bool:
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() == ".md"

    def _parse_table_rows(self, content: str) -> list[list[str]]:
        """Parse markdown table rows from content.

        Handles both GFM-style tables (with outer | pipes) and
        simplified tables where outer pipes are optional. Also
        tolerates leading/trailing whitespace.

        Args:
            content: Markdown content

        Returns:
            List of rows, each row is a list of cell values
        """
        lines = content.split("\n")
        rows = []

        for line in lines:
            line = line.strip()
            # Table row must contain at least one | and at least 2 cells
            if "|" not in line or line.count("|") < 2:
                continue

            # Normalize: add leading/trailing | if missing
            if not line.startswith("|"):
                line = "|" + line
            if not line.endswith("|"):
                line = line + "|"

            # Skip separator lines like |---|---|
            if re.match(r"^\|[\s\-:|]+\|$", line):
                continue

            # Strip leading/trailing | and split by |
            cells = [cell.strip() for cell in line[1:-1].split("|")]
            rows.append(cells)

        return rows

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

        sheet_name = opts.sheet_name

        try:
            content = input_path.read_text(encoding="utf-8")
            rows = self._parse_table_rows(content)

            # Handle case with no table
            if not rows:
                # Create empty workbook
                wb = Workbook()
                ws = wb.active
                ws.title = sheet_name
                wb.save(output_path)
                return ConversionResult(
                    output_path=output_path,
                    success=True,
                    metadata={"format": "XLSX", "rows": 0},
                )

            headers = rows[0]
            data_rows = rows[1:] if len(rows) > 1 else []

            wb = Workbook(write_only=True)
            ws = wb.create_sheet(title=sheet_name)
            ws.append(headers)
            for row in data_rows:
                ws.append(row)

            wb.save(output_path)

            logger.debug(f"XLSX written: {output_path} with {len(data_rows) if rows else 0} rows")

            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={"format": "XLSX", "rows": len(data_rows) if rows else 0},
            )

        except Exception as e:
            logger.error(f"XLSX conversion failed: {e}")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[str(e)],
            )

    def inject_images(
        self,
        skeleton_path: Path | str,
        images: list,
        output_path: Path | str,
    ) -> tuple[list, list]:
        logger.warning(
            "MD2XLSX does not support inject_images via paragraph_index. "
            "Images in MD are handled by Pandoc automatically. "
            "Use --embed-media with Pandoc for inline image embedding."
        )
        return ([], images)