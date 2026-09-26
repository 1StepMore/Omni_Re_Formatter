"""LEAK-FIX (2026-06-08): regression tests for XLIFF bx/ex tag leak into final DOCX.

Bug surfaced during the 2026-06-08 production pipeline run on
``爱上海尔_第二章_全球创牌 - E2E测试专用.docx``: the OL preserved
inline ``<bx id="1" type="bold"/>`` / ``<ex id="1"/>`` markers in the
target as XML entities (because the LLM produced them as literal
characters in its output). When ORF read the target via
``target_el.itertext()`` and wrote it verbatim into ``<w:t>``, the user
saw ``<bx id="1" type="bold"/>Chapter 2<ex id="1"/>`` in the final
DOCX.

The leak survived THREE previous fix attempts because the
position-based backfill path (``_backfill_by_position`` — added in
PR3 / Phase B.2) bypassed the inline-formatting code path that was
fixed in commit ``c64d3f9`` (``_build_formatted_runs`` /
``_backfill_split_runs``).

These tests pin the contract:
  1. Position-based backfill MUST convert literal ``<bx>``/``<ex>``
     in the target into proper DOCX ``<w:rPr>`` formatting.
  2. As a defense-in-depth safety net, every backfill path that
     writes ``target_text`` to a ``<w:t>`` MUST strip any remaining
     ``<bx>``/``<ex>`` markup via ``_strip_inline_tags``.
  3. The end-to-end production XLIFF must produce a final DOCX
     whose ``<w:t>`` content never contains ``<bx`` or ``<ex``
     substrings.
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

from orf.channels.xliff2docx import (
    XLIFF2DOCXConverter,
    _strip_inline_tags,
)


# ---------------------------------------------------------------------------
# DOCX / XLIFF builders (minimal in-memory fixtures)
# ---------------------------------------------------------------------------

def _build_minimal_docx(docx_path: Path, paragraphs: list[str]) -> Path:
    """Build a minimal DOCX with one ``<w:p>`` per string in ``paragraphs``."""
    import tempfile

    docx_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        (pkg / "word").mkdir(parents=True)
        (pkg / "_rels").mkdir(parents=True)

        body_xml = "\n".join(
            f'    <w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
            for p in paragraphs
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{body_xml}<w:sectPr/></w:body>'
            '</w:document>'
        )
        (pkg / "word" / "document.xml").write_text(document_xml, encoding="utf-8")
        (pkg / "_rels" / ".rels").write_text(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
            encoding="utf-8",
        )
        (pkg / "[Content_Types].xml").write_text(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>',
            encoding="utf-8",
        )

        with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _dirs, files in os.walk(pkg):
                for f in files:
                    fp = os.path.join(root, f)
                    arcname = os.path.relpath(fp, pkg)
                    z.write(fp, arcname)
    return docx_path


def _build_xliff(
    xliff_path: Path,
    trans_units: list[dict[str, str]],
    *,
    source_language: str = "zh",
    target_language: str = "en",
) -> Path:
    """Build a 1.2 XLIFF with the given trans-units.

    Each trans-unit dict must have ``id``, ``source``, ``target``.
    Optional ``resname`` is written verbatim (used for para_index_N).
    """
    unit_xml = []
    for tu in trans_units:
        attrs = [f'id="{tu["id"]}"']
        if "resname" in tu:
            attrs.append(f'resname="{tu["resname"]}"')
        attr_str = " ".join(attrs)
        unit_xml.append(
            f'      <trans-unit xml:space="preserve" {attr_str}>\n'
            f'        <source>{tu["source"]}</source>\n'
            f'        <target>{tu["target"]}</target>\n'
            f'      </trans-unit>'
        )
    body = "".join(unit_xml)
    xliff_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">\n'
        f'  <file original="t" source-language="{source_language}" '
        f'target-language="{target_language}" datatype="plaintext">\n'
        f'    <body>{body}\n    </body>\n'
        '  </file>\n'
        '</xliff>\n'
    )
    xliff_path.write_text(xliff_xml, encoding="utf-8")
    return xliff_path


def _read_docx_xml(docx_path: Path) -> str:
    with zipfile.ZipFile(docx_path) as z:
        return z.read("word/document.xml").decode("utf-8")


def _visible_wt_texts(docx_path: Path) -> list[str]:
    """Return the USER-VISIBLE text of every ``<w:t>``, entities decoded.

    Unlike a raw-XML regex, this parses the DOCX XML with lxml so that
    ``&amp;lt;`` (a literal ``&lt;`` the user would SEE) is distinguished
    from ``&lt;`` (the XML serialization of a real ``<`` character).
    """
    from lxml import etree

    with zipfile.ZipFile(docx_path) as z:
        xml_bytes = z.read("word/document.xml")
    root = etree.fromstring(xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    return [t.text or "" for t in root.xpath("//w:t", namespaces=ns)]


def _assert_no_bx_ex_in_text(out_xml: str) -> None:
    """Assert that no ``<w:t>`` element contains literal ``<bx`` or ``<ex``."""
    w_t_re = re.compile(r"<w:t[^>]*>([^<]*)</w:t>", re.DOTALL)
    leaks = []
    for m in w_t_re.finditer(out_xml):
        text = m.group(1)
        # When text contains literal '<', lxml encodes it as &lt; on output.
        # So we look for the encoded form OR the raw form (defense in depth).
        if "<bx" in text or "<ex" in text:
            leaks.append(text)
        if "&lt;bx" in text or "&lt;ex" in text:
            leaks.append(text)
    assert not leaks, (
        "BUG: literal <bx>/<ex> leaked into <w:t> elements: "
        f"{leaks!r}\n\nFull output XML:\n{out_xml}"
    )


# ---------------------------------------------------------------------------
# Unit tests for the safety helper
# ---------------------------------------------------------------------------

class TestStripInlineTags:
    def test_strips_bx(self):
        assert _strip_inline_tags('<bx id="1" type="bold"/>hello') == "hello"

    def test_strips_ex(self):
        assert _strip_inline_tags('hello<ex id="1"/>') == "hello"

    def test_strips_both_paired(self):
        text = '<bx id="1" type="bold"/>Chapter 2<ex id="1"/>'
        assert _strip_inline_tags(text) == "Chapter 2"

    def test_strips_nested_pairs(self):
        text = (
            '<bx id="1" type="bold"/>A<ex id="1"/>'
            '<bx id="2" type="italic"/>B<ex id="2"/>'
        )
        assert _strip_inline_tags(text) == "AB"

    def test_passthrough_plain_text(self):
        assert _strip_inline_tags("plain text") == "plain text"

    def test_idempotent(self):
        text = "no tags here"
        assert _strip_inline_tags(_strip_inline_tags(text)) == text


# ---------------------------------------------------------------------------
# Regression tests for the production bug
# ---------------------------------------------------------------------------

class TestPositionBasedBackfillNoBxExLeak:
    """LEAK-FIX (2026-06-08): the position-based backfill path
    (``_backfill_by_position``) was the path that survived previous fix
    attempts. These tests pin that the leak is now closed.
    """

    def test_target_with_escaped_bx_ex_produces_no_literal_leak(self, tmp_path: Path):
        """End-to-end: OPP-style XLIFF where the target contains
        ``&lt;bx id="1" type="bold"/&gt;`` (the OL stored LLM-emitted
        markup as XML entities). After ORF backfills, the DOCX must
        contain NO ``<bx``/``<ex`` text — only proper ``<w:rPr>``
        formatting or stripped text.
        """
        # OPP source: book title with bold around 《爱上海尔》
        source = '<bx id="1" type="bold"/>《爱上海尔》<ex id="1"/>'
        # LLM output: same inline markup preserved, stored as entities in XLIFF
        target_with_bx = (
            '&lt;bx id="1" type="bold"/&gt;I Love Shanghai, Er&lt;ex id="1"/&gt;'
        )

        docx_path = _build_minimal_docx(
            tmp_path / "input.docx", paragraphs=["PLACEHOLDER_BODY"]
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "1",
                    "resname": "para_index_0",
                    "source": source,
                    "target": target_with_bx,
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        result = XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)
        assert result.success, f"convert failed: {result.errors}"

        out_xml = _read_docx_xml(output_path)
        _assert_no_bx_ex_in_text(out_xml)

    def test_target_with_bx_ex_applies_bold_rpr(self, tmp_path: Path):
        """The bold formatting from the LLM-emitted ``<bx>`` MUST be
        applied as a real ``<w:rPr><w:b/></w:rPr>`` on the corresponding
        run — not lost in the strip.
        """
        source = '<bx id="1" type="bold"/>《爱上海尔》<ex id="1"/>'
        target_with_bx = (
            '&lt;bx id="1" type="bold"/&gt;I Love Shanghai, Er&lt;ex id="1"/&gt;'
        )

        docx_path = _build_minimal_docx(
            tmp_path / "input.docx", paragraphs=["PLACEHOLDER_BODY"]
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "1",
                    "resname": "para_index_0",
                    "source": source,
                    "target": target_with_bx,
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)

        out_xml = _read_docx_xml(output_path)
        # The run carrying "I Love Shanghai, Er" must have <w:b/> rPr.
        assert "<w:b/>" in out_xml or "<w:b " in out_xml, (
            f"BUG: bold rPr not applied to formatted run.\n"
            f"Output XML:\n{out_xml}"
        )
        # The visible text must be in the doc, un-leaked.
        assert "I Love Shanghai, Er" in out_xml, (
            f"Target text not present in output.\n"
            f"Output XML:\n{out_xml}"
        )

    def test_multi_pair_target_with_escaped_bx_ex(self, tmp_path: Path):
        """Reproduction of the EXACT case from the user's report:
        ``<bx id="1" type="bold"/>Chapter 2<ex id="1"/><bx id="2"
        type="bold"/>{<ex id="2"/><bx id="3" type="bold"/>Haier's<ex
        id="3"/><bx id="4" type="bold"/>Global Brand Creation<ex id="4"/>``
        stored as XML entities.
        """
        source = (
            '<bx id="1" type="bold"/>第二<ex id="1"/>'
            '<bx id="2" type="bold"/>章<ex id="2"/>'
            '<bx id="3" type="bold"/>海尔的<ex id="3"/>'
            '<bx id="4" type="bold"/>全球创牌<ex id="4"/>'
        )
        target_with_bx = (
            '&lt;bx id="1" type="bold"/&gt;Chapter 2'
            '&lt;ex id="1"/&gt;'
            '&lt;bx id="2" type="bold"/&gt;{'
            '&lt;ex id="2"/&gt;'
            '&lt;bx id="3" type="bold"/&gt;Haier\'s'
            '&lt;ex id="3"/&gt;'
            '&lt;bx id="4" type="bold"/&gt;Global Brand Creation'
            '&lt;ex id="4"/&gt;'
        )

        # 2 paragraphs: para_index_0 (title) + para_index_1 (chapter heading)
        docx_path = _build_minimal_docx(
            tmp_path / "input.docx",
            paragraphs=["TITLE_PLACEHOLDER", "CHAPTER_PLACEHOLDER"],
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "2",
                    "resname": "para_index_1",
                    "source": source,
                    "target": target_with_bx,
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)

        out_xml = _read_docx_xml(output_path)
        _assert_no_bx_ex_in_text(out_xml)

        # Verify all four visible segments landed (with their rPr bold).
        for seg in ("Chapter 2", "Haier", "Global Brand Creation"):
            assert seg in out_xml, (
                f"Expected visible text segment {seg!r} in output. "
                f"Output XML:\n{out_xml}"
            )

    def test_plain_target_no_bx_ex_still_applies_source_formatting(self, tmp_path: Path):
        """ORF#33: when the LLM drops bx/ex tags, source-side inline formatting
        must STILL be applied via Phase B.4 post-processing.

        The LLM never sees source formatting — ``parser.py`` strips
        <bx>/<ex> via ``target_el.itertext()`` before passing the
        target text to the LLM — so the LLM has no opportunity to
        "preserve" what it never received. The converter's job is to
        re-apply source-side <w:rPr> formatting to the runs that now
        hold the translated text, mirroring the PPTX post-processing
        pattern at ``xliff2pptx.py:427-440``.
        """
        docx_path = _build_minimal_docx(
            tmp_path / "input.docx", paragraphs=["PLACEHOLDER_BODY"]
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "1",
                    "resname": "para_index_0",
                    "source": '<bx id="1" type="bold"/>《爱上海尔》<ex id="1"/>',
                    "target": "I Love Shanghai, Er",
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        result = XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)
        assert result.success, f"convert failed: {result.errors}"

        out_xml = _read_docx_xml(output_path)
        _assert_no_bx_ex_in_text(out_xml)
        assert "I Love Shanghai, Er" in out_xml
        # ORF#33: source bold formatting must be re-applied as <w:b/> in <w:rPr>
        assert "<w:b/>" in out_xml or "<w:b " in out_xml, (
            f"BUG: source bold not re-applied to runs containing target text.\n"
            f"Output XML:\n{out_xml}"
        )


class TestTextMatchBackfillSafetyStrip:
    """Defense-in-depth: even the non-position-based paths must
    strip any ``<bx>``/``<ex>`` that ends up in ``target_text``.
    """

    def test_text_match_strips_bx_ex(self, tmp_path: Path):
        """No ``para_index`` → text-match path. The OPP source matches
        a paragraph; the LLM target has bx/ex that must not leak.
        """
        # Source: plain "Source Text" (no inline)
        # Target: contains literal bx/ex as text
        docx_path = _build_minimal_docx(
            tmp_path / "input.docx", paragraphs=["Source Text"]
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "1",
                    "source": "Source Text",
                    "target": '<bx id="1" type="bold"/>Translated<ex id="1"/>',
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        result = XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)
        assert result.success, f"convert failed: {result.errors}"

        out_xml = _read_docx_xml(output_path)
        _assert_no_bx_ex_in_text(out_xml)


class TestFuzzyBackfillSafetyStrip:
    """Defense-in-depth: fuzzy match path also has the safety strip."""

    def test_fuzzy_match_strips_bx_ex(self, tmp_path: Path):
        # Source: rephrased vs the docx text — forces fuzzy match path
        docx_path = _build_minimal_docx(
            tmp_path / "input.docx", paragraphs=["Original text in doc"]
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "1",
                    # Whitespace/punct differences → triggers fuzzy
                    "source": "Original  text, in doc.",
                    "target": '<bx id="1" type="bold"/>Fuzzy translated<ex id="1"/>',
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        result = XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)
        assert result.success, f"convert failed: {result.errors}"

        out_xml = _read_docx_xml(output_path)
        _assert_no_bx_ex_in_text(out_xml)


class TestDoubleEscapedBxEx:
    """E2E-06b: XLIFF target markup escaped twice (``&amp;lt;bx``).

    lxml resolves the first layer, leaving a literal ``&lt;bx`` string that
    leaks into user-visible DOCX text unless the parse boundary unescapes it.
    """

    def test_double_escaped_bx_ex_produces_no_visible_leak(self, tmp_path: Path):
        source = '<bx id="1" type="bold"/>《爱上海尔》<ex id="1"/>'
        # &amp;lt; resolves once to &lt;, so the converter sees "&lt;bx ...".
        target_double_escaped = (
            '&amp;lt;bx id="1" type="bold"/&amp;gt;I Love Shanghai, Er'
            '&amp;lt;ex id="1"/&amp;gt;'
        )

        docx_path = _build_minimal_docx(
            tmp_path / "input.docx", paragraphs=["PLACEHOLDER_BODY"]
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "1",
                    "resname": "para_index_0",
                    "source": source,
                    "target": target_double_escaped,
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        result = XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)
        assert result.success, f"convert failed: {result.errors}"

        visible = "".join(_visible_wt_texts(output_path))
        assert "&lt;bx" not in visible and "&lt;ex" not in visible, (
            "BUG: double-escaped entity markup leaked into user-visible "
            f"DOCX text: {visible!r}"
        )
        assert "<bx" not in visible and "<ex" not in visible, (
            f"BUG: literal <bx>/<ex> leaked into user-visible text: {visible!r}"
        )
        assert "I Love Shanghai, Er" in visible, (
            f"Translated text missing from output. visible={visible!r}"
        )

    def test_double_escaped_bx_ex_applies_bold_rpr(self, tmp_path: Path):
        source = '<bx id="1" type="bold"/>《爱上海尔》<ex id="1"/>'
        target_double_escaped = (
            '&amp;lt;bx id="1" type="bold"/&amp;gt;I Love Shanghai, Er'
            '&amp;lt;ex id="1"/&amp;gt;'
        )

        docx_path = _build_minimal_docx(
            tmp_path / "input.docx", paragraphs=["PLACEHOLDER_BODY"]
        )
        xliff_path = _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[
                {
                    "id": "1",
                    "resname": "para_index_0",
                    "source": source,
                    "target": target_double_escaped,
                }
            ],
        )
        output_path = tmp_path / "output.docx"

        result = XLIFF2DOCXConverter().convert(docx_path, xliff_path, output_path)
        assert result.success, f"convert failed: {result.errors}"

        out_xml = _read_docx_xml(output_path)
        assert "<w:b/>" in out_xml or "<w:b " in out_xml, (
            f"BUG: bold rPr not applied for double-escaped markup.\n{out_xml}"
        )
        visible = "".join(_visible_wt_texts(output_path))
        assert "I Love Shanghai, Er" in visible


class TestEntityUnescapeScopedToXliff2Docx:
    """E2E-06b scope guard: the double-escape unescape lives ONLY in the
    xliff2docx parser (``parser._target_text``). The PPTX/EPUB/HTML channels
    keep their existing extraction — broadening the unescape to them is a
    deliberate, separately-documented decision, not a drive-by change.
    """

    DOUBLE_ESCAPED_TARGET: str = (
        '&amp;lt;bx id="1" type="bold"/&amp;gt;I Love Shanghai, Er'
        '&amp;lt;ex id="1"/&amp;gt;'
    )

    def _xliff_path(self, tmp_path: Path) -> Path:
        return _build_xliff(
            tmp_path / "input.xlf",
            trans_units=[{
                "id": "1",
                "source": "《爱上海尔》",
                "target": self.DOUBLE_ESCAPED_TARGET,
            }],
        )

    def test_docx_parser_resolves_double_escaped_markup(self, tmp_path: Path):
        from orf.channels.xliff2docx import XLIFF2DOCXConverter

        units = XLIFF2DOCXConverter()._parse_xliff(self._xliff_path(tmp_path))
        target = units[0]["target"]
        assert isinstance(target, str)
        assert '<bx id="1"' in target and '<ex id="1"' in target, (
            f"DOCX parser must unescape double-escaped inline markup; "
            f"target={target!r}"
        )

    def test_pptx_parser_does_not_unescape(self, tmp_path: Path):
        from orf.channels.xliff2pptx import XLIFF2PPTXConverter

        parsed = XLIFF2PPTXConverter()._parse_xliff(self._xliff_path(tmp_path))
        target = parsed["units"][0]["target"]
        assert isinstance(target, str)
        assert "<bx" not in target and "<ex" not in target, (
            f"PPTX parser must stay entity-encoded (scope guard); "
            f"target={target!r}"
        )
        assert "lt;bx" in target, f"PPTX target unexpectedly rewritten: {target!r}"

    def test_epub_parser_does_not_unescape(self, tmp_path: Path):
        from orf.channels.xliff2epub import XLIFF2EPUBConverter

        segments = XLIFF2EPUBConverter()._parse_xliff(self._xliff_path(tmp_path))
        target = segments["1"]
        assert "<bx" not in target and "<ex" not in target, (
            f"EPUB parser must stay entity-encoded (scope guard); "
            f"target={target!r}"
        )
        assert "lt;bx" in target, f"EPUB target unexpectedly rewritten: {target!r}"

    def test_html_parser_does_not_unescape(self, tmp_path: Path):
        from orf.channels.xliff2html.parser import parse_xliff

        translations = parse_xliff(
            self._xliff_path(tmp_path).read_text(encoding="utf-8")
        )
        target = translations["1"]
        assert "<bx" not in target and "<ex" not in target, (
            f"HTML parser must stay entity-encoded (scope guard); "
            f"target={target!r}"
        )
