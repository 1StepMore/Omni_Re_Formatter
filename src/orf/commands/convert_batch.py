"""convert-batch command: Batch convert MD files in a directory.

Explicit partial-success contract (plan R-04): the ``--json`` summary
always carries an overall ``status`` so an agent never has to re-derive
success from counts:

* ``empty``    — no files matched the pattern (exit 0)
* ``complete`` — every file converted (exit 0)
* ``partial``  — some files succeeded, some failed (exit non-zero unless
  ``--allow-partial``)
* ``failed``   — every file failed (exit ALWAYS non-zero; ``--allow-partial``
  only downgrades *partial*, never masks a total failure)

Counts: ``succeeded`` / ``failed`` / ``retryable`` / ``total``.
``retryable`` is the number of failed items a retry could plausibly clear;
every batch failure is an operational conversion failure (no permanent
classifier exists), so today ``retryable == failed``. ``success_count`` /
``fail_count`` are kept as backward-compatible aliases of ``succeeded`` /
``failed`` (T-03 schema).
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click

from orf.parsers.manifest import parse_manifest, find_manifest
from orf.parsers.frontmatter import parse_frontmatter, has_frontmatter
from orf.logging import get_logger

logger = get_logger("cli")


def _convert_single(
    md_file: Path,
    converter_class: type,
    output_path: Path,
    target_format: str,
) -> tuple[bool, str | None]:
    """Convert a single MD file and return (success, error_message_or_none).

    This is extracted as a module-level function so it can be submitted
    to a ThreadPoolExecutor for parallel batch processing.
    """
    try:
        manifest = None
        try:
            manifest_path = find_manifest(md_file)
            if manifest_path:
                manifest = parse_manifest(manifest_path)
        except Exception as exc:
            logger.warning("Failed to parse manifest for %s: %s", md_file, exc)

        frontmatter = None
        try:
            frontmatter = parse_frontmatter(md_file)
        except Exception as exc:
            logger.warning("Failed to parse frontmatter for %s: %s", md_file, exc)

        converter = converter_class(manifest=manifest, frontmatter=frontmatter)
        output_file = output_path / f"{md_file.stem}.{target_format}"

        result = converter.convert(md_file, output_file)

        if result.success:
            return True, None
        else:
            return False, str(result.errors)

    except Exception as e:
        logger.warning("Batch conversion failed for %s: %s", md_file, e, exc_info=True)
        return False, str(e)


@click.command("convert-batch")
@click.argument("input_dir", type=click.Path(exists=True))
@click.option(
    "--target-format",
    "-t",
    type=click.Choice([
        "docx", "odt", "epub", "html", "rtf", "pdf", "pptx",
        "icml", "srt", "csv", "xlsx", "json", "ipynb", "eml",
        "msg", "xml",
    ]),
)
@click.option("--output-dir", "-o", type=click.Path(), help="输出目录")
@click.option("--pattern", "-p", default="*.md", help="文件匹配模式")
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
@click.option(
    "--allow-partial",
    is_flag=True,
    help="Exit 0 on partial success (some files failed but at least one succeeded). "
    "A fully-failed batch still exits non-zero.",
)
@click.option(
    "--max-file-size-mb",
    type=float,
    default=None,
    help="拒绝超过此大小 (MB) 的文件",
)
@click.option(
    "--max-workers",
    type=int,
    default=4,
    help="最大并行工作线程数",
)
def convert_batch(
    input_dir: str,
    target_format: str,
    output_dir: str | None,
    pattern: str,
    output_json: bool,
    max_file_size_mb: float | None = None,
    max_workers: int = 4,
    allow_partial: bool = False,
) -> None:
    """批量转换 MD 文件"""
    input_path = Path(input_dir)
    output_path = Path(output_dir) if output_dir else input_path

    md_files = []
    for md_file in input_path.rglob(pattern):
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            if has_frontmatter(content):
                md_files.append(md_file)
        except Exception as e:
            logger.warning(f"Skipping {md_file}: {e}")

    logger.info(f"Found {len(md_files)} MD files to convert")

    if not md_files:
        if output_json:
            click.echo("No files found to convert", err=True)
            click.echo(
                json.dumps(
                    {
                        "success": True,
                        "status": "empty",
                        "succeeded": 0,
                        "failed": 0,
                        "retryable": 0,
                        "total": 0,
                        "success_count": 0,
                        "fail_count": 0,
                    },
                    indent=2,
                )
            )
        else:
            click.echo("No files found to convert")
        return

    if max_file_size_mb is not None:
        for md_file in md_files:
            size_mb = md_file.stat().st_size / (1024 * 1024)
            if size_mb > max_file_size_mb:
                click.echo(
                    f"Error: File {md_file} is {size_mb:.1f}MB, exceeds limit of {max_file_size_mb}MB",
                    err=True,
                )
                raise SystemExit(1)

    # Lazy imports from orf.channels for test patch compat
    from orf.cli import MD2DOCXConverter, MD2ODTConverter, MD2EPUBConverter, MD2HTMLConverter

    if target_format == "docx":
        converter_class: type = MD2DOCXConverter
    elif target_format == "odt":
        converter_class = MD2ODTConverter
    elif target_format == "epub":
        converter_class = MD2EPUBConverter
    elif target_format == "html":
        converter_class = MD2HTMLConverter
    elif target_format == "rtf":
        from orf.channels.md2rtf import MD2RTFConverter
        converter_class = MD2RTFConverter
    elif target_format == "pdf":
        from orf.channels.md2pdf import MD2PDFConverter
        converter_class = MD2PDFConverter
    elif target_format == "pptx":
        from orf.channels.md2pptx import MD2PPTXConverter
        converter_class = MD2PPTXConverter
    elif target_format == "icml":
        from orf.channels.md2icml import MD2ICMLConverter
        converter_class = MD2ICMLConverter
    elif target_format == "srt":
        from orf.channels.md2srt import MD2SRTConverter
        converter_class = MD2SRTConverter
    elif target_format == "csv":
        from orf.channels.md2csv import MD2CSVConverter
        converter_class = MD2CSVConverter
    elif target_format == "xlsx":
        try:
            from orf.channels.md2xlsx import MD2XLSXConverter
            converter_class = MD2XLSXConverter
        except ImportError as e:
            raise click.ClickException(
                f"XLSX conversion requires openpyxl.\n"
                f"Install with: pip install omni-re-formatter[office]\n"
                f"Error: {e}"
            )
    elif target_format == "json":
        from orf.channels.md2json import MD2JSONConverter
        converter_class = MD2JSONConverter
    elif target_format == "ipynb":
        try:
            from orf.channels.md2ipynb import MD2IPYNBConverter
            converter_class = MD2IPYNBConverter
        except ImportError as e:
            raise click.ClickException(
                f"IPYNB conversion requires nbformat.\n"
                f"Install with: pip install omni-re-formatter[notebook]\n"
                f"Error: {e}"
            )
    elif target_format == "eml":
        from orf.channels.md2eml import MD2EMLConverter
        converter_class = MD2EMLConverter
    elif target_format == "msg":
        try:
            from orf.channels.md2msg import MD2MSGConverter
            converter_class = MD2MSGConverter
        except ImportError as e:
            raise click.ClickException(
                f"MSG conversion requires aspose-email-foss.\n"
                f"Install with: pip install omni-re-formatter[email-output]\n"
                f"Error: {e}"
            )
    elif target_format == "xml":
        from orf.channels.md2xml import MD2XMLConverter
        converter_class = MD2XMLConverter
    else:
        raise click.ClickException(
            f"Unsupported format '{target_format}'\n"
            f"Hint: Valid formats are: docx, odt, epub, html, rtf, pdf, pptx, icml, srt, csv, xlsx, json, ipynb, eml, msg, xml\n"
            f"       Use --target-format <format> to specify"
        )

    success_count = 0
    fail_count = 0

    if max_workers <= 1:
        with click.progressbar(
            md_files,
            label=f"Converting to {target_format}",
            show_pos=True,
            show_percent=True,
            file=sys.stderr if output_json else None,
        ) as bar:
            for md_file in bar:
                success, error = _convert_single(
                    md_file, converter_class, output_path, target_format
                )
                if success:
                    success_count += 1
                else:
                    fail_count += 1
                    logger.error(f"Failed: {md_file}: {error}")
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    _convert_single,
                    md_file,
                    converter_class,
                    output_path,
                    target_format,
                )
                for md_file in md_files
            ]
            for i, future in enumerate(futures):
                md_file = md_files[i]
                try:
                    success, error = future.result()
                    if success:
                        success_count += 1
                    else:
                        fail_count += 1
                        logger.error(f"Failed: {md_file}: {error}")
                except Exception as e:
                    fail_count += 1
                    logger.error(f"Error processing {md_file}: {e}")

    total = len(md_files)
    if fail_count == 0:
        status = "complete"
    elif success_count > 0:
        status = "partial"
    else:
        status = "failed"

    retryable = fail_count

    # T-03: human summary -> stderr under --json (stdout = one JSON object).
    click.echo(
        f"\nCompleted: {success_count} succeeded, {fail_count} failed",
        err=output_json,
    )

    if output_json:
        click.echo(
            json.dumps(
                {
                    "success": fail_count == 0 or (allow_partial and status == "partial"),
                    "status": status,
                    "succeeded": success_count,
                    "failed": fail_count,
                    "retryable": retryable,
                    "total": total,
                    "success_count": success_count,
                    "fail_count": fail_count,
                },
                indent=2,
            )
        )

    # R-04 exit rule: partial exits non-zero unless --allow-partial; failed
    # always exits non-zero (the flag must not mask a total failure).
    if fail_count > 0 and not (allow_partial and status == "partial"):
        raise SystemExit(1)
