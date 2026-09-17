"""MCP Server configuration with directory allowlist support."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class MCPConfig:
    """Configuration for ORF MCP Server.

    Attributes:
        host: Server host address.
        port: Server port number.
        max_file_size_mb: Maximum file size in megabytes.
        timeout_seconds: Request timeout.
        allowed_formats: List of allowed output formats.
        allowed_directories: List of root directories allowed for file access.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    max_file_size_mb: int = 100
    timeout_seconds: int = 120
    allowed_formats: Optional[List[str]] = None
    allowed_directories: List[Path] = None  # type: ignore[assignment]
    metrics_dir: str = "/tmp/omni-metrics"

    def __post_init__(self) -> None:
        if self.allowed_formats is None:
            self.allowed_formats = [
                "docx",
                "odt",
                "epub",
                "html",
                "rtf",
                "pdf",
                "csv",
                "json",
                "xlsx",
                "xml",
                "ipynb",
                "eml",
                "msg",
                "pptx",
            ]


def _parse_allowed_dirs(value: str) -> List[Path]:
    """Parse platform-separator/comma separated paths into a list of Path objects.

    2026-06-17 round 12 (FIX-#5): handle single-path values without
    separator. Previously `/tmp` returned [] (no separator matched),
    which caused the MCP server to fall back to Path.cwd() as the
    allowlist, rejecting tmp_path fixtures in tests.

    2026-09-17 修复（ADR 0007 同类缺陷，与 ``omni_mcp/orchestrator._split_allowlist``
    同一模式）：旧实现在所有平台上都优先按 ``":"`` 切分（``separators = [":", ";"]``）。
    Windows 盘符本身含 ``":"``，于是 ``ORF_MCP_ALLOWED_DIRS=C:\\work``（以及文档里
    展示的 ``C:\\a;C:\\b`` 写法）被切成 ``["C", "\\work"]`` —— 解析出的目录都不包含
    目标路径，allowlist 在 Windows 上等于完全失效，任何合法路径都被判
    ``PATH_NOT_ALLOWED``（复现：``tests/test_orf_security_attacks.py`` 的两条
    happy-path 用例）。

    现在按 ``os.pathsep``（POSIX ``":"``、Windows ``";"``）加逗号切分：Linux/CI
    行为与修复前一致，Windows 上盘符不再被误切。三份实现（OPP/OL/ORF 的
    allowlist 入口）由 ``tests/security/test_path_policy_parity.py`` 锁定一致性。
    """
    value = value.strip()
    if not value:
        return []
    parts: List[str] = []
    for chunk in value.split(os.pathsep):
        parts.extend(chunk.split(","))
    return [Path(p.strip()) for p in parts if p.strip()]


def _load_from_yaml(config_path: Path) -> Optional[dict]:
    """Load configuration from a YAML file."""
    if not config_path.exists():
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load MCP config from {config_path}: {e}")
        return None


def _load_from_env() -> dict:
    """Load configuration from environment variables."""
    config = {}

    allowed_dirs = os.environ.get("MCP_ALLOWED_DIRECTORIES") or os.environ.get("ORF_MCP_ALLOWED_DIRS", "")
    if allowed_dirs:
        config["allowed_directories"] = _parse_allowed_dirs(allowed_dirs)

    max_file_size = os.environ.get("ORF_MCP_MAX_FILE_SIZE_MB")
    if max_file_size:
        try:
            config["max_file_size_mb"] = int(max_file_size)
        except ValueError:
            logger.warning(
                "Invalid ORF_MCP_MAX_FILE_SIZE_MB=%r; falling back to default", max_file_size
            )

    timeout = os.environ.get("MCP_TOOL_TIMEOUT") or os.environ.get("ORF_MCP_TIMEOUT")
    if timeout:
        try:
            config["timeout_seconds"] = int(timeout)
        except ValueError:
            logger.warning(
                "Invalid ORF_MCP_TIMEOUT=%r; falling back to default", timeout
            )

    metrics_dir = os.environ.get("OMNI_METRICS_DIR")
    if metrics_dir:
        config["metrics_dir"] = metrics_dir

    return config


def _normalize_allowed_directories(dirs: list) -> List[Path]:
    """Convert a mixed list of str/Path to List[Path]."""
    result: List[Path] = []
    for d in dirs:
        if isinstance(d, str):
            result.append(Path(d))
        elif isinstance(d, Path):
            result.append(d)
        else:
            logger.warning("Ignoring unexpected type in allowed_directories: %s", type(d))
    return result


def load_config(config_path: Optional[Path] = None) -> MCPConfig:
    """Load MCP configuration from YAML file or environment variables.

    Resolution order:
      1. Environment variable ``MCP_ALLOWED_DIRECTORIES`` (primary, cross-module).
      2. Environment variable ``ORF_MCP_ALLOWED_DIRS`` (ORF-specific fallback).
      3. YAML config file (optional, with ``security.allowed_directories`` key).
      4. Raises ``ValueError`` if neither provides a value (fail-CLOSED).

    Args:
        config_path: Optional path to YAML configuration file.

    Returns:
        MCPConfig instance with loaded configuration.

    Raises:
        ValueError: If neither ``MCP_ALLOWED_DIRECTORIES`` nor ``ORF_MCP_ALLOWED_DIRS`` is set.
    """
    config_data: dict = {}

    # Try loading from YAML file if provided
    if config_path:
        yaml_data = _load_from_yaml(config_path)
        if yaml_data:
            # Flatten nested keys: security.allowed_directories → allowed_directories
            if "security" in yaml_data and isinstance(yaml_data["security"], dict):
                for k, v in yaml_data["security"].items():
                    config_data[k] = v
            # Top-level keys override
            for k, v in yaml_data.items():
                if k != "security":
                    config_data[k] = v

    # Merge with environment variables (env vars take precedence)
    env_data = _load_from_env()
    config_data.update(env_data)

    # Normalize allowed_directories from YAML strings to Path objects
    if "allowed_directories" in config_data and isinstance(
        config_data["allowed_directories"], list
    ):
        config_data["allowed_directories"] = _normalize_allowed_directories(
            config_data["allowed_directories"]
        )

    # P0-T2: fail-CLOSED — allowed_directories must be explicitly provided
    allowed_dirs = config_data.get("allowed_directories")
    if not allowed_dirs:
        raise ValueError(
            "MCP_ALLOWED_DIRECTORIES (or ORF_MCP_ALLOWED_DIRS) must be set "
            "(fail-CLOSED security policy). "
            "Export it as a colon-separated list of allowed directories."
        )

    # Build MCPConfig; __post_init__ fills defaults for any omitted fields
    known_fields = {f.name for f in MCPConfig.__dataclass_fields__.values()}
    return MCPConfig(**{k: v for k, v in config_data.items() if k in known_fields})
