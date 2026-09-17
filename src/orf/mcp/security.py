"""Path validation with directory allowlist and file size limits."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# System directories that should never be accessed
SYSTEM_DIRS: set = {
    '/etc',
    '/usr',
    '/var',
    '/proc',
    '/sys',
    '/System',
    '/Library',
    r'/C:/Windows',
    r'C:\Windows',
}

# Blocked file extensions (executables and scripts)
BLOCKED_EXTENSIONS: set = {
    '.exe',
    '.bat',
    '.cmd',
    '.sh',
    '.ps1',
    '.vbs',
    '.js',
}


#: Environment variable that replaces the default extension whitelist
#: (comma-separated, leading dots optional).
_EXTENSIONS_ENV_VAR = 'MCP_ALLOWED_EXTENSIONS'


def resolve_allowed_extensions(default: set) -> set:
    """解析 ``MCP_ALLOWED_EXTENSIONS`` 覆盖，未设置或为空时返回 *default*。

    修复（2026-09-17，ADR 0007）：原先只有 ``__init__`` 读环境变量，legacy
    ``validate()`` 直接读类常量 ``ALLOWED_EXTENSIONS``，于是
    ``MCP_ALLOWED_EXTENSIONS`` 在 legacy 路径上静默失效 —— 同一个进程里两条入口
    对同一个文件给出不同答案。抽成单一解析入口后两条路径共用同一份逻辑。

    同时把「纯空白」视同未设置（与 OL 的 ``get_allowed_extensions()`` 语义一致）：
    旧实现用 ``if custom_exts:`` 判断，``MCP_ALLOWED_EXTENSIONS=" "`` 会产生空集，
    把全部路径判为非法。

    Args:
        default: 环境变量缺省或为空白时使用的默认白名单。

    Returns:
        生效的扩展名集合，每项都带前导点。
    """
    raw = os.environ.get(_EXTENSIONS_ENV_VAR, '').strip()
    if not raw:
        return default
    return {
        ext if ext.startswith('.') else f'.{ext}'
        for ext in (part.strip() for part in raw.split(','))
        if ext
    }


@dataclass
class ValidationResult:
    """Result of path validation.

    Attributes:
        success: True if path passed all validation checks.
        error: Error message if validation failed, None otherwise.
        resolved_path: The resolved Path object if successful, None otherwise.
    """
    success: bool
    error: Optional[str] = None
    resolved_path: Optional[Path] = None


class PathValidator:
    """Validates file paths against security rules.

    Validates that:
    - Path doesn't contain traversal components (..)
    - Path resolves to an allowed directory
    - Path is not a symlink pointing outside allowed directories
    - Path doesn't target system directories
    - File has allowed extension (document whitelist)
    - File extension is not blocked (executable blacklist)
    - File size is within limit
    - File exists and is accessible

    Args:
        allowed_directories: List of root directories that are allowed to access.
        max_file_size_bytes: Maximum allowed file size in bytes (default: 100MB).
    """

    # E2E-80: extended with all the output formats ORF's apply-md
    # advertises support for. Previously the validator rejected output
    # paths like result.csv or result.xlsx even though ORF supports them.
    ALLOWED_EXTENSIONS = {
        '.md', '.docx', '.pptx', '.xliff', '.xlf', '.xml', '.html', '.odt', '.epub', '.zip',
        '.csv', '.tsv', '.xlsx', '.json', '.ipynb', '.eml', '.msg', '.srt', '.icml', '.rtf',
        '.pdf',
    }

    def __init__(
        self,
        allowed_directories: List[Path],
        max_file_size_bytes: int = 100_000_000,
    ):
        self.allowed_directories = [Path(d).resolve() for d in allowed_directories]
        self.max_file_size_bytes = max_file_size_bytes

        # P2-T4: MCP_ALLOWED_EXTENSIONS env var overrides the default set
        # (2026-09-17, ADR 0007: routed through the shared resolver so the
        # legacy validate() below cannot disagree with validate_path()).
        self.ALLOWED_EXTENSIONS = resolve_allowed_extensions(PathValidator.ALLOWED_EXTENSIONS)

    def validate_path(self, path: str, allow_missing: bool = False) -> ValidationResult:
        """Validate a file path against security rules.

        Args:
            path: The path string to validate.
            allow_missing: If True, skip the existence check (for output paths).
                          If False (default), file must exist and be readable.

        Returns:
            ValidationResult with success=True if valid, or success=False with error message.
        """
        try:
            input_path = Path(path)
        except (ValueError, OSError) as e:
            return ValidationResult(
                success=False,
                error=f"Invalid path format: {e}",
            )

        # Check for path traversal attempts
        if '..' in input_path.parts:
            return ValidationResult(
                success=False,
                error="Path traversal detected (.. components are not allowed)",
            )

        # Resolve the path (follows symlinks)
        try:
            resolved = input_path.resolve()
        except (ValueError, OSError) as e:
            return ValidationResult(
                success=False,
                error=f"Cannot resolve path: {e}",
            )

        # Check if path points to a system directory
        for sys_dir in SYSTEM_DIRS:
            sys_path = Path(sys_dir)
            try:
                resolved_parts = resolved.parts
                sys_parts = sys_path.parts
                if len(resolved_parts) >= len(sys_parts):
                    if all(
                        a == b for a, b in zip(resolved_parts[:len(sys_parts)], sys_parts)
                    ):
                        return ValidationResult(
                            success=False,
                            error=f"Access to system directory not allowed: {sys_dir}",
                        )
            except ValueError:
                pass

        # Check if resolved path is within allowed directories
        is_in_allowed = False
        for allowed_dir in self.allowed_directories:
            try:
                resolved.relative_to(allowed_dir)
                is_in_allowed = True
                break
            except ValueError:
                pass

        if not is_in_allowed:
            return ValidationResult(
                success=False,
                error=f"Path is not within allowed directories: {', '.join(str(d) for d in self.allowed_directories)}",
            )

        # Check if path is a symlink pointing outside allowed directories
        if input_path.is_symlink():
            try:
                link_target = input_path.resolve()
                link_in_allowed = False
                for allowed_dir in self.allowed_directories:
                    try:
                        link_target.relative_to(allowed_dir)
                        link_in_allowed = True
                        break
                    except ValueError:
                        pass
                if not link_in_allowed:
                    return ValidationResult(
                        success=False,
                        error="Symlink points outside allowed directories",
                    )
            except (ValueError, OSError):
                return ValidationResult(
                    success=False,
                    error="Symlink target is not accessible",
                )

        # Check blocked extensions
        if input_path.suffix.lower() in BLOCKED_EXTENSIONS:
            return ValidationResult(
                success=False,
                error=f"File extension '{input_path.suffix}' is blocked",
            )

        # Check allowed extensions (document whitelist)
        if input_path.suffix.lower() not in self.ALLOWED_EXTENSIONS:
            # Directories have no extension; skip extension check for directories
            if resolved.is_dir():
                return ValidationResult(success=True, resolved_path=resolved)
            return ValidationResult(
                success=False,
                error=f"Extension '{input_path.suffix}' not in allowed set",
            )

        if not resolved.exists():
            if allow_missing:
                return ValidationResult(success=True, resolved_path=resolved)
            return ValidationResult(
                success=False,
                error="File does not exist",
            )

        # Check if it's a file (not a directory)
        if not resolved.is_file():
            return ValidationResult(
                success=False,
                error="Path must be a file, not a directory",
            )

        # Check file size
        try:
            file_size = resolved.stat().st_size
            if file_size > self.max_file_size_bytes:
                return ValidationResult(
                    success=False,
                    error=f"File size ({file_size} bytes) exceeds limit of {self.max_file_size_bytes} bytes",
                )
        except OSError as e:
            return ValidationResult(
                success=False,
                error=f"Cannot access file to check size: {e}",
            )

        return ValidationResult(
            success=True,
            resolved_path=resolved,
        )

    @staticmethod
    def validate(input_path: str, base_dir: Optional[Path] = None) -> Tuple[bool, str]:
        """[Legacy] Static path validation for backward compatibility.

        This is a simplified wrapper that checks path traversal and allowed
        extensions only. For full validation (directory containment, system
        dir blocking, symlink checks, file size limits), use the instance
        method ``validate_path()`` via ``PathValidator(allowed_directories=...).validate_path(path)``.

        Returns:
            (is_valid, error_message) tuple matching the original API.
        """
        path = Path(input_path)

        # Check for directory traversal
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False, "Invalid path"

        if '..' in path.parts:
            return False, "Path traversal not allowed"

        if path.suffix.lower() not in resolve_allowed_extensions(
            PathValidator.ALLOWED_EXTENSIONS
        ):
            return False, f"Extension '{path.suffix}' not in allowed set"

        if base_dir:
            try:
                resolved.relative_to(base_dir.resolve())
            except ValueError:
                return False, "Path outside allowed directory"

        return True, ""
