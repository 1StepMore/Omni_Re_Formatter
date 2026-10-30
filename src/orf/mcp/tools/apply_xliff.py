"""apply_xliff tool — Apply XLIFF translation for ORF MCP Server."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from orf.mcp._errors import FILE_PATH_NOT_ALLOWED, MUTUALLY_EXCLUSIVE, PATH_NOT_ALLOWED
from orf.mcp.auth import auth_failure_response, check_auth
from orf.mcp.common import (
    augment_error,
    get_path_validator,
    logger,
    safe_unlink,
    success_response,
)
from orf.mcp.manifest import written_files
from orf.mcp.rate_limiter import check_rate_limit, rate_limit_failure_response


def apply_xliff(
    input_file: str,
    xliff_path: str,
    output_path: str,
    format: str,
    xliff_content: Optional[str] = None,
    images: Optional[list[dict]] = None,
    force: bool = False,
    no_cache: bool = False,
    max_file_size_mb: Optional[float] = None,
    auth_token: Optional[str] = None,
    traceparent: Optional[str] = None,
    skeleton_html: Optional[str] = None,
) -> str:
    # H5: token bucket rate limiter
    rate_ok, rate_err = check_rate_limit()
    if not rate_ok:
        return json.dumps({**rate_limit_failure_response(), "error": rate_err})
    # 2026-06-18 round 16 Phase A4: MCP shared-secret auth.
    auth_ok, _ = check_auth(auth_token)
    if not auth_ok:
        return json.dumps(auth_failure_response(), ensure_ascii=False)
    """Apply XLIFF translation. In-process equivalent of the MCP tool."""
    # C4 fix: reject image placements that carry `file_path` (arbitrary
    # file read). MCP clients must provide `data_base64` only.
    if images:
        for idx, img_dict in enumerate(images):
            if isinstance(img_dict, dict) and img_dict.get("file_path"):
                return json.dumps(augment_error({
                    "success": False,
                    "output_path": None,
                    "errors": [{
                        "code": FILE_PATH_NOT_ALLOWED,
                        "message": (
                            f"image[{idx}].file_path is not allowed via MCP; "
                            "supply data_base64 instead."
                        ),
                    }],
                    "warnings": [],
                    "metadata": {},
                }))
    # C5 fix: validate output_path against allowlist before subprocess
    result_out = get_path_validator().validate_path(output_path, allow_missing=True)
    if not result_out.success:
        return json.dumps(augment_error({
            "success": False,
            "output_path": None,
            "errors": [{"code": PATH_NOT_ALLOWED, "message": f"output_path: {result_out.error}"}],
            "warnings": [],
            "metadata": {},
        }))
    if xliff_path and xliff_content:
        return json.dumps(augment_error({
            "success": False,
            "output_path": None,
            "errors": [{"code": MUTUALLY_EXCLUSIVE, "message": "xliff_path and xliff_content are mutually exclusive"}],
            "warnings": [],
            "metadata": {}
        }))

    xliff_to_use = xliff_path
    xliff_temp_path = None
    if xliff_content:
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.xliff', delete=False)
        tmp.write(xliff_content)
        tmp.close()
        xliff_temp_path = tmp.name
        xliff_to_use = xliff_temp_path

    for p in [input_file, xliff_to_use]:
        result_p = get_path_validator().validate_path(p)
        if not result_p.success:
            if xliff_temp_path:
                safe_unlink(xliff_temp_path)
            return json.dumps(augment_error({
                "success": False,
                "output_path": None,
                "errors": [{"code": PATH_NOT_ALLOWED, "message": result_p.error}],
                "warnings": [],
                "metadata": {}
            }))

    args = [
        "apply-xliff", input_file,
        "--xliff", xliff_to_use,
        "--output", output_path,
        "--format", format,
    ]
    if force:
        args.append("--force")
    if no_cache:
        args.append("--no-cache")
    if max_file_size_mb is not None:
        args.extend(["--max-file-size-mb", str(max_file_size_mb)])
    if skeleton_html:
        sh_result = get_path_validator().validate_path(skeleton_html)
        if not sh_result.success:
            return json.dumps(augment_error({
                "success": False,
                "output_path": None,
                "errors": [{"code": PATH_NOT_ALLOWED, "message": f"skeleton_html: {sh_result.error}"}],
                "warnings": [],
                "metadata": {},
            }))
        args.extend(["--skeleton-html", skeleton_html])

    temp_created = False
    temp_path = ""
    if images:
        images_data = []
        for img_dict in images:
            try:
                img_bytes = None
                if "data_base64" in img_dict and img_dict["data_base64"]:
                    img_bytes = base64.b64decode(img_dict["data_base64"])
                elif "file_path" in img_dict and img_dict["file_path"]:
                    img_bytes = Path(img_dict["file_path"]).read_bytes()

                if img_bytes:
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    img_record = {
                        "data_base64": b64,
                        "mime_type": img_dict.get("mime_type", "image/png"),
                        "width": img_dict.get("width"),
                        "height": img_dict.get("height"),
                        "paragraph_index": img_dict.get("paragraph_index"),
                        "slide_index": img_dict.get("slide_index"),
                        "page_number": img_dict.get("page_number"),
                        "element_index": img_dict.get("element_index"),
                        "spine_index": img_dict.get("spine_index"),
                    }
                    images_data.append(img_record)
            except Exception as e:
                logger.warning("Failed to process image placement: %s", e)
                continue

        if images_data:
            fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="orf_images_")
            try:
                # Canonical cross-repo shape (e2e-test-suite#223 P2.2): OPP's
                # images.json and the apply_md MCP tool both write
                # {"images": [...]}; the bare list was this tool's outlier.
                # Both ORF CLI readers dual-read (wrapped + legacy bare list),
                # so this stays backward-compatible for every in-tree consumer.
                os.write(fd, json.dumps({"images": images_data}).encode("utf-8"))
                os.close(fd)
                args.extend(["--images-json", temp_path])
                temp_created = True
            except Exception as e:
                logger.error("Failed to create temp images file: %s", e)
                os.close(fd)
                temp_created = False

    from orf.mcp.server import _run_cli_command
    result = _run_cli_command(args)

    if temp_created:
        safe_unlink(temp_path)

    if xliff_temp_path:
        safe_unlink(xliff_temp_path)

    if result.get("success"):
        response = success_response(result)
        response["outputs"], response["sidecars"] = written_files(
            result.get("output_path")
        )
        return json.dumps(response)
    return json.dumps(augment_error(result))
