"""ORF CLI entry point for format conversion."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import click

from orf.channels.md2docx import MD2DOCXConverter
from orf.channels.md2odt import MD2ODTConverter
from orf.channels.md2epub import MD2EPUBConverter
from orf.channels.md2html import MD2HTMLConverter
from orf.converters.base import BaseConverter
from orf.converters.options import ConverterOptions
from orf.parsers.manifest import parse_manifest, find_manifest, ManifestParseError
from orf.parsers.frontmatter import (
    parse_frontmatter,
    FrontmatterParseError,
    has_frontmatter,
)
from orf.error_handlers.conversion_error import FormatDetectionError
from orf.logging import setup_logger, get_logger

logger = get_logger("cli")


# ========== A6: Content-addressed cache (~/.omni_cache/orf/) ==========
# Re-runs of the same input+config skip the expensive conversion (pandoc,
# openpyxl, etc.) and just copy the cached <sha256>.<ext> to the output
# path. The cache root can be overridden with the OMNI_CACHE_DIR env var
# (used by tests). Mode 0o700 protects any sensitive content.
# CACHE_DIR_NAME is the per-module subdirectory under OMNI_CACHE_DIR.
CACHE_DIR_NAME = "orf"
_cache_logger = get_logger("cli.cache")


def _cache_root() -> Path:
    """Return the ORF cache root, creating it (mode 0o700) on first access.

    The env var is read at call-time (not at import-time) so tests can
    override it via monkeypatch.setenv() before any call.
    """
    root = Path(
        os.environ.get("OMNI_CACHE_DIR", str(Path.home() / ".omni_cache"))
    ) / CACHE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _hash_file(path: Path) -> str:
    """SHA-256 hex digest of a file, read in 8192-byte chunks (avoids loading the entire file into memory).

    This is used by the cache key functions so that large input files (e.g.
    14 MB+) are not fully read into memory just to check cache membership.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_key_apply_md(
    input_path: Path,
    target_format: str,
    manifest_path: Optional[Path],
    template: Optional[str],
    images_json: Optional[str],
) -> str:
    """sha256(input_bytes + target_format + manifest_bytes + template_bytes + images_json_bytes).

    Each "config" element that can change the conversion output is hashed
    in. Any change to input, target_format, manifest content, template
    content, or images.json content yields a different cache key and
    forces a fresh conversion.
    """
    h = hashlib.sha256()
    h.update(_hash_file(input_path).encode("utf-8"))
    h.update(target_format.encode("utf-8"))
    if manifest_path is not None and manifest_path.exists():
        h.update(_hash_file(manifest_path).encode("utf-8"))
    if template:
        tp = Path(template)
        if tp.exists():
            h.update(_hash_file(tp).encode("utf-8"))
    if images_json:
        ip = Path(images_json)
        if ip.exists():
            h.update(_hash_file(ip).encode("utf-8"))
    return h.hexdigest()


def _cache_key_apply_xliff(
    input_path: Path,
    xliff_path: Path,
    fmt: str,
    images_json: Optional[str],
) -> str:
    """sha256(input_bytes + xliff_bytes + format + images_json_bytes)."""
    h = hashlib.sha256()
    h.update(_hash_file(input_path).encode("utf-8"))
    h.update(_hash_file(xliff_path).encode("utf-8"))
    h.update(fmt.encode("utf-8"))
    if images_json:
        ip = Path(images_json)
        if ip.exists():
            h.update(_hash_file(ip).encode("utf-8"))
    return h.hexdigest()


def _check_cache(cache_key: str, output_path: Path, ext: str, no_cache: bool = False) -> bool:
    """If cached, copy to ``output_path`` and return True. Honors ``--no-cache``."""
    if no_cache:
        return False
    cache_file = _cache_root() / f"{cache_key}{ext}"
    if cache_file.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(cache_file, output_path)
        _cache_logger.info(f"Cache hit: {cache_file} -> {output_path}")
        return True
    return False


def _write_cache(cache_key: str, output_path: Path, ext: str, no_cache: bool = False) -> None:
    """Copy ``output_path`` into the cache for next run. Honors ``--no-cache``."""
    if no_cache:
        return
    if not output_path.exists():
        return
    cache_file = _cache_root() / f"{cache_key}{ext}"
    cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy(output_path, cache_file)
    _cache_logger.debug(f"Cache miss: wrote {cache_file}")


def _clear_orf_cache() -> int:
    """Remove all cached ORF files. Returns the number of files removed."""
    root = _cache_root()
    if not root.exists():
        return 0
    count = sum(1 for _ in root.iterdir())
    shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return count


def _error_item_to_dict(e: Any) -> dict[str, Any]:
    """Normalize error items (ErrorDetail or str) to dict for JSON output."""
    if hasattr(e, 'code'):
        return {
            'code': e.code,
            'message': e.message,
            'recovery_strategy': e.recovery_strategy.value if e.recovery_strategy else None,
        }
    return {'code': 'UNKNOWN', 'message': str(e), 'recovery_strategy': None}


def _error_item_to_str(e: Any) -> str:
    """Normalize error items (ErrorDetail or str) to string."""
    return e.message if hasattr(e, 'message') else str(e)


def _warning_item_to_dict(w: Any) -> dict[str, str]:
    """Normalize warning items (WarningDetail or str) to dict for JSON output."""
    if hasattr(w, 'code'):
        return {'code': w.code, 'message': w.message}
    return {'code': 'UNKNOWN', 'message': str(w)}


def _safe_json_dumps(data: dict) -> str:
    """Serialize data to JSON, filtering non-serializable metadata values."""
    import json
    cleaned = dict(data)
    if 'metadata' in cleaned:
        cleaned['metadata'] = _sanitize_for_json(cleaned['metadata'])
    return json.dumps(cleaned, indent=2, default=str)


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively sanitize objects for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(type(obj).__name__)


@click.group()
@click.version_option(package_name="omni-re-formatter", prog_name="orf")
@click.option("--verbose", "-v", is_flag=True, help="启用详细日志")
@click.option(
    "--log-format",
    type=click.Choice(["console", "json"]),
    default=None,
    envvar="OMNI_LOG_FORMAT",
    help="日志输出格式: 'console' (默认) 或 'json'。也可通过 OMNI_LOG_FORMAT 环境变量设置。",
)
def main(verbose: bool, log_format: str | None = None) -> None:
    """ORF - Omni-Re-Formatter: 将本地化后的 MD/XLIFF 还原为目标复杂格式。"""
    log_level = "DEBUG" if verbose else "INFO"
    if log_format:
        os.environ["OMNI_LOG_FORMAT"] = log_format
    setup_logger(level=log_level)
    _maybe_install_fake_pandoc()


def _maybe_install_fake_pandoc() -> None:
    import os
    if os.environ.get("OMNI_TEST_FAKE_PANDOC") != "1":
        return
    import subprocess as _subprocess
    import sys
    from pathlib import Path as _SeamPath
    _suite_root = _SeamPath(__file__).resolve().parents[3]
    if str(_suite_root) not in sys.path:
        sys.path.insert(0, str(_suite_root))
    from tests.test_e2e_pipeline_fixtures import _FakePandocRunner
    _fake_runner = _FakePandocRunner()
    _original_run = _subprocess.run
    def _patched_run(*args, **kwargs):
        try:
            cmd = args[0] if args else kwargs.get("args") or kwargs.get("cmd")
        except (IndexError, KeyError, TypeError):
            logger.exception("Failed to extract command from patched subprocess call")
            cmd = None
        if cmd and isinstance(cmd, (list, tuple)) and len(cmd) > 0 and "pandoc" in str(cmd[0]):
            return _fake_runner(*args, **kwargs)
        return _original_run(*args, **kwargs)
    _subprocess.run = _patched_run


@main.command("apply-md")
@click.argument("input_md", type=click.Path(exists=True))
@click.option(
    "--target-format",
    "-t",
    # 2026-06-17 round 10: pptx removed (md2pptx binary not installed).
    # 2026-06-18 round 13: re-added after MartinPacker/md2pptx installed at
    # /home/renanzai/.local/bin/md2pptx. See .omo/plans/2026-06-17-fix-plan-round-13.md.
    type=click.Choice(["auto", "docx", "odt", "epub", "html", "rtf", "pdf", "pptx", "csv", "json", "xlsx", "xml", "ipynb", "eml", "msg", "icml", "srt"]),
    default="docx",
    help="目标格式",
)
@click.option("--auto-detect", is_flag=True, help="自动检测输入文件格式")
@click.option("--output", "-o", type=click.Path(), help="输出文件路径")
@click.option("--template", type=click.Path(), help="Pandoc reference 模板路径")
@click.option("--reference-doc", type=click.Path(exists=True), help="Reference DOCX for document styles (maps to pandoc --reference-doc)")
@click.option("--title", type=str, help="EPUB 标题")
@click.option("--author", type=str, help="EPUB 作者")
@click.option("--lang", type=str, default="zh", help="EPUB 语言")
@click.option("--embed-images", is_flag=True, help="EPUB 嵌入图片")
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
@click.option("--text-only", "text_only", is_flag=True, help="产生纯文本输出，跳过所有图片引用和占位符")
@click.option("--separate-images/--no-separate-images", "separate_images", default=None, help="将图片从 DOCX 中分离为独立 assets（默认开启）")
@click.option("--images-json", "images_json", type=click.Path(exists=True), help="JSON file with image placement data from OPP (NOTE: MD path extracts images for organized output, not injection)")
@click.option("--no-cache", "no_cache", is_flag=True, help="Skip the .omni_cache/ cache check (force a fresh conversion)")
@click.option("--clear-cache", "clear_cache", is_flag=True, help="Remove all cached ORF outputs and exit")
@click.option("--max-file-size-mb", type=float, default=None, help="拒绝超过此大小 (MB) 的文件")
def apply_md(
    input_md: str,
    target_format: str,
    auto_detect: bool,
    output: str | None,
    template: str | None,
    reference_doc: str | None,
    title: str | None,
    author: str | None,
    lang: str,
    embed_images: bool,
    output_json: bool,
    text_only: bool,
    separate_images: bool | None,
    images_json: str | None,
    no_cache: bool,
    clear_cache: bool,
    max_file_size_mb: float | None = None,
) -> None:
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
            click.echo(f"Error: File {input_path} is {size_mb:.1f}MB, exceeds limit of {max_file_size_mb}MB", err=True)
            raise SystemExit(1)

    effective_template = template or reference_doc

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

    cache_key = _cache_key_apply_md(input_path, target_format, manifest_path, effective_template, images_json)
    if _check_cache(cache_key, output_path, f".{target_format}", no_cache=no_cache):
        logger.info(f"Cache hit for {input_path.name} -> {output_path}")
        if output_json:
            click.echo(_safe_json_dumps({
                'success': True,
                'output_path': str(output_path),
                'errors': [],
                'warnings': [],
                'metadata': {'cache_hit': True},
            }))
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

    if target_format == "docx":
        converter: BaseConverter = MD2DOCXConverter(manifest=manifest, frontmatter=frontmatter)
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

    with click.progressbar(  # type: ignore[var-annotated]
        length=1,
        label=f"Converting to {target_format}",
        show_pos=True,
        show_percent=True,
        file=sys.stderr if output_json else None,
    ):
        result = converter.convert(input_path, output_path, ConverterOptions(**options))

    # Post-conversion image injection (XLIFF path only — MD2DOCX.inject_images is a no-op)
    if result.success and images_raw_data and target_format == "docx":
        try:
            from orf.mcp.schemas import ImagePlacement
            placements = [ImagePlacement(**img) for img in images_raw_data if "paragraph_index" in img]
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

    if result.success:
        _write_cache(cache_key, output_path, f".{target_format}", no_cache=no_cache)
        logger.info(f"Conversion successful: {result.output_path}")
        if output_json:
            click.echo(_safe_json_dumps({
                'success': True,
                'output_path': str(result.output_path),
                'errors': [_error_item_to_dict(e) for e in result.errors],
                'warnings': [_warning_item_to_dict(w) for w in result.warnings],
                'metadata': result.metadata
            }))
        else:
            click.echo(f"Created {result.output_path}")
    else:
        logger.error(f"Conversion failed: {result.errors}")
        errors_str = ", ".join(_error_item_to_str(e) for e in result.errors) if result.errors else "Unknown error"
        if output_json:
            click.echo(_safe_json_dumps({
                'success': False,
                'output_path': str(result.output_path) if result.output_path else None,
                'errors': [_error_item_to_dict(e) for e in result.errors],
                'warnings': [_warning_item_to_dict(w) for w in result.warnings],
                'metadata': result.metadata
            }))
        else:
            raise click.ClickException(
                f"Conversion failed: {errors_str}\n"
                f"Hint: 1) Check Pandoc is installed (pip install pandoc)\n"
                f"       2) Verify input file is valid\n"
                f"       3) Try --verbose for detailed logs"
            )


# ========== Batch mode: single-file helper ==========


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
        return False, str(e)


@main.command("convert-batch")
@click.argument("input_dir", type=click.Path(exists=True))
@click.option("--target-format", "-t", type=click.Choice(["docx", "odt", "epub", "html"]))
@click.option("--output-dir", "-o", type=click.Path(), help="输出目录")
@click.option("--pattern", "-p", default="*.md", help="文件匹配模式")
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
@click.option("--max-file-size-mb", type=float, default=None, help="拒绝超过此大小 (MB) 的文件")
@click.option("--max-workers", type=int, default=4, help="最大并行工作线程数")
def convert_batch(
    input_dir: str,
    target_format: str,
    output_dir: str | None,
    pattern: str,
    output_json: bool,
    max_file_size_mb: float | None = None,
    max_workers: int = 4,
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
        click.echo("No files found to convert")
        return

    if max_file_size_mb is not None:
        for md_file in md_files:
            size_mb = md_file.stat().st_size / (1024 * 1024)
            if size_mb > max_file_size_mb:
                click.echo(f"Error: File {md_file} is {size_mb:.1f}MB, exceeds limit of {max_file_size_mb}MB", err=True)
                raise SystemExit(1)

    if target_format == "docx":
        converter_class: type = MD2DOCXConverter
    elif target_format == "odt":
        converter_class = MD2ODTConverter
    elif target_format == "epub":
        converter_class = MD2EPUBConverter
    elif target_format == "html":
        converter_class = MD2HTMLConverter
    else:
        raise click.ClickException(
            f"Unsupported format '{target_format}'\n"
            f"Hint: Valid formats are: docx, odt, epub, html\n"
            f"       Use --target-format <format> to specify"
        )

    success_count = 0
    fail_count = 0

    if max_workers <= 1:
        with click.progressbar(md_files, label=f"Converting to {target_format}", show_pos=True, show_percent=True) as bar:
            for md_file in bar:
                success, error = _convert_single(md_file, converter_class, output_path, target_format)
                if success:
                    success_count += 1
                else:
                    fail_count += 1
                    logger.error(f"Failed: {md_file}: {error}")
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_convert_single, md_file, converter_class, output_path, target_format)
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

    click.echo(f"\nCompleted: {success_count} succeeded, {fail_count} failed")

    if output_json:
        click.echo(json.dumps({
            'success_count': success_count,
            'fail_count': fail_count,
            'total': len(md_files)
        }, indent=2))


@main.command("apply-xliff")
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--xliff", "-x", required=True, help="Translated XLIFF file")
@click.option("--xliff-content", "xliff_content", type=str, help="Inline XLIFF content (alternative to --xliff)")
@click.option("--output", "-o", required=True, help="Output file path")
@click.option(
    "--format",
    "-f",
    type=click.Choice(["docx", "pptx", "epub", "html", "odt", "pdf"]),
    default="docx",
    help="Output format",
)
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
@click.option("--images-json", "images_json", type=click.Path(exists=True), help="JSON file with image placement data from OPP")
@click.option("--no-cache", "no_cache", is_flag=True, help="Skip the .omni_cache/ cache check (force a fresh conversion)")
@click.option("--clear-cache", "clear_cache", is_flag=True, help="Remove all cached ORF outputs and exit")
@click.option("--max-file-size-mb", type=float, default=None, help="拒绝超过此大小 (MB) 的文件")
@click.option("--force", is_flag=True, default=False, help="Bypass skeleton format validation (may produce broken output). Use with caution.")
def apply_xliff(input_file: str, xliff: str, xliff_content: Optional[str], output: str, format: str, output_json: bool, images_json: str, no_cache: bool, clear_cache: bool, max_file_size_mb: float | None = None, force: bool = False) -> None:
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
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xliff', delete=False) as tmp:
            tmp.write(xliff_content)
            xliff_path = Path(tmp.name)

    if max_file_size_mb is not None:
        for check_path in [input_path, xliff_path]:
            size_mb = check_path.stat().st_size / (1024 * 1024)
            if size_mb > max_file_size_mb:
                click.echo(f"Error: File {check_path} is {size_mb:.1f}MB, exceeds limit of {max_file_size_mb}MB", err=True)
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
            click.echo(_safe_json_dumps({
                'success': True,
                'output_path': str(output_path),
                'errors': [],
                'warnings': [],
                'metadata': {'cache_hit': True},
            }))
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

    logger.info(f"Applying XLIFF {xliff_path} to {input_path} -> {output_path} ({format})")

    # 2026-06-17 round 5 (FIX-#8): XLIFF backfill is format-preserving —
    # fail early on a mismatched skeleton rather than letting
    # translate-toolkit crash with an abstract error.
    # Round 9: also accept .zip (OPP's skeleton.zip packaging).
    _FORMAT_EXT = {"docx": ".docx", "pptx": ".pptx", "epub": ".epub", "html": ".html", "odt": ".odt"}
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
    else:
        raise click.ClickException(
            f"Unsupported format '{format}'\n"
            f"Hint: Valid formats are: docx, pptx, epub, html, odt\n"
            f"       Use --format <format> to specify"
        )

    result = converter.convert(input_path, xliff_path, output_path)

    if result.success and images:
        import tempfile
        import shutil
        try:
            tmp_skeleton = tempfile.NamedTemporaryFile(suffix='.docx', delete=False)
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
            click.echo(_safe_json_dumps({
                'success': True,
                'output_path': str(result.output_path),
                'errors': [_error_item_to_dict(e) for e in result.errors],
                'warnings': [_warning_item_to_dict(w) for w in result.warnings],
                'metadata': result.metadata
            }))
        else:
            click.echo(f"Created {result.output_path}")
    else:
        logger.error(f"Conversion failed: {result.errors}")
        errors_str = ", ".join(_error_item_to_str(e) for e in result.errors) if result.errors else "Unknown error"
        if output_json:
            click.echo(_safe_json_dumps({
                'success': False,
                'output_path': str(result.output_path) if result.output_path else None,
                'errors': [_error_item_to_dict(e) for e in result.errors],
                'warnings': [_warning_item_to_dict(w) for w in result.warnings],
                'metadata': result.metadata
            }))
        else:
            raise click.ClickException(
                f"Conversion failed: {errors_str}\n"
                f"Hint: 1) Check XLIFF file is valid\n"
                f"       2) Verify original document exists\n"
                f"       3) Try --verbose for detailed logs"
            )


@main.command("info")
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--json", "output_json", is_flag=True, help="JSON 格式输出")
def info(input_file: str, output_json: bool) -> None:
    """Show information about a document file.

    INPUT_FILE: Document file to inspect (DOCX, ODT, EPUB, etc.)
    """
    from orf.detection import FormatDetector

    input_path = Path(input_file)

    # Detect format
    detector = FormatDetector()
    try:
        detected_format = detector.detect(input_path)
    except FormatDetectionError as e:
        raise click.ClickException(f"Format detection failed: {e}")

    # Get file size
    file_size = input_path.stat().st_size
    size_mb = file_size / (1024 * 1024)

    # Check manifest presence and get resource count
    manifest_path = find_manifest(input_path)
    manifest_status = "present" if manifest_path else "not found"

    resource_count = None
    if manifest_path:
        try:
            manifest = parse_manifest(manifest_path)
            if manifest.images:
                resource_count = len(manifest.images)
        except Exception as e:
            logger.warning(f"Failed to parse manifest for resource count: {e}")

    # Output
    if output_json:
        click.echo(json.dumps({
            'format': detected_format,
            'size_mb': round(size_mb, 2),
            'resource_count': resource_count,
            'manifest_status': manifest_status
        }, indent=2))
    else:
        click.echo(f"Format: {detected_format}")
        click.echo(f"Size: {size_mb:.2f} MB")
        if resource_count is not None:
            click.echo(f"Resources: {resource_count} images")
        click.echo(f"Manifest: {manifest_status}")


if __name__ == "__main__":
    main()
