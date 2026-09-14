"""apply-md command: Convert MD file to target format."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import click

from orf.converters.atomic import discard, publish, temp_sibling_path
from orf.converters.options import ConverterOptions
from orf.parsers.manifest import parse_manifest, find_manifest, ManifestParseError
from orf.parsers.frontmatter import (
    parse_frontmatter,
    FrontmatterParseError,
)
from orf.error_handlers.conversion_error import FormatDetectionError
from orf.logging import get_logger

logger = get_logger("cli")


@click.command("apply-md")
@click.argument("input_md", type=click.Path(exists=True))
@click.option(
    "--target-format",
    "-t",
    type=click.Choice(
        [
            "auto",
            "docx",
            "odt",
            "epub",
            "html",
            "rtf",
            "pdf",
            "pptx",
            "csv",
            "json",
            "xlsx",
            "xml",
            "ipynb",
            "eml",
            "msg",
            "icml",
            "srt",
        ]
    ),
    default="docx",
    help="目标格式",
)
@click.option("--auto-detect", is_flag=True, help="自动检测输入文件格式")
@click.option("--output", "-o", type=click.Path(), help="输出文件路径")
@click.option("--template", type=click.Path(), help="Pandoc reference 模板路径")
@click.option(
    "--reference-doc",
    type=click.Path(exists=True),
    help="Reference DOCX for document styles (maps to pandoc --reference-doc)",
)
@click.option(
    "--reference-doc-content",
    "reference_doc_content",
    type=str,
    default=None,
    help=(
        "Inline base64-encoded DOCX bytes (alternative to --reference-doc). "
        "Mutually exclusive with --reference-doc. "
        "Prefix with '@' to read from a file: e.g. '@ref.docx.b64'."
    ),
)
@click.option("--title", type=str, help="EPUB 标题")
@click.option("--author", type=str, help="EPUB 作者")
@click.option("--lang", type=str, default="zh", help="EPUB 语言")
@click.option("--embed-images", is_flag=True, help="EPUB 嵌入图片")
@click.option("--css", type=click.Path(exists=True), help="EPUB CSS 样式表路径（传递给 pandoc --css）")
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
@click.option(
    "--text-only",
    "text_only",
    is_flag=True,
    help="产生纯文本输出，跳过所有图片引用和占位符",
)
@click.option(
    "--separate-images/--no-separate-images",
    "separate_images",
    default=None,
    help="将图片从 DOCX 中分离为独立 assets（默认开启）",
)
@click.option(
    "--images-json",
    "images_json",
    type=click.Path(exists=True),
    help="JSON file with image placement data from OPP (NOTE: MD path extracts images for organized output, not injection)",
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
def apply_md(
    input_md: str,
    target_format: str,
    auto_detect: bool,
    output: str | None,
    template: str | None,
    reference_doc: str | None,
    reference_doc_content: str | None,
    title: str | None,
    author: str | None,
    lang: str,
    embed_images: bool,
    css: str | None,
    output_json: bool,
    text_only: bool,
    separate_images: bool | None,
    images_json: str | None,
    no_cache: bool,
    clear_cache: bool,
    max_file_size_mb: float | None = None,
) -> None:
    # Lazy imports from orf.cli to avoid circular imports
    from orf.cli import (
        _clear_orf_cache,
        _cache_root,
        _cache_key_apply_md,
        _check_cache,
        _write_cache,
        _error_item_to_dict,
        _error_item_to_str,
        _warning_item_to_dict,
        _safe_json_dumps,
    )

    """将 MD 文件转换为目标格式

    INPUT_MD: 输入的 MD 文件路径（通常由 OL 翻译后的文件）

    注意：MD 格式是行级别的，而非段落级别。Pandoc 将 ![Image](url) 作为行内元素处理，
    原始 DOCX 的段落边界在 MD 输出中不会保留。对于需要精确结构的文档（如图片+标题段落），
    请使用 XLIFF 管道。
    """
    input_path = Path(input_md)
    if max_file_size_mb is not None:
        size_mb = input_path.stat().st_size / (1024 * 1024)
        if size_mb > max_file_size_mb:
            click.echo(
                f"Error: File {input_path} is {size_mb:.1f}MB, exceeds limit of {max_file_size_mb}MB",
                err=True,
            )
            raise SystemExit(1)

    # Handle --reference-doc-content: write inline content to a temp file
    # and use that as the effective_template. Mutually exclusive with
    # --reference-doc. Supports '@' prefix to read from a file.
    if reference_doc_content is not None and reference_doc is not None:
        click.echo(
            "Error: --reference-doc and --reference-doc-content are mutually exclusive",
            err=True,
        )
        raise SystemExit(1)
    inline_ref_temp: str | None = None
    if reference_doc_content is not None:
        import base64
        import tempfile
        try:
            content_str = reference_doc_content
            if content_str.startswith("@"):
                content_str = Path(content_str[1:]).read_text(encoding="utf-8")
            try:
                docx_bytes = base64.b64decode(content_str, validate=True)
            except Exception:  # expected
                docx_bytes = content_str.encode("utf-8")
            parent = Path.cwd().resolve()
            fd, inline_ref_temp = tempfile.mkstemp(
                suffix=".docx", prefix="orf_cli_refdoc_", dir=str(parent),
            )
            os.close(fd)
            with open(inline_ref_temp, "wb") as f:
                f.write(docx_bytes)
        except Exception as e:  # expected
            click.echo(f"Error: failed to write inline reference doc: {e}", err=True)
            if inline_ref_temp and Path(inline_ref_temp).exists():
                Path(inline_ref_temp).unlink()
            raise SystemExit(1)

    effective_template = template or reference_doc or inline_ref_temp

    if target_format == "auto" or auto_detect:
        from orf.detection import FormatDetector

        detector = FormatDetector()
        try:
            detected = detector.detect(input_path)
            format_map = {
                "DOCX": "docx",
                "ODT": "odt",
                "EPUB": "epub",
                "HTML": "html",
                "RTF": "rtf",
                "PDF": "pdf",
            }
            target_format = format_map.get(detected, detected.lower())
            logger.info(f"Auto-detected format: {detected} -> {target_format}")
        except FormatDetectionError as e:
            logger.error(f"Format detection failed: {e}")
            raise click.ClickException(
                f"Format detection failed: {e}\n"
                f"Hint: 1) Ensure manifest.json exists with source.format field\n"
                f"       2) Check the input file is a valid document\n"
                f"       3) Use --target-format <format> to specify manually"
            )

    if output is None:
        output_path = input_path.with_suffix(f".{target_format}")
    else:
        output_path = Path(output)

    if clear_cache:
        n = _clear_orf_cache()
        logger.info(f"Cleared {n} cached file(s) from {_cache_root()}")
        click.echo(f"Cleared {n} cached file(s) from {_cache_root()}")
        return

    logger.info(f"Converting {input_path} -> {output_path} ({target_format})")

    manifest = None
    manifest_path: Optional[Path] = None
    try:
        manifest_path = find_manifest(input_path)
        if manifest_path:
            logger.info(f"Found manifest: {manifest_path}")
            manifest = parse_manifest(manifest_path)
    except ManifestParseError as e:
        logger.warning(f"Failed to parse manifest: {e}")

    cache_key = _cache_key_apply_md(
        input_path, target_format, manifest_path, effective_template, images_json
    )
    if _check_cache(cache_key, output_path, f".{target_format}", no_cache=no_cache):
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

    frontmatter = None
    try:
        frontmatter = parse_frontmatter(input_path)
        logger.info(
            f"Frontmatter: {frontmatter.source_lang} -> {frontmatter.target_lang}"
        )
    except FrontmatterParseError as e:
        logger.warning(f"Failed to parse frontmatter: {e}")

    # Lazy imports from orf.cli for converters so patching works in tests
    from orf.cli import MD2DOCXConverter, MD2ODTConverter, MD2EPUBConverter, MD2HTMLConverter

    if target_format == "docx":
        converter = MD2DOCXConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "odt":
        converter = MD2ODTConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "epub":
        converter = MD2EPUBConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "html":
        from orf.channels.md2html import MD2HTMLConverter

        converter = MD2HTMLConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "rtf":
        from orf.channels.md2rtf import MD2RTFConverter

        converter = MD2RTFConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "pdf":
        from orf.channels.md2pdf import MD2PDFConverter

        converter = MD2PDFConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "csv":
        from orf.channels.md2csv import MD2CSVConverter

        converter = MD2CSVConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "json":
        from orf.channels.md2json import MD2JSONConverter

        converter = MD2JSONConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "xml":
        from orf.channels.md2xml import MD2XMLConverter

        converter = MD2XMLConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "eml":
        from orf.channels.md2eml import MD2EMLConverter

        converter = MD2EMLConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "xlsx":
        try:
            from orf.channels.md2xlsx import MD2XLSXConverter

            converter = MD2XLSXConverter(manifest=manifest, frontmatter=frontmatter)
        except ImportError as e:
            raise click.ClickException(
                f"XLSX conversion requires openpyxl.\n"
                f"Install with: pip install omni-re-formatter[office]\n"
                f"Error: {e}"
            )
    elif target_format == "ipynb":
        try:
            from orf.channels.md2ipynb import MD2IPYNBConverter

            converter = MD2IPYNBConverter(manifest=manifest, frontmatter=frontmatter)
        except ImportError as e:
            raise click.ClickException(
                f"IPYNB conversion requires nbformat.\n"
                f"Install with: pip install omni-re-formatter[notebook]\n"
                f"Error: {e}"
            )
    elif target_format == "msg":
        try:
            from orf.channels.md2msg import MD2MSGConverter

            converter = MD2MSGConverter(manifest=manifest, frontmatter=frontmatter)
        except ImportError as e:
            raise click.ClickException(
                f"MSG conversion requires aspose-email-foss.\n"
                f"Install with: pip install omni-re-formatter[email-output]\n"
                f"Error: {e}"
            )
    elif target_format == "icml":
        from orf.channels.md2icml import MD2ICMLConverter

        converter = MD2ICMLConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "srt":
        from orf.channels.md2srt import MD2SRTConverter

        converter = MD2SRTConverter(manifest=manifest, frontmatter=frontmatter)
    elif target_format == "pptx":
        from orf.channels.md2pptx import MD2PPTXConverter

        converter = MD2PPTXConverter(manifest=manifest, frontmatter=frontmatter)
    else:
        raise click.ClickException(
            f"Unsupported format '{target_format}'\n"
            f"Hint: Valid formats are: docx, odt, epub, html, rtf, pdf, pptx, csv, json, xlsx, xml, ipynb, eml, msg\n"
            f"       Use --target-format <format> to specify"
        )

    options: dict[str, Any] = {}
    if effective_template:
        options["template"] = Path(effective_template)
    if title:
        options["title"] = title
    if author:
        options["author"] = author
    if lang:
        options["lang"] = lang
    if embed_images:
        options["embed_images"] = True
    if css:
        options["css"] = css
    if text_only:
        options["text_only"] = True
    if separate_images is not None:
        options["separate_images"] = separate_images

    # For MD path: pass OPP images.json raw data into converter options so that
    # _extract_images_separately() can decode images, create images.zip + images.json.
    images_raw_data: list[dict] | None = None
    if images_json:
        try:
            import json as _json

            with open(images_json) as f:
                img_data = _json.load(f)
            if isinstance(img_data, dict) and "images" in img_data:
                img_data = img_data["images"]
            if isinstance(img_data, list):
                images_raw_data = img_data
                options["images_data"] = img_data
                logger.info(
                    f"Loaded {len(img_data)} image entries from {images_json} "
                    f"for MD-path image separation"
                )
            else:
                logger.warning(
                    f"images_json does not contain a list of images: {type(img_data)}"
                )
        except Exception as e:
            logger.warning(f"Failed to load images_data from {images_json}: {e}")

    # When images_data is loaded for MD-path image separation, the cache
    # only stores the DOCX (not the sidecar images.zip + images.json), so
    # we must skip the cache to ensure sidecar files are produced.
    if images_raw_data:
        no_cache = True

    # Stage the converter's write to a hidden temp sibling, then atomically
    # rename onto the final path only after a successful conversion. A failed
    # or interrupted run therefore never truncates a previously-good output.
    staged_output = temp_sibling_path(output_path)
    with click.progressbar(  # type: ignore[var-annotated]
        length=1,
        label=f"Converting to {target_format}",
        show_pos=True,
        show_percent=True,
        file=sys.stderr if output_json else None,
    ):
        result = converter.convert(input_path, staged_output, ConverterOptions(**options))

    result.output_path = output_path

    if not result.success:
        discard(staged_output)
    elif staged_output.exists():
        try:
            publish(staged_output, output_path)
        except OSError as e:
            discard(staged_output)
            raise click.ClickException(f"Failed to finalize output {output_path}: {e}")

        # Post-conversion image injection (XLIFF path only — MD2DOCX.inject_images is a no-op)
        if images_raw_data and target_format == "docx":
            try:
                from orf.mcp.schemas import ImagePlacement

                placements = [
                    ImagePlacement(**img)
                    for img in images_raw_data
                    if "paragraph_index" in img
                ]
                if placements:
                    injected, orphaned = converter.inject_images(
                        result.output_path, placements, result.output_path
                    )
                    logger.info(
                        f"Post-conversion: injected {len(injected)} images, "
                        f"{len(orphaned)} orphaned"
                    )
            except Exception as e:
                logger.debug(f"Post-conversion image injection skipped or failed: {e}")
    else:
        logger.debug("Converter reported success without writing %s", staged_output)

    if result.success:
        _write_cache(cache_key, output_path, f".{target_format}", no_cache=no_cache)
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
                f"Hint: 1) Check Pandoc is installed (pip install pandoc)\n"
                f"       2) Verify input file is valid\n"
                f"       3) Try --verbose for detailed logs"
            )
