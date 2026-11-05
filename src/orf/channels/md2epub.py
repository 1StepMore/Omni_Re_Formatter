"""Markdown to EPUB conversion channel using Pandoc."""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

from orf.converters.base import BaseConverter, ConversionResult
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.logging import get_logger
from orf.converters.options import ConverterOptions

logger = get_logger("channel.md2epub")

_CONVERSION_TIMEOUT = int(os.environ.get("ORF_CONVERSION_TIMEOUT", "300"))


def _deterministic_pandoc_env(source_path: Path) -> dict[str, str]:
    """Env for pandoc with SOURCE_DATE_EPOCH derived from the input bytes.

    Pandoc stamps EPUB metadata (``dcterms:modified``, the ``dc:date``
    fallback) and every zip member with the wall clock, so two runs of the
    same conversion produced different EPUB bytes. Unlike the DOCX
    channel's mtime-based pin (``md2docx._deterministic_pandoc_env``), the
    epoch derives from the INPUT CONTENT: apply-md stages every conversion
    through a fresh temp sibling whose mtime changes per run, so an
    mtime-derived pin would break cross-directory reproducibility.
    """
    env = dict(os.environ)
    try:
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        env["SOURCE_DATE_EPOCH"] = str(int(digest[:8], 16) % (2**31 - 1))
    except OSError:
        pass
    return env


def _stable_identifier(source_path: Path) -> str:
    """Content-derived UUID for the EPUB ``dc:identifier`` / ``dtb:uid``.

    Without identifier metadata pandoc generates a fresh random UUID per
    run (its EPUB writer calls getRandomUUID) — SOURCE_DATE_EPOCH cannot
    pin that. A uuid5 over the input bytes is stable for identical inputs.
    """
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:orf:epub:{digest}"))


class MD2EPUBConverter(BaseConverter):
    """Markdown to EPUB converter using Pandoc."""

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
    ) -> None:
        super().__init__(manifest, frontmatter)

    @property
    def supported_format(self) -> str:
        return "EPUB"

    def validate_input(self, input_path: Path | str) -> bool:
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() == ".md"

    def inject_images(
        self,
        skeleton_path: Path | str,
        images: list[Any],
        output_path: Path | str,
    ) -> tuple[list[Any], list[Any]]:
        """EPUB format does not support external image injection.
        Images are embedded by pandoc during conversion.

        Returns empty embedded list and unchanged images list.
        """
        return [], images

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

        cmd = [
            "pandoc",
            str(input_path),
            "-o", str(output_path),
            "--to", "epub",
        ]

        if opts.css:
            cmd.extend(["--css", opts.css])
        if opts.title:
            cmd.extend(["--metadata", f"title={opts.title}"])
        if opts.author:
            cmd.extend(["--metadata", f"author={opts.author}"])
        if opts.lang:
            cmd.extend(["--metadata", f"lang={opts.lang}"])
        if opts.toc:
            cmd.append("--toc")
        if opts.embed_images:
            cmd.append("--self-contained")
        # Deterministic EPUB (#P1-A): replace pandoc's per-run random UUID
        # with a content-derived identifier, so identical inputs produce
        # byte-identical EPUBs. (Wall-clock stamps are pinned separately via
        # SOURCE_DATE_EPOCH on the subprocess env below.)
        cmd.extend(["--metadata", f"identifier={_stable_identifier(input_path)}"])

        try:
            logger.info(f"Running: {' '.join(cmd)}")
            cmd[1] = str(input_path.resolve())
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=str(input_path.parent),
                timeout=_CONVERSION_TIMEOUT,
                env=_deterministic_pandoc_env(input_path),
            )

            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={"tool": "pandoc", "cmd": " ".join(cmd)},
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Pandoc timed out after {_CONVERSION_TIMEOUT}s")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Pandoc timed out after {_CONVERSION_TIMEOUT}s"],
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Pandoc failed: {e.stderr}")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Pandoc error: {e.stderr}"],
            )
        except FileNotFoundError:
            logger.error("Pandoc not found in PATH")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=["Pandoc not installed or not in PATH"],
            )
