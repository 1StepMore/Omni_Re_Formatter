"""ORF CLI entry point for format conversion."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

import click

# Converter imports kept here for backward compat (tests patch orf.cli.MD2DOCXConverter etc.)
# Command modules import from orf.channels.* directly and use lazy imports.
from orf.channels.md2docx import MD2DOCXConverter  # noqa: F401
from orf.channels.md2odt import MD2ODTConverter  # noqa: F401
from orf.channels.md2epub import MD2EPUBConverter  # noqa: F401
from orf.channels.md2html import MD2HTMLConverter  # noqa: F401
from orf.converters.base import BaseConverter  # noqa: F401
from orf.converters.options import ConverterOptions  # noqa: F401
from orf.parsers.manifest import parse_manifest, find_manifest, ManifestParseError  # noqa: F401
from orf.parsers.frontmatter import (  # noqa: F401
    parse_frontmatter,
    FrontmatterParseError,
    has_frontmatter,
)
from orf.error_handlers.conversion_error import FormatDetectionError  # noqa: F401
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


# Set by _maybe_install_fake_pandoc() once the stub is actually installed.
# Deliberately NOT read from the env var: test jobs export
# OMNI_TEST_FAKE_PANDOC=1 process-wide (e2e contract-tests.yml) while calling
# cache helpers directly, never going through main(). Keying the cache off the
# env var alone disabled caching for those tests too (issue #429).
_FAKE_PANDOC_ACTIVE = False


def _cache_enabled() -> bool:
    """Fake-pandoc runs must never read or write the shared cache (issue #429).

    The fake seam replaces pandoc with a fixed-template stub, so its output
    carries no relation to the input. Caching it poisons real runs that later
    hit the same content key. Only the *installed* stub disables the cache.
    """
    return not _FAKE_PANDOC_ACTIVE


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
    if not _cache_enabled():
        return False
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
    if not _cache_enabled():
        return
    if no_cache:
        return
    if not output_path.exists():
        return
    cache_file = _cache_root() / f"{cache_key}{ext}"
    cache_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
    try:
        shutil.copy(output_path, tmp_file)
        os.replace(tmp_file, cache_file)  # atomic replace: no half-written file on interrupt
    finally:
        if tmp_file.exists():
            tmp_file.unlink(missing_ok=True)
    _cache_logger.debug(f"Cache miss: wrote {cache_file}")


def _ensure_output_dir(path: Path) -> None:
    """Create ``path`` as a directory with parents, idempotently (#102).

    A missing output directory used to surface as an unrelated converter
    error (e.g. "Failed to repack DOCX: [Errno 2] No such file or
    directory: '<out>'"), sending agents chasing the XLIFF or the input
    document instead of the directory. Callers pass the *parent* of a
    file output (apply-md / apply-xliff) or the output directory itself
    (convert-batch). On failure the error names the directory path and
    the underlying reason.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise click.ClickException(
            f"Failed to create output directory {path}: {e}"
        )


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
@click.option("--load-dotenv", is_flag=True, default=False, help="Load .env file before running (opt-in)")
@click.pass_context
def main(ctx: click.Context, verbose: bool, log_format: str | None = None, load_dotenv: bool = False) -> None:
    """ORF - Omni-Re-Formatter: 将本地化后的 MD/XLIFF 还原为目标复杂格式。"""
    if load_dotenv or os.environ.get("ORF_AUTOLOAD_DOTENV") == "1":
        _load_env_for_orf()
    log_level = "DEBUG" if verbose else "INFO"
    if log_format:
        os.environ["OMNI_LOG_FORMAT"] = log_format
    setup_logger(level=log_level)
    _maybe_install_fake_pandoc()
    # The cache gate is process-global state, so it must not outlive this
    # invocation: a test suite that invokes the CLI once and then exercises
    # the cache helpers directly (test_orf_cache.py) would otherwise inherit
    # FAKE_PANDOC_ACTIVE and see every cache write silently skipped.
    ctx.call_on_close(_reset_fake_pandoc_active)


def _reset_fake_pandoc_active() -> None:
    """Clear the cache gate once the CLI invocation ends (see main())."""
    global _FAKE_PANDOC_ACTIVE
    _FAKE_PANDOC_ACTIVE = False


def _maybe_install_fake_pandoc() -> None:
    """Monkey-patch subprocess.run for OMNI_TEST_FAKE_PANDOC=1."""
    if os.environ.get("OMNI_TEST_FAKE_PANDOC") != "1":
        return
    import subprocess as _subprocess
    from pathlib import Path as _SeamPath

    _suite_root = _SeamPath(__file__).resolve().parents[3]
    if str(_suite_root) not in sys.path:
        sys.path.insert(0, str(_suite_root))
    try:
        from tests.test_e2e_pipeline_fixtures import _FakePandocRunner
    except ImportError:
        return

    global _FAKE_PANDOC_ACTIVE
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
    _FAKE_PANDOC_ACTIVE = True


def _load_dotenv_for_orf(env_path: Path) -> None:
    """Parse and export .env file without blocking on missing keys."""
    try:
        content = env_path.read_text()
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            # Strip optional 'export' prefix (common shell convention)
            line = line.removeprefix("export ").lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)
    except Exception as exc:
        logger.warning("Failed to load .env file %s: %s", env_path, exc)


def _load_env_for_orf() -> None:
    """Load .env file for CLI commands.

    Search order:
      1. $ORF_DOTENV env var (explicit override)
      2. ./.env (current working directory)
      3. Walk up parent directories looking for .env
      4. ~/.config/orf/.env (user-level fallback)

    If no .env is found, the function returns silently.
    """
    from pathlib import Path as _Path

    search_paths: list[_Path] = []
    explicit = os.environ.get("ORF_DOTENV")
    if explicit:
        search_paths.append(_Path(explicit))
    search_paths.append(_Path.cwd() / ".env")
    for parent in _Path.cwd().resolve().parents:
        candidate = parent / ".env"
        if candidate not in search_paths:
            search_paths.append(candidate)
    search_paths.append(_Path.home() / ".config" / "orf" / ".env")

    for env_path in search_paths:
        if env_path.exists() and env_path.is_file():
            _load_dotenv_for_orf(env_path)
            return


# ========== Register commands ==========

from orf.commands.apply_md import apply_md  # noqa: E402
from orf.commands.apply_xliff import apply_xliff  # noqa: E402
from orf.commands.convert_batch import convert_batch  # noqa: E402
from orf.commands.info import info  # noqa: E402
from orf.commands.capabilities import capabilities  # noqa: E402
from orf.commands.detect_format import detect_format  # noqa: E402

main.add_command(apply_md)
main.add_command(apply_xliff)
main.add_command(convert_batch)
main.add_command(capabilities)
main.add_command(detect_format)
main.add_command(info)

if __name__ == "__main__":
    main()
