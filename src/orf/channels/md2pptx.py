"""Markdown to PPTX conversion channel using md2pptx CLI."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from logging import Logger
from typing import Optional

from orf.converters.base import BaseConverter, ConversionResult
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.logging import get_logger
from orf.converters.options import ConverterOptions

logger = get_logger("channel.md2pptx")

_TIP_MD2PPTX_FALLBACK = (
    "💡 Tip: For higher-quality PPTX output, install md2pptx:\n"
    "   dotnet tool install --global md2pptx\n"
    "   (or download a release binary from "
    "https://github.com/MartinPacker/md2pptx/releases)\n"
    "   Otherwise, ORF will use the pandoc fallback which produces "
    "lower-quality output."
)
_tip_shown = False


def _show_md2pptx_tip_once(logger_inst: Logger) -> None:
    global _tip_shown
    if not _tip_shown:
        logger_inst.info(_TIP_MD2PPTX_FALLBACK)
        _tip_shown = True


def _md2pptx_install_hint() -> str:
    """Return an install hint for the md2pptx binary.

    md2pptx is a .NET tool, not a pip package.
    """
    return (
        "md2pptx binary not found in PATH. md2pptx is a .NET tool, not a "
        "pip package. Install one of:\n"
        "  - .NET SDK + 'dotnet tool install --global md2pptx'\n"
        "  - Or download a release binary from "
        "https://github.com/MartinPacker/md2pptx/releases and place it "
        "on PATH (e.g. /usr/local/bin/md2pptx)\n"
        "  - Or use the 'pandoc' backend (--target-format pptx via pandoc) "
        "as a fallback."
    )


class MD2PPTXConverter(BaseConverter):
    """Markdown to PPTX converter using md2pptx CLI."""

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
    ) -> None:
        super().__init__(manifest, frontmatter)

    @property
    def supported_format(self) -> str:
        return "PPTX"

    def validate_input(self, input_path: Path | str) -> bool:
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() == ".md"

    def _convert_via_pandoc(
        self, input_path: Path, output_path: Path
    ) -> ConversionResult:
        """Fallback: convert MD to PPTX via pandoc when md2pptx is unavailable."""
        _show_md2pptx_tip_once(logger)
        warning_msg = (
            "md2pptx not found, using pandoc fallback — quality may differ. "
            "Install md2pptx for higher-quality output (tip shown above)."
        )
        logger.warning(warning_msg)
        cmd = ["pandoc", str(input_path), "-o", str(output_path)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
            result.check_returncode()
            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={
                    "tool": "pandoc",
                    "fallback_from": "md2pptx",
                    "warning": warning_msg,
                },
            )
        except subprocess.CalledProcessError as e:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[
                    {
                        "code": "PANDOC_FAILED",
                        "message": f"pandoc conversion failed: {e.stderr.strip()}",
                    }
                ],
            )
        except FileNotFoundError:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[
                    {
                        "code": "PANDOC_NOT_FOUND",
                        "message": "pandoc binary not found after pre-flight check",
                    }
                ],
            )
        except subprocess.TimeoutExpired:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[
                    {
                        "code": "PANDOC_TIMEOUT",
                        "message": "pandoc conversion timed out after 60 seconds",
                    }
                ],
            )

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

        # E2E-79: pre-flight check for the md2pptx binary. The previous
        # code relied on FileNotFoundError being raised by subprocess.run
        # which gave no actionable guidance. Now we surface the install
        # hint up front.
        # ORF#7: if md2pptx is missing but pandoc is available, fall back
        # to pandoc automatically (graceful degradation).
        if shutil.which("md2pptx") is None:
            if shutil.which("pandoc") is not None:
                return self._convert_via_pandoc(input_path, output_path)
            hint = _md2pptx_install_hint()
            logger.error(hint)
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[hint],
            )

        # md2pptx (MartinPacker) uses positional INPUT OUTPUT — no -o flag.
        # The earlier `md2pptx INPUT -o OUTPUT` form made md2pptx treat "-o"
        # as the output filename and silently created a file literally named
        # "-o" in CWD. See .omo/plans/2026-06-17-fix-plan-round-13.md.
        cmd = [
            "md2pptx",
            str(input_path),
            str(output_path),
        ]

        try:
            logger.info(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )

            logger.debug(f"md2pptx output: {result.stdout}")
            if result.stderr:
                logger.warning(f"md2pptx stderr: {result.stderr}")

            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={"tool": "md2pptx", "cmd": " ".join(cmd)},
            )

        except subprocess.CalledProcessError as e:
            logger.error(f"md2pptx failed: {e.stderr}")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"md2pptx error: {e.stderr}"],
            )
        except FileNotFoundError:
            # E2E-79: rare race — binary on PATH at pre-flight but gone
            # now. Surface the same hint.
            hint = _md2pptx_install_hint()
            logger.error(hint)
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[hint],
            )