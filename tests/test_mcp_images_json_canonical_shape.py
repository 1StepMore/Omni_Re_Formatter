"""Canonical ``images.json`` wire shape for the ORF MCP tools (P2.2).

Cross-repo contract (e2e-test-suite#223 P2.2, recorded in ADR 0008): OPP's
``images.json`` and the ``apply_md`` MCP tool both write ``{"images": [...]}``;
the ``apply_xliff`` MCP tool was the outlier writing a bare list. Both ORF CLI
readers dual-read (wrapped + legacy bare list), so converging the writer is
backward-compatible. These tests lock the wrapped shape on BOTH MCP writers so
the outlier cannot silently return.
"""
import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from orf.mcp.tools.apply_md import apply_md  # noqa: E402
from orf.mcp.tools.apply_xliff import apply_xliff  # noqa: E402


@pytest.fixture()
def allow_tmp_paths(tmp_path, monkeypatch):
    """Point the path-validator singleton's allowlist at this test's tmp_path."""
    import orf.mcp.server as server_module

    monkeypatch.setattr(
        server_module._path_validator,
        "allowed_directories",
        [tmp_path.resolve()],
    )

MINIMAL_XLIFF = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<xliff version='1.2' xmlns='urn:oasis:names:tc:xliff:document:1.2'>"
    "<file original='hello.txt' source-language='en' target-language='zh' datatype='plaintext'>"
    "<body><trans-unit id='1'><source>Hello</source><target>你好</target></trans-unit></body>"
    "</file></xliff>"
)

PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63fcffff3f0300050001a5f6454000"
        "00000049454e44ae426082"
    )
).decode("ascii")


def _capture_images_json(tmp_path: Path):
    """Return a mocked ``_run_cli_command`` that snapshots --images-json content."""
    captured: dict = {}

    def fake_cli(args):
        if "--images-json" in args:
            images_json = args[args.index("--images-json") + 1]
            captured["raw"] = Path(images_json).read_text(encoding="utf-8")
        output_path = tmp_path / "out.bin"
        output_path.write_bytes(b"artifact")
        captured["output_path"] = str(output_path)
        return {
            "success": True,
            "output_path": captured["output_path"],
            "errors": [],
            "warnings": [],
            "metadata": {},
        }

    return captured, fake_cli


def test_apply_xliff_mcp_writes_wrapped_images_shape(tmp_path, allow_tmp_paths):
    input_file = tmp_path / "in.docx"
    input_file.write_bytes(b"fake-docx-for-test")
    # xliff_path (inside the allowlisted tmp_path), not xliff_content — the
    # latter lands a NamedTemporaryFile in gettempdir(), which the validator
    # correctly rejects when /tmp is not an allowed directory.
    xliff_file = tmp_path / "t.xlf"
    xliff_file.write_text(MINIMAL_XLIFF, encoding="utf-8")
    captured, fake_cli = _capture_images_json(tmp_path)

    with patch("orf.mcp.server._run_cli_command", side_effect=fake_cli):
        raw = apply_xliff(
            input_file=str(input_file),
            xliff_path=str(xliff_file),
            output_path=str(tmp_path / "out.docx"),
            format="docx",
            images=[{
                "data_base64": PNG_1PX,
                "mime_type": "image/png",
                "width": 1,
                "height": 1,
                "paragraph_index": 3,
            }],
        )

    assert captured["raw"], "apply_xliff MCP never wrote --images-json"
    payload = json.loads(captured["raw"])
    assert set(payload) == {"images"}, f"expected wrapped shape, got keys {set(payload)}"
    (record,) = payload["images"]
    assert record["data_base64"] == PNG_1PX
    assert record["mime_type"] == "image/png"
    assert record["paragraph_index"] == 3
    assert json.loads(raw)["success"] is True


def test_apply_md_mcp_writes_wrapped_images_shape(tmp_path, allow_tmp_paths):
    input_md = tmp_path / "in.md"
    input_md.write_text("# Hello\n\nWorld\n", encoding="utf-8")
    captured, fake_cli = _capture_images_json(tmp_path)

    with patch("orf.mcp.server._run_cli_command", side_effect=fake_cli):
        raw = apply_md(
            input_md=str(input_md),
            target_format="docx",
            output_path=str(tmp_path / "out.docx"),
            images=[{"data_base64": PNG_1PX, "mime_type": "image/png"}],
        )

    assert captured["raw"], "apply_md MCP never wrote --images-json"
    payload = json.loads(captured["raw"])
    assert set(payload) == {"images"}, f"expected wrapped shape, got keys {set(payload)}"
    (record,) = payload["images"]
    assert record["data_base64"] == PNG_1PX
    assert json.loads(raw)["success"] is True
