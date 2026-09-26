"""apply-xliff command: Apply XLIFF translation to original document."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import click

from orf.logging import get_logger

logger = get_logger("cli")


@click.command("apply-xliff")
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--xliff", "-x", required=True, help="Translated XLIFF file")
@click.option(
    "--xliff-content",
    "xliff_content",
    type=str,
    help="Inline XLIFF content (alternative to --xliff)",
)
@click.option("--output", "-o", required=True, help="Output file path")
@click.option(
    "--format",
    "-f",
    type=click.Choice(["docx", "pptx", "epub", "html", "odt", "pdf", "json"]),
    default="docx",
    help="Output format",
)
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
@click.option(
    "--images-json",
    "images_json",
    type=click.Path(exists=True),
    help="JSON file with image placement data from OPP",
)
@click.option(
    "--no-cache",
    "no_cache",
    is_flag=True,
    help="Skip the .omni_cache/ cache check (force a fresh conversion)",
)
@click.option(
    "--clear-cache",
    "clear_cache",
    is_flag=True,
    help="Remove all cached ORF outputs and exit",
)
@click.option(
    "--max-file-size-mb",
    type=float,
    default=None,
    help="拒绝超过此大小 (MB) 的文件",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Bypass skeleton format validation (may produce broken output). Use with caution.",
)
@click.option(
    "--original-json",
    "original_json",
    type=click.Path(exists=True),
    default=None,
    help="Path to original JSON file for structure reconstruction (json format only)",
)
@click.option(
    "--skeleton-html",
    "skeleton_html_param",
    type=str,
    default=None,
    help="Path to skeleton HTML (required for PDF format)",
)
def apply_xliff(
    input_file: str,
    xliff: str,
    xliff_content: Optional[str],
    output: str,
    format: str,
    output_json: bool,
    images_json: str,
    no_cache: bool,
    clear_cache: bool,
    max_file_size_mb: float | None = None,
    force: bool = False,
    original_json: str | None = None,
    skeleton_html_param: str | None = None,
) -> None:
    # Lazy imports from orf.cli to avoid circular imports
    from orf.cli import (
        _clear_orf_cache,
        _cache_root,
        _cache_key_apply_xliff,
        _check_cache,
        _write_cache,
        _error_item_to_dict,
        _error_item_to_str,
        _warning_item_to_dict,
        _safe_json_dumps,
    )

    """Apply XLIFF translation to original document.

    INPUT_FILE: Original document (DOCX/PPTX/EPUB/HTML) or skeleton (XLIFF/ZIP).
    
    XLIFF backfill is format-preserving — the skeleton file extension must match
    --format by default. Use --force to bypass this validation for experimental
    cross-format conversion (output may be incomplete or invalid).
    """
    input_path = Path(input_file)
    output_path = Path(output)

    # Mutually exclusive check
    if xliff and xliff_content:
        raise click.BadParameter("--xliff and --xliff-content are mutually exclusive")

    # Handle inline XLIFF content
    xliff_path = Path(xliff)
    if xliff_content:
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".xliff", delete=False) as tmp:
            tmp.write(xliff_content)
            xliff_path = Path(tmp.name)

    if max_file_size_mb is not None:
        for check_path in [input_path, xliff_path]:
            size_mb = check_path.stat().st_size / (1024 * 1024)
            if size_mb > max_file_size_mb:
                click.echo(
                    f"Error: File {check_path} is {size_mb:.1f}MB, exceeds limit of {max_file_size_mb}MB",
                    err=True,
                )
                raise SystemExit(1)

    if clear_cache:
        n = _clear_orf_cache()
        logger.info(f"Cleared {n} cached file(s) from {_cache_root()}")
        click.echo(f"Cleared {n} cached file(s) from {_cache_root()}")
        return

    cache_key = _cache_key_apply_xliff(input_path, xliff_path, format, images_json)
    if _check_cache(cache_key, output_path, f".{format}", no_cache=no_cache):
        logger.info(f"Cache hit for {input_path.name} -> {output_path}")
        if output_json:
            click.echo(
                _safe_json_dumps(
                    {
                        "success": True,
                        "output_path": str(output_path),
                        "errors": [],
                        "warnings": [],
                        "metadata": {"cache_hit": True},
                    }
                )
            )
        else:
            click.echo(f"Created {output_path} (cached)")
        return

    images = None
    if images_json:
        import json

        try:
            with open(images_json) as f:
                images_data = json.load(f)
            from orf.mcp.schemas import ImagePlacement

            if isinstance(images_data, dict) and "images" in images_data:
                images_data = images_data["images"]
            images = [ImagePlacement(**img) for img in images_data]
            logger.info(f"Loaded {len(images)} images from {images_json}")
        except Exception as e:
            logger.warning(f"Failed to load images from {images_json}: {e}")

    logger.info(
        f"Applying XLIFF {xliff_path} to {input_path} -> {output_path} ({format})"
    )

    # 2026-06-17 round 5 (FIX-#8): XLIFF backfill is format-preserving —
    # fail early on a mismatched skeleton rather than letting
    # translate-toolkit crash with an abstract error.
    # Round 9: also accept .zip (OPP's skeleton.zip packaging).
    _FORMAT_EXT = {
        "docx": ".docx",
        "pptx": ".pptx",
        "epub": ".epub",
        "html": ".html",
        "odt": ".odt",
        "pdf": ".pdf",
        "json": ".json",
    }
    _ZIP_FORMATS = {"docx", "pptx", "epub"}
    if format in _FORMAT_EXT:
        expected_ext = _FORMAT_EXT[format]
        actual_ext = input_path.suffix.lower()
        valid_exts = {".xlf", ".xliff", expected_ext}
        if format in _ZIP_FORMATS:
            valid_exts.add(".zip")
        if actual_ext and actual_ext not in valid_exts:
            if not force:
                raise click.BadParameter(
                    f"Skeleton file extension '{actual_ext}' does not match "
                    f"--format '{format}' (expected '{expected_ext}' or '.zip'). "
                    f"XLIFF backfill is format-preserving; use the MD path for "
                    f"cross-format conversion, or pass --force to attempt anyway "
                    f"(output may be incomplete or invalid)."
                )
            else:
                logger.warning(
                    f"FORCE MODE: Skeleton extension '{actual_ext}' does not match "
                    f"--format '{format}'. Continuing with --force flag — output may be broken."
                )

    # Skeleton content-level format validation for ZIP-based skeletons
    # Peek inside .skeleton.zip files to detect actual format and compare
    # with --format. This catches mismatches that the extension check
    # cannot (e.g., a DOCX skeleton renamed to .zip with --format=pptx).
    if format in _ZIP_FORMATS and input_path.suffix.lower() == ".zip":
        from orf.detection.format_detector import FormatDetector

        detected = None
        try:
            detector = FormatDetector()
            detected = detector.detect_from_skeleton(str(input_path))
        except Exception:
            logger.debug(
                f"Could not detect format from skeleton {input_path}, "
                f"skipping content-level validation"
            )

        if detected is not None:
            detected_lower = detected.lower()
            if detected_lower != format:
                msg = (
                    f"Skeleton ZIP contains '{detected}' format content, "
                    f"but --format is '{format}'. "
                    f"XLIFF backfill is format-preserving; use the MD path for "
                    f"cross-format conversion, or pass --force to attempt anyway "
                    f"(output may be incomplete or invalid)."
                )
                if not force:
                    raise click.BadParameter(msg)
                else:
                    logger.warning(
                        f"FORCE MODE: Skeleton ZIP contains '{detected}' format, "
                        f"but --format is '{format}'. Continuing with --force flag — "
                        f"output may be broken."
                    )

    from orf.converters.options import ConverterOptions

    opts = ConverterOptions()
    if skeleton_html_param:
        opts.skeleton_html = skeleton_html_param

    converter: Any
    if format == "docx":
        from orf.channels.xliff2docx import XLIFF2DOCXConverter

        converter = XLIFF2DOCXConverter()
    elif format == "pptx":
        from orf.channels.xliff2pptx import XLIFF2PPTXConverter

        converter = XLIFF2PPTXConverter()
    elif format == "epub":
        from orf.channels.xliff2epub import XLIFF2EPUBConverter

        converter = XLIFF2EPUBConverter()
    elif format == "html":
        from orf.channels.xliff2html import XLIFF2HTMLConverter

        converter = XLIFF2HTMLConverter()
    elif format == "odt":
        from orf.channels.xliff2odf import XLIFF2ODFConverter

        converter = XLIFF2ODFConverter()
    elif format == "pdf":
        from orf.channels.xliff2pdf import XLIFF2PDFConverter

        converter = XLIFF2PDFConverter()
    elif format == "json":
        from orf.channels.xliff2json import apply_xliff_to_json

        orig_json_path: Path | None = None
        if original_json is not None:
            orig_json_path = Path(original_json)
        conv_result = apply_xliff_to_json(
            xliff_path=xliff_path,
            output_path=output_path,
            skeleton_path=input_path,
            original_json_path=orig_json_path,
        )
        if conv_result.get("success", True):
            _write_cache(cache_key, output_path, ".json", no_cache=no_cache)
            logger.info(f"JSON XLIFF backfill successful: {output_path}")
            if output_json:
                click.echo(_safe_json_dumps({
                    "success": True,
                    "output_path": str(output_path),
                    "errors": [],
                    "warnings": [],
                    "metadata": {"unit_count": conv_result.get("unit_count", 0)},
                }))
            else:
                click.echo(f"Created {output_path}")
        else:
            logger.error("JSON XLIFF backfill failed")
            err_msg = conv_result.get("error", "Unknown error")
            if output_json:
                click.echo(_safe_json_dumps({
                    "success": False,
                    "output_path": str(output_path),
                    "errors": [{"code": "JSON_BACKFILL_ERROR", "message": err_msg}],
                    "warnings": [],
                    "metadata": {},
                }))
                raise SystemExit(1)
            else:
                raise click.ClickException(f"JSON XLIFF backfill failed: {err_msg}")
        return
    else:
        raise click.ClickException(
            f"Unsupported format '{format}'\n"
            f"Hint: Valid formats are: docx, pptx, epub, html, odt, pdf, json\n"
            f"       Use --format <format> to specify"
        )

    # e2e-test-suite#86: honor --force here like the two skeleton checks
    # above — without this guard a raw cross-format skeleton is re-rejected
    # after already passing with a FORCE MODE warning.
    if not force and not converter.validate_input(input_path):
        raise click.ClickException(
            f"Input file '{input_path}' is not valid for {format} format. "
            f"Skeleton file must have a valid extension (.{format} or .zip)."
        )

    result = converter.convert(input_path, xliff_path, output_path, options=opts)

    if result.success and images:
        import tempfile
        import shutil

        try:
            tmp_skeleton = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
            tmp_skeleton.close()
            shutil.copy2(result.output_path, tmp_skeleton.name)

            injected, orphaned = converter.inject_images(
                tmp_skeleton.name, images, result.output_path
            )
            Path(tmp_skeleton.name).unlink(missing_ok=True)
            logger.info(
                f"Injected {len(injected)} images, {len(orphaned)} orphaned"
            )
            if orphaned:
                for img in orphaned:
                    logger.warning(
                        f"Orphaned image (no position): mime_type={img.mime_type}"
                    )
        except AttributeError:
            logger.warning(
                "Converter does not support image injection, continuing without images"
            )
        except Exception as e:
            logger.error(f"Image injection failed: {e}")

    if result.success:
        _write_cache(cache_key, output_path, f".{format}", no_cache=no_cache)
        logger.info(f"Conversion successful: {result.output_path}")
        if output_json:
            click.echo(
                _safe_json_dumps(
                    {
                        "success": True,
                        "output_path": str(result.output_path),
                        "errors": [_error_item_to_dict(e) for e in result.errors],
                        "warnings": [
                            _warning_item_to_dict(w) for w in result.warnings
                        ],
                        "metadata": result.metadata,
                    }
                )
            )
        else:
            click.echo(f"Created {result.output_path}")
    else:
        logger.error(f"Conversion failed: {result.errors}")
        errors_str = (
            ", ".join(_error_item_to_str(e) for e in result.errors)
            if result.errors
            else "Unknown error"
        )
        if output_json:
            click.echo(
                _safe_json_dumps(
                    {
                        "success": False,
                        "output_path": str(result.output_path)
                        if result.output_path
                        else None,
                        "errors": [_error_item_to_dict(e) for e in result.errors],
                        "warnings": [
                            _warning_item_to_dict(w) for w in result.warnings
                        ],
                        "metadata": result.metadata,
                    }
                )
            )
            raise SystemExit(1)
        else:
            raise click.ClickException(
                f"Conversion failed: {errors_str}\n"
                f"Hint: 1) Check XLIFF file is valid\n"
                f"       2) Verify original document exists\n"
                f"       3) Try --verbose for detailed logs"
            )
