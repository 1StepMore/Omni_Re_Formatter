"""XLIFF to EML backfill channel (format-preserving).

OPP packages an EML skeleton as ``<stem>.skeleton.zip`` holding exactly two
archive-root entries:

``message.eml``
    The original ``.eml`` bytes, verbatim. This channel never rebuilds the
    message from parsed fields; it parses those bytes, rewrites one body part,
    and splices the new body under the *original* header block.
``eml_map.json``
    A UTF-8 sidecar describing which MIME part was offered for translation::

        {"version": 1,
         "body": {"walk_index": 3,
                  "content_type": "text/plain",
                  "charset": "utf-8",
                  "content_transfer_encoding": "base64",
                  "line_sep": "\\r\\n",
                  "unit_ids": ["1", "2", "3"],
                  "joiner": "\\n\\n"},
         "headers_rebuilt": false}

    ``list(msg.walk())[walk_index]`` is the body part; its decoded text is the
    paragraphs joined by ``joiner``; paragraph ``i`` is the paragraph carried
    by XLIFF trans-unit ``unit_ids[i]``.

Fidelity contract
-----------------
Only the selected body part's payload is rewritten. Every header — including
the folded ``Subject`` block and the multipart boundary — is copied from the
original bytes up to and including the first blank line, so it is byte
identical in the output. The alternative body part (``text/html`` beside a
``text/plain`` selection) and every attachment are left exactly as the
skeleton holds them: they are never re-encoded, never re-serialized and never
translated. When the skeleton carries no header/body separator (or the
sidecar says ``headers_rebuilt: true``) the message has to be serialized
whole; that path emits a ``HEADERS_REBUILT`` warning because header byte
identity is no longer guaranteed.

A bare ``.eml`` is rejected: it carries no ``eml_map.json``, so nothing says
which part was extracted for translation. Re-extract with
``opp <file>.eml --target-format both`` to get the skeleton.
"""

from __future__ import annotations

import base64
import email.parser
import email.policy
import json
import quopri
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from lxml import etree

from orf.converters.base import (
    BaseConverter,
    ConversionResult,
    ErrorDetail,
    WarningDetail,
)
from orf.converters.options import ConverterOptions
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.logging import get_logger

logger = get_logger("channel.xliff2eml")

# Archive-root entries OPP writes for an EML skeleton. The .eml is verbatim and
# the sidecar is the only extra entry; neither is ever added to or rewritten.
_MESSAGE_ENTRY = "message.eml"
_MAP_ENTRY = "eml_map.json"

# Sidecar schema this channel understands. Version 1 is the paragraph-mapped
# form; any other version falls back to the single-unit whole-body rule.
_MAP_VERSION = 1

# OPP joins paragraphs with the sidecar's ``joiner`` when it extracts. Splitting
# is deliberately blank-line based (a blank line is a line containing only
# optional spaces/tabs), which is the inverse of the "\n\n" joiner and tolerates
# CRLF and trailing indentation without shifting paragraph indices.
_PARAGRAPH_SPLIT = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)")

# Content-transfer-encodings this channel can re-encode without touching any
# header. Anything else is reported instead of guessed.
_SUPPORTED_CTE = {"base64", "quoted-printable", "7bit", "8bit", "binary"}

_LINE_SEPS = {"\n", "\r\n"}


@dataclass(frozen=True)
class _EmlBody:
    """The body part the sidecar offered for translation."""

    walk_index: int
    content_type: str
    charset: Optional[str]
    content_transfer_encoding: Optional[str]
    line_sep: str
    unit_ids: tuple[str, ...]
    joiner: str


@dataclass(frozen=True)
class _EmlMap:
    """The validated ``eml_map.json`` sidecar."""

    version: int
    body: _EmlBody
    headers_rebuilt: bool


def _local_name(element: etree._Element) -> str:
    """Namespace-agnostic tag name (XLIFF 1.2 and 2.0 differ only here)."""
    return etree.QName(element).localname


def _element_text(element: etree._Element | None) -> str | None:
    """Full text of a ``<source>``/``<target>``, inline runs included."""
    if element is None:
        return None
    return "".join(element.itertext())


def _parse_xliff_units(
    path: Path,
) -> dict[str, tuple[str | None, str | None]]:
    """Return ``{trans-unit id: (source, target)}`` from an XLIFF file.

    Namespace-agnostic and XXE-hardened. Units without a ``<source>`` are kept
    (with ``None``) so the caller can treat them as untranslated; units without
    a ``<target>`` also map to ``None`` and leave the original text in place.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.parse(str(path), parser)
    units: dict[str, tuple[str | None, str | None]] = {}
    for element in tree.getroot().iter():
        if not isinstance(element.tag, str) or _local_name(element) != "trans-unit":
            continue
        unit_id = element.get("id")
        if unit_id is None:
            continue
        source: str | None = None
        target: str | None = None
        for child in element:
            if not isinstance(child.tag, str):
                continue
            name = _local_name(child)
            if name == "source" and source is None:
                source = _element_text(child)
            elif name == "target" and target is None:
                target = _element_text(child)
        units[unit_id] = (source, target)
    return units


def _validate_str(payload: dict[str, Any], key: str, where: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{where}: {key!r} is not a string: {value!r}")
    return value


def _parse_body(raw: Any) -> _EmlBody:
    """Validate the sidecar's ``body`` object."""
    if not isinstance(raw, dict):
        raise ValueError(
            f"{_MAP_ENTRY}: 'body' is not a JSON object"
        )

    walk_index = raw.get("walk_index")
    if not isinstance(walk_index, int) or isinstance(walk_index, bool):
        raise ValueError(
            f"{_MAP_ENTRY}: body.walk_index is not an integer: {walk_index!r}"
        )
    if walk_index < 0:
        raise ValueError(
            f"{_MAP_ENTRY}: body.walk_index must be >= 0, got {walk_index}"
        )

    content_type = _validate_str(raw, "content_type", f"{_MAP_ENTRY}: body")

    charset = raw.get("charset")
    if charset is not None and not isinstance(charset, str):
        raise ValueError(
            f"{_MAP_ENTRY}: body.charset is not a string: {charset!r}"
        )

    cte = raw.get("content_transfer_encoding")
    if cte is not None and not isinstance(cte, str):
        raise ValueError(
            f"{_MAP_ENTRY}: body.content_transfer_encoding is not a string: {cte!r}"
        )

    line_sep = raw.get("line_sep")
    if line_sep not in _LINE_SEPS:
        raise ValueError(
            f"{_MAP_ENTRY}: body.line_sep must be '\\n' or '\\r\\n', "
            f"got {line_sep!r}"
        )

    raw_ids = raw.get("unit_ids")
    if not isinstance(raw_ids, list) or not all(
        isinstance(unit_id, str) for unit_id in raw_ids
    ):
        raise ValueError(
            f"{_MAP_ENTRY}: body.unit_ids is not a list of strings: {raw_ids!r}"
        )

    joiner = raw.get("joiner")
    if not isinstance(joiner, str):
        raise ValueError(
            f"{_MAP_ENTRY}: body.joiner is not a string: {joiner!r}"
        )

    return _EmlBody(
        walk_index=walk_index,
        content_type=content_type,
        charset=charset,
        content_transfer_encoding=cte,
        line_sep=line_sep,
        unit_ids=tuple(raw_ids),
        joiner=joiner,
    )


def _parse_sidecar(payload: Any, source_name: str) -> _EmlMap:
    """Validate the whole sidecar up front; raise ``ValueError`` on any break."""
    if not isinstance(payload, dict):
        raise ValueError(
            f"{_MAP_ENTRY} in {source_name} is not a JSON object"
        )
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(
            f"{_MAP_ENTRY} in {source_name} has no integer 'version': {version!r}"
        )
    body = _parse_body(payload.get("body"))
    headers_rebuilt = payload.get("headers_rebuilt", False)
    if not isinstance(headers_rebuilt, bool):
        raise ValueError(
            f"{_MAP_ENTRY} in {source_name}: 'headers_rebuilt' is not a "
            f"boolean: {headers_rebuilt!r}"
        )
    return _EmlMap(version=version, body=body, headers_rebuilt=headers_rebuilt)


def _read_eml_skeleton(template: Path) -> tuple[bytes, _EmlMap]:
    """Return the original message bytes and sidecar from an EML skeleton."""
    if template.suffix.lower() != ".zip":
        raise ValueError(
            f"{template.name} is a bare email, not an EML skeleton: EML "
            f"backfill needs OPP's <stem>.skeleton.zip, whose {_MAP_ENTRY} "
            f"sidecar says which body part was offered for translation. "
            f"Without it nothing can be written without guessing — re-extract "
            f"with `opp <file>.eml --target-format both` and pass the skeleton."
        )

    with zipfile.ZipFile(template, "r") as archive:
        names = archive.namelist()
        missing = [
            entry for entry in (_MESSAGE_ENTRY, _MAP_ENTRY) if entry not in names
        ]
        if missing:
            listed = ", ".join(sorted(names)[:3]) or "nothing"
            raise ValueError(
                f"Skeleton zip {template.name} is missing "
                f"{', '.join(missing)} (it holds {listed}), so it is not an "
                f"EML skeleton from OPP. Re-extract with "
                f"`opp <file>.eml --target-format both`."
            )
        try:
            payload = json.loads(archive.read(_MAP_ENTRY).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"{_MAP_ENTRY} in {template.name} is not valid JSON: {exc}"
            ) from exc
        raw = archive.read(_MESSAGE_ENTRY)
    return raw, _parse_sidecar(payload, template.name)


def _map_paragraphs_v1(
    body_text: str,
    body: _EmlBody,
    units: dict[str, tuple[str | None, str | None]],
) -> tuple[str, int, int]:
    """Map version-1 sidecars paragraph by paragraph.

    Returns ``(new_text, units_written, untranslated)``. A count mismatch or a
    source that cannot be found verbatim raises ``ValueError``: guessing which
    translation belongs to which paragraph would silently reorder the message.

    The replacement is a substring swap, not a whole-paragraph overwrite. OPP
    offers only a paragraph's *translatable* text, and its splitter deliberately
    drops structure that lives inside the same blank-line block: an RFC 3676
    signature's ``-- `` separator line, for example, is not part of the
    ``Signature`` paragraph. Overwriting the whole block would delete that
    separator from the output; swapping just the source substring keeps it.

    OPP normalises line endings to ``\\n`` before building the XLIFF, while the
    decoded MIME body may still carry ``\\r\\n``, so the source is matched in
    both line-ending forms and the target inherits the paragraph's own ending.
    """
    paragraphs = _PARAGRAPH_SPLIT.split(body_text)
    if len(paragraphs) == len(body.unit_ids) + 1 and not paragraphs[-1].strip():
        paragraphs = paragraphs[:-1]
    if len(paragraphs) != len(body.unit_ids):
        raise ValueError(
            f"the skeleton body splits into {len(paragraphs)} paragraph(s) but "
            f"{_MAP_ENTRY} lists {len(body.unit_ids)} trans-unit id(s); the "
            f"paragraph/unit mapping is ambiguous, so no file was written"
        )

    new_paragraphs: list[str] = []
    written = 0
    untranslated = 0
    for index, original in enumerate(paragraphs):
        unit = units.get(body.unit_ids[index])
        source = unit[0] if unit is not None else None
        target = unit[1] if unit is not None else None
        if source is None or target is None:
            new_paragraphs.append(original)
            untranslated += 1
            continue
        source_lf = source.replace("\r\n", "\n").replace("\r", "\n")
        if not source_lf:
            new_paragraphs.append(original)
            untranslated += 1
            continue
        line_sep = "\r\n" if "\r\n" in original else "\n"
        candidates = [source_lf]
        if line_sep == "\r\n":
            candidates.append(source_lf.replace("\n", "\r\n"))
        matched = next((c for c in candidates if c in original), None)
        if matched is None:
            raise ValueError(
                f"trans-unit {body.unit_ids[index]!r} source text cannot be "
                f"located verbatim in paragraph {index} of the body, so the "
                f"translation cannot be injected without guessing"
            )
        if line_sep == "\r\n":
            target = target.replace("\r\n", "\n").replace("\n", "\r\n")
        new_paragraphs.append(original.replace(matched, target, 1))
        written += 1
    return body.joiner.join(new_paragraphs), written, untranslated


def _map_whole_body_fallback(
    body_text: str,
    map_data: _EmlMap,
    units: dict[str, tuple[str | None, str | None]],
) -> tuple[str, int, int]:
    """Unknown sidecar version: replace the whole body with one trans-unit."""
    if len(units) != 1:
        raise ValueError(
            f"sidecar version {map_data.version} has no paragraph mapping, so "
            f"this channel can only replace the whole body from exactly one "
            f"trans-unit, but the XLIFF has {len(units)} unit(s)"
        )
    source, target = next(iter(units.values()))
    if source is None or target is None:
        return body_text, 0, 1
    return target, 1, 0


class XLIFF2EMLConverter(BaseConverter):
    """XLIFF to EML backfill converter (format-preserving).

    The skeleton's ``message.eml`` is parsed, the single body part named by
    ``eml_map.json`` is rewritten with the XLIFF translations, and the new body
    is spliced under the original header block. Attachments and the
    unselected alternative body part are never touched.
    """

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
    ) -> None:
        """Initialize the XLIFF to EML converter.

        Args:
            manifest: OPP manifest.json metadata
            frontmatter: OL YAML frontmatter metadata
        """
        super().__init__(manifest, frontmatter)

    @property
    def supported_format(self) -> str:
        """Supported output format."""
        return "EML"

    def validate_input(self, input_path: Path | str) -> bool:
        """Accept an existing skeleton ``.zip`` only.

        A bare ``.eml`` has no ``eml_map.json`` sidecar, so there is no way to
        know which MIME part was extracted for translation; it is rejected and
        :meth:`convert` explains how to re-extract.

        Args:
            input_path: Path to OPP's ``<stem>.skeleton.zip``

        Returns:
            True if the input exists and ends in ``.zip``
        """
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() == ".zip"

    def convert(  # type: ignore[override]  # backfill channels take the XLIFF path too
        self,
        input_path: Path | str,
        xliff_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        """Write the XLIFF translations into the skeleton's selected body part.

        Args:
            input_path: Path to OPP's ``<stem>.skeleton.zip``
            xliff_path: Path to the translated XLIFF file
            output_path: Path for the output ``.eml`` file
            options: Converter options (unused: the message's own charset and
                line separator govern serialization)

        Returns:
            ConversionResult. Every failure leaves ``output_path`` untouched.
        """
        skeleton = Path(input_path)
        xliff = Path(xliff_path)
        output = Path(output_path)

        def failure(code: str, message: str) -> ConversionResult:
            return ConversionResult(
                output_path=output,
                success=False,
                errors=[ErrorDetail(code=code, message=message)],
            )

        try:
            raw, map_data = _read_eml_skeleton(skeleton)
        except Exception as e:
            logger.warning(
                "Failed to read EML skeleton %s: %s", skeleton, e, exc_info=True
            )
            return failure(
                "EML_SKELETON_ERROR", f"Failed to read EML skeleton: {e}"
            )

        if not xliff.exists():
            return failure("XLIFF_NOT_FOUND", f"XLIFF file not found: {xliff}")
        try:
            units = _parse_xliff_units(xliff)
        except Exception as e:
            logger.warning(
                "Failed to parse XLIFF %s: %s", xliff, e, exc_info=True
            )
            return failure("XLIFF_PARSE_ERROR", f"Failed to parse XLIFF: {e}")

        body = map_data.body
        line_sep = body.line_sep
        line_sep_bytes = b"\r\n" if line_sep == "\r\n" else b"\n"
        blank = line_sep_bytes + line_sep_bytes
        orig_head: bytes | None = None
        if blank in raw:
            orig_head = raw[: raw.find(blank) + len(blank)]

        parse_policy = email.policy.default
        generator_policy = parse_policy.clone(
            refold_source="none", linesep=line_sep
        )

        try:
            message = email.parser.BytesParser(policy=parse_policy).parsebytes(raw)
        except Exception as e:
            logger.warning(
                "Failed to parse the skeleton message %s: %s",
                skeleton,
                e,
                exc_info=True,
            )
            return failure(
                "EML_SKELETON_ERROR", f"Failed to parse message.eml: {e}"
            )

        boundary_before = message.get_boundary()

        parts = list(message.walk())
        if body.walk_index >= len(parts):
            return failure(
                "EML_MAP_MISMATCH",
                f"{_MAP_ENTRY} points at walk_index {body.walk_index} but the "
                f"message has only {len(parts)} part(s)",
            )
        part = parts[body.walk_index]
        if part.get_content_type() != body.content_type:
            return failure(
                "EML_MAP_MISMATCH",
                f"{_MAP_ENTRY} says walk_index {body.walk_index} is "
                f"'{body.content_type}' but that part is actually "
                f"'{part.get_content_type()}'",
            )

        try:
            body_text = part.get_content()
        except Exception as e:
            logger.warning(
                "Failed to decode the body part of %s: %s",
                skeleton,
                e,
                exc_info=True,
            )
            return failure(
                "EML_BODY_DECODE_ERROR", f"Failed to decode the body part: {e}"
            )
        if not isinstance(body_text, str):
            return failure(
                "EML_BODY_DECODE_ERROR",
                f"The body part decoded to {type(body_text).__name__}, not text",
            )

        warnings: list[WarningDetail] = []
        units_written = 0
        untranslated = 0
        if map_data.version == _MAP_VERSION:
            try:
                new_text, units_written, untranslated = _map_paragraphs_v1(
                    body_text, body, units
                )
            except ValueError as e:
                logger.warning("EML backfill mapping rejected: %s", e)
                return failure("EML_UNIT_MISMATCH", str(e))
        else:
            try:
                new_text, units_written, untranslated = _map_whole_body_fallback(
                    body_text, map_data, units
                )
            except ValueError as e:
                logger.warning("EML backfill fallback rejected: %s", e)
                return failure(
                    "EML_SKELETON_ERROR",
                    f"Cannot map the translation onto the skeleton: {e}",
                )
            warnings.append(WarningDetail(
                code="SIDECAR_VERSION_FALLBACK",
                message=(
                    f"sidecar version {map_data.version} is not "
                    f"{_MAP_VERSION}; the whole body was replaced from a single "
                    f"trans-unit instead of mapped paragraph by paragraph"
                ),
            ))

        charset = body.charset or part.get_content_charset() or "utf-8"
        cte = (part.get("Content-Transfer-Encoding") or "7bit").strip().lower()
        if cte not in _SUPPORTED_CTE:
            return failure(
                "EML_UNSUPPORTED_CTE",
                f"The selected body part uses Content-Transfer-Encoding "
                f"'{cte}', which this channel cannot re-encode without "
                f"rebuilding headers",
            )

        try:
            encoded = new_text.encode(charset)
        except (LookupError, UnicodeEncodeError):
            charset = "utf-8"
            encoded = new_text.encode(charset)
            part.set_param("charset", charset)
            part_header_changed = True
        else:
            part_header_changed = False

        if cte == "base64":
            part.set_payload(base64.encodebytes(encoded).decode("ascii"))
        elif cte == "quoted-printable":
            part.set_payload(quopri.encodestring(encoded).decode("ascii"))
        elif encoded.isascii() and not part_header_changed:
            part.set_payload(new_text)
        else:
            # 7bit/8bit/binary cannot carry a non-ASCII translation: the
            # bytes generator writes a str payload as ASCII and raises. Base64
            # keeps the message valid and only rewrites this part's own CTE
            # header, which lives below the top-level header block.
            part.set_payload(base64.encodebytes(encoded).decode("ascii"))
            if part.get("Content-Transfer-Encoding") is None:
                part["Content-Transfer-Encoding"] = "base64"
            else:
                part.replace_header("Content-Transfer-Encoding", "base64")
            part_header_changed = True

        try:
            serialized = message.as_bytes(policy=generator_policy)
        except Exception as e:
            logger.warning(
                "Failed to serialize the EML message: %s", e, exc_info=True
            )
            return failure("EML_SERIALIZE_ERROR", f"Failed to serialize: {e}")

        if message.get_boundary() != boundary_before:
            return failure(
                "EML_BOUNDARY_CHANGED",
                "The multipart boundary changed during serialization, so the "
                "message would no longer match its own Content-Type; nothing "
                "was written",
            )

        rebuild_headers = map_data.headers_rebuilt or orig_head is None
        if part_header_changed and not message.is_multipart():
            # The selected part is the message itself, so the CTE/charset
            # header it just gained sits inside the block orig_head locks.
            rebuild_headers = True

        if rebuild_headers or orig_head is None:
            output_bytes = serialized
            warnings.append(WarningDetail(
                code="HEADERS_REBUILT",
                message=(
                    "the message was re-serialized instead of splicing the "
                    "original header block: the skeleton had no header/body "
                    "separator, or the body needed a new "
                    "Content-Transfer-Encoding/charset; header bytes are not "
                    "guaranteed identical"
                ),
            ))
        else:
            split_at = serialized.find(blank)
            if split_at < 0:
                return failure(
                    "EML_SERIALIZE_ERROR",
                    "serialized message has no header/body separator, so the "
                    "original header block cannot be preserved",
                )
            new_body = serialized[split_at + len(blank):]
            output_bytes = orig_head + new_body

        if untranslated:
            warnings.append(WarningDetail(
                code="UNITS_UNTRANSLATED",
                message=(
                    f"{untranslated} paragraph(s) had no translated <target> "
                    f"in the XLIFF; their text keeps the original"
                ),
            ))

        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(output_bytes)
        except OSError as e:
            logger.warning("Failed to write output EML: %s", e, exc_info=True)
            return failure("EML_WRITE_ERROR", f"Failed to write output EML: {e}")

        logger.info(
            "EML backfill wrote %d trans-unit(s) into part %d (%d untranslated)",
            units_written,
            body.walk_index,
            untranslated,
        )
        return ConversionResult(
            output_path=output,
            success=True,
            warnings=warnings,
            metadata={
                "source_format": "XLIFF",
                "target_format": "EML",
                "skeleton": str(skeleton),
                "xliff": str(xliff),
                "units_written": units_written,
                "units_untranslated": untranslated,
                "walk_index": body.walk_index,
                "headers_rebuilt": rebuild_headers,
            },
        )
