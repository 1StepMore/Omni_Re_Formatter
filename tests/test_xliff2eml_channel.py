"""XLIFF → EML backfill channel tests (T2 of the EML production plan).

Fixtures are built in-code: a multipart/mixed message (multipart/alternative
with a base64 ``text/plain`` and a quoted-printable ``text/html``, plus one
attachment), a single-part message, and OPP's two-entry skeleton
(``message.eml`` + ``eml_map.json``).

The invariants under test are fidelity ones: the original header block stays
byte-identical, the multipart boundary never changes, only the selected body
part receives the translation, and the unselected alternative plus every
attachment survive untouched.
"""

from __future__ import annotations

import email.parser
import json
import zipfile
from email.encoders import encode_base64
from email.message import EmailMessage, Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.policy import default
from pathlib import Path
from typing import Any

from orf.channels.xliff2eml import XLIFF2EMLConverter
from orf.converters.base import ConversionResult
from orf.converters.options import ConverterOptions

_CRLF = "\r\n"
_BLANK = b"\r\n\r\n"
_ATTACHMENT_BYTES = b"\x00\x01BINARY-PAYLOAD"


def _parse(raw: bytes) -> Message:
    return email.parser.BytesParser(policy=default).parsebytes(raw)


def _multipart_message() -> bytes:
    """Multipart/mixed: alternative (base64 plain + QP html) + attachment."""
    message = MIMEMultipart("mixed")
    message["From"] = "author@example.com"
    message["To"] = "reader@example.com"
    message["Subject"] = (
        "A deliberately long subject line that the email library folds across "
        "several header lines so header preservation is actually exercised"
    )

    alternative = MIMEMultipart("alternative")
    plain = EmailMessage()
    plain.set_content("Hello\n\nWorld", subtype="plain", charset="utf-8", cte="base64")
    alternative.attach(plain)
    html = EmailMessage()
    html.set_content(
        "<p>Hello<br><br>World</p>",
        subtype="html",
        charset="utf-8",
        cte="quoted-printable",
    )
    alternative.attach(html)
    message.attach(alternative)

    attachment = MIMEBase("application", "octet-stream")
    attachment.set_payload(_ATTACHMENT_BYTES)
    encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition", "attachment", filename="data.bin"
    )
    message.attach(attachment)
    return message.as_bytes(policy=default.clone(linesep=_CRLF))


def _single_part_message(body: str, cte: str) -> bytes:
    message = EmailMessage()
    message["From"] = "author@example.com"
    message["To"] = "reader@example.com"
    message["Subject"] = "Single part message"
    message.set_content(body, subtype="plain", charset="utf-8", cte=cte)
    return message.as_bytes(policy=default.clone(linesep=_CRLF))


def _sidecar(
    *,
    walk_index: int = 2,
    content_type: str = "text/plain",
    unit_ids: tuple[str, ...] = ("1", "2"),
    version: int = 1,
    headers_rebuilt: bool = False,
    joiner: str = "\n\n",
) -> dict[str, Any]:
    return {
        "version": version,
        "body": {
            "walk_index": walk_index,
            "content_type": content_type,
            "charset": "utf-8",
            "content_transfer_encoding": "base64",
            "line_sep": "\r\n",
            "unit_ids": list(unit_ids),
            "joiner": joiner,
        },
        "headers_rebuilt": headers_rebuilt,
    }


def _make_skeleton(
    directory: Path, raw: bytes, sidecar: dict[str, Any]
) -> Path:
    skeleton = directory / "msg.skeleton.zip"
    with zipfile.ZipFile(skeleton, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("message.eml", raw)
        archive.writestr("eml_map.json", json.dumps(sidecar, ensure_ascii=False))
    return skeleton


def _make_xliff(
    directory: Path, units: dict[str, tuple[str, str | None]]
) -> Path:
    entries = []
    for unit_id, (source, target) in units.items():
        entry = [f'<trans-unit id="{unit_id}">', f"<source>{source}</source>"]
        if target is not None:
            entry.append(f"<target>{target}</target>")
        entry.append("</trans-unit>")
        entries.append("".join(entry))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">\n'
        '  <file original="msg.eml" source-language="en" datatype="plaintext"'
        ' target-language="fr">\n'
        "    <body>\n      " + "\n      ".join(entries) + "\n    </body>\n"
        "  </file>\n</xliff>\n"
    )
    path = directory / "msg.fr.xlf"
    path.write_text(xml, encoding="utf-8")
    return path


def _convert(skeleton: Path, xliff: Path, output: Path) -> ConversionResult:
    return XLIFF2EMLConverter().convert(
        skeleton, xliff, output, options=ConverterOptions()
    )


def _part(content_type: str, raw: bytes) -> Message:
    matches = [
        part for part in _parse(raw).walk() if part.get_content_type() == content_type
    ]
    assert len(matches) == 1
    return matches[0]


def _translated_pair(tmp_path: Path) -> tuple[Path, Path, bytes]:
    raw = _multipart_message()
    skeleton = _make_skeleton(
        tmp_path,
        raw,
        _sidecar(walk_index=2, content_type="text/plain", unit_ids=("1", "2")),
    )
    xliff = _make_xliff(
        tmp_path, {"1": ("Hello", "译1"), "2": ("World", "译2")}
    )
    return skeleton, xliff, raw


def test_header_block_byte_identical(tmp_path: Path) -> None:
    """Headers, boundary and attachment survive a body translation."""
    skeleton, xliff, raw = _translated_pair(tmp_path)
    output = tmp_path / "out.eml"

    result = _convert(skeleton, xliff, output)

    assert result.success, [error.message for error in result.errors]
    written = output.read_bytes()
    head = raw[: raw.find(_BLANK) + len(_BLANK)]
    assert written[: len(head)] == head

    original = _parse(raw)
    translated = _parse(written)
    assert translated.get_boundary() == original.get_boundary()
    assert _part("application/octet-stream", written).get_payload(
        decode=True
    ) == _ATTACHMENT_BYTES
    assert _part("application/octet-stream", written).get_filename() == "data.bin"


def test_selected_body_receives_translation(tmp_path: Path) -> None:
    skeleton, xliff, _raw = _translated_pair(tmp_path)
    output = tmp_path / "out.eml"

    result = _convert(skeleton, xliff, output)

    assert result.success, [error.message for error in result.errors]
    body = list(_parse(output.read_bytes()).walk())[2]
    assert body.get_content() == "译1\n\n译2\n"


def test_unselected_alternative_untouched(tmp_path: Path) -> None:
    """Only the selected part is translated; the HTML alternative is preserved."""
    skeleton, xliff, raw = _translated_pair(tmp_path)
    output = tmp_path / "out.eml"
    original_html = _part("text/html", raw).get_content()

    result = _convert(skeleton, xliff, output)

    assert result.success, [error.message for error in result.errors]
    assert _part("text/html", output.read_bytes()).get_content() == original_html


def test_single_part_eml(tmp_path: Path) -> None:
    raw = _single_part_message(
        "Hi team,\n\n-- \nJohn Doe\nACME\n", cte="7bit"
    )
    skeleton = _make_skeleton(
        tmp_path,
        raw,
        _sidecar(
            walk_index=0,
            content_type="text/plain",
            unit_ids=("1", "2"),
        ),
    )
    xliff = _make_xliff(
        tmp_path,
        {
            "1": ("Hi team,", "Hi all,"),
            "2": ("John Doe\nACME", "Jane Doe\nACME"),
        },
    )
    output = tmp_path / "out.eml"

    result = _convert(skeleton, xliff, output)

    assert result.success, [error.message for error in result.errors]
    original = _parse(raw)
    translated = _parse(output.read_bytes())
    assert len(list(translated.walk())) == 1
    body = translated.get_content().replace("\r\n", "\n")
    assert "Hi all," in body
    assert "-- \nJane Doe\nACME" in body
    for header in ("From", "To", "Subject"):
        assert translated[header] == original[header]
    head = raw[: raw.find(_BLANK) + len(_BLANK)]
    assert output.read_bytes()[: len(head)] == head

    chinese_xliff = _make_xliff(
        tmp_path,
        {
            "1": ("Hi team,", "各位好，"),
            "2": ("John Doe\nACME", "张三\nACME"),
        },
    )
    chinese_output = tmp_path / "out-zh.eml"
    chinese_result = _convert(skeleton, chinese_xliff, chinese_output)

    assert chinese_result.success, [
        error.message for error in chinese_result.errors
    ]
    chinese_message = _parse(chinese_output.read_bytes())
    assert "各位好，" in chinese_message.get_content()
    assert chinese_message["Content-Transfer-Encoding"] == "base64"
    assert [warning.code for warning in chinese_result.warnings] == [
        "HEADERS_REBUILT"
    ]


def test_headers_rebuilt_warning(tmp_path: Path) -> None:
    raw = _multipart_message()
    skeleton = _make_skeleton(
        tmp_path,
        raw,
        _sidecar(headers_rebuilt=True),
    )
    xliff = _make_xliff(
        tmp_path, {"1": ("Hello", "译1"), "2": ("World", "译2")}
    )
    output = tmp_path / "out.eml"

    result = _convert(skeleton, xliff, output)

    assert result.success, [error.message for error in result.errors]
    assert any(warning.code == "HEADERS_REBUILT" for warning in result.warnings)
    assert result.metadata["headers_rebuilt"] is True


def test_walk_index_content_type_mismatch(tmp_path: Path) -> None:
    raw = _multipart_message()
    # walk order: 0 mixed, 1 alternative, 2 text/plain, 3 text/html, 4 attachment
    skeleton = _make_skeleton(
        tmp_path,
        raw,
        _sidecar(walk_index=3, content_type="text/plain"),
    )
    xliff = _make_xliff(
        tmp_path, {"1": ("Hello", "译1"), "2": ("World", "译2")}
    )
    output = tmp_path / "out.eml"

    result = _convert(skeleton, xliff, output)

    assert not result.success
    assert result.errors[0].code == "EML_MAP_MISMATCH"
    assert "text/plain" in result.errors[0].message
    assert "text/html" in result.errors[0].message
    assert not output.exists()


def test_sidecar_version_fallback(tmp_path: Path) -> None:
    raw = _single_part_message("Hello\n\nWorld", cte="base64")

    fallback_skeleton = _make_skeleton(
        tmp_path,
        raw,
        _sidecar(walk_index=0, unit_ids=("1",), version=2),
    )
    single_unit = _make_xliff(tmp_path, {"1": ("Hello\n\nWorld", "Salut")})
    fallback_output = tmp_path / "fallback.eml"

    fallback = _convert(fallback_skeleton, single_unit, fallback_output)

    assert fallback.success, [error.message for error in fallback.errors]
    assert any(
        warning.code == "SIDECAR_VERSION_FALLBACK"
        for warning in fallback.warnings
    )
    assert _parse(fallback_output.read_bytes()).get_content() == "Salut"

    two_unit = _make_xliff(
        tmp_path, {"1": ("Hello", "Salut"), "2": ("World", "Monde")}
    )
    rejected_output = tmp_path / "rejected.eml"

    rejected = _convert(fallback_skeleton, two_unit, rejected_output)

    assert not rejected.success
    assert rejected.errors[0].code == "EML_SKELETON_ERROR"
    assert "2" in rejected.errors[0].message
    assert not rejected_output.exists()


def test_missing_target_keeps_original(tmp_path: Path) -> None:
    raw = _single_part_message("Hello\n\nWorld", cte="base64")
    skeleton = _make_skeleton(
        tmp_path,
        raw,
        _sidecar(walk_index=0, unit_ids=("1", "2")),
    )
    xliff = _make_xliff(tmp_path, {"1": ("Hello", "译1"), "2": ("World", None)})
    output = tmp_path / "out.eml"

    result = _convert(skeleton, xliff, output)

    assert result.success, [error.message for error in result.errors]
    assert _parse(output.read_bytes()).get_content() == "译1\n\nWorld\n"
    untranslated = [
        warning for warning in result.warnings
        if warning.code == "UNITS_UNTRANSLATED"
    ]
    assert len(untranslated) == 1
    assert "1" in untranslated[0].message
    assert result.metadata["units_untranslated"] == 1
    assert result.metadata["units_written"] == 1
