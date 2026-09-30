"""Issue A: positional table-cell backfill for xliff2docx.

OPP emits one trans-unit per table cell with resname
``table_{t}_r{r}_c{c}``.  The old text-matching fallback looks a cell's text
up in a ``text -> <w:t>`` dict, so a value repeated across cells can only be
written into ONE of them.  These tests pin the deterministic positional
write-back and the resname parser contract.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from lxml import etree

from orf.channels.xliff2docx import XLIFF2DOCXConverter
from orf.channels.xliff2docx.matcher import backfill_by_table_cell
from orf.channels.xliff2docx.parser import _parse_resname

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _document_xml(body_inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}">'
        f"<w:body>{body_inner}</w:body>"
        "</w:document>"
    )


def _cell(text: str, runs: list[str] | None = None) -> str:
    parts = runs if runs is not None else [text]
    run_xml = "".join(
        f'<w:r><w:t xml:space="preserve">{p}</w:t></w:r>' for p in parts
    )
    return f"<w:tc><w:p>{run_xml}</w:p></w:tc>"


# 2x3 table; the value column repeats "IP67" in rows 0 and 1.
IP67_BODY = (
    "<w:p><w:r><w:t>Body paragraph</w:t></w:r></w:p>"
    "<w:tbl>"
    "<w:tr>"
    + _cell("IP67")
    + _cell("Yes")
    + _cell("N/A")
    + "</w:tr>"
    "<w:tr>"
    + _cell("IP67")
    + _cell("No")
    + _cell("0.5 ms")
    + "</w:tr>"
    "</w:tbl>"
)
IP67_DOCUMENT = _document_xml(IP67_BODY)


def _xliff(units: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        '  <file original="t" source-language="en" target-language="zh">\n'
        "    <body>\n" + units + "\n    </body>\n  </file>\n</xliff>\n"
    )


def _unit(uid: str, resname: str, source: str, target: str) -> str:
    return (
        f'      <trans-unit xml:space="preserve" id="{uid}" resname="{resname}">\n'
        f'        <source xml:space="preserve">{source}</source>\n'
        f'        <target xml:space="preserve">{target}</target>\n'
        "      </trans-unit>"
    )


def _run_docx_backfill(
    tmp_path: Path, document_xml: str, xliff_content: str
) -> etree._Element:
    """Build a minimal DOCX skeleton, run convert(), return the output root."""
    skeleton = tmp_path / "skeleton.docx"
    with zipfile.ZipFile(skeleton, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("[Content_Types].xml", "<ContentTypes/>")
        zf.writestr("word/_rels/document.xml.rels", "<Relationships/>")

    xliff = tmp_path / "t.xlf"
    xliff.write_text(xliff_content, encoding="utf-8")
    output = tmp_path / "out.docx"

    mock_loader = MagicMock()
    mock_loader.load_skeleton.return_value = {"xml": document_xml}
    captured: dict[str, str] = {}
    mock_loader.repack_docx.side_effect = (
        lambda op, xml, *a, **k: captured.update(xml=xml) or op
    )

    with patch(
        "orf.channels.xliff2docx.SkeletonLoader", return_value=mock_loader
    ):
        result = XLIFF2DOCXConverter().convert(skeleton, xliff, output)

    assert result.success is True, result.errors
    assert "xml" in captured, "repack_docx was not called"
    return etree.fromstring(captured["xml"].encode("utf-8"))


def _cells(root: etree._Element) -> dict[tuple[int, int, int], etree._Element]:
    out: dict[tuple[int, int, int], etree._Element] = {}
    for t_idx, tbl in enumerate(root.iter(f"{{{W_NS}}}tbl")):
        for r_idx, tr in enumerate(tbl.findall(f"{{{W_NS}}}tr")):
            for c_idx, tc in enumerate(tr.findall(f"{{{W_NS}}}tc")):
                out[(t_idx, r_idx, c_idx)] = tc
    return out


def _cell_text(tc: etree._Element) -> str:
    return "".join(t.text or "" for t in tc.iter(f"{{{W_NS}}}t"))


def _multi_paragraph_cell(paragraphs: list[str]) -> str:
    """A ``w:tc`` whose direct children are one ``w:p`` per string."""
    inner = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    return f"<w:tc>{inner}</w:tc>"


def _paragraph_texts(tc: etree._Element) -> list[str]:
    """One text entry per DIRECT ``w:p`` child of the cell."""
    return [
        "".join(t.text or "" for t in p.xpath(".//w:t", namespaces={"w": W_NS}))
        for p in tc.findall(f"{{{W_NS}}}p")
    ]


def _table_with_cell(cell_xml: str) -> str:
    return "<w:tbl><w:tr>" + cell_xml + "</w:tr></w:tbl>"


class TestParseResname:
    """The resname parser must return the 3-tuple and never raise."""

    def test_table_resname_parsed(self):
        assert _parse_resname("table_0_r1_c2") == (None, None, (0, 1, 2, None))

    def test_table_resname_with_para_parsed(self):
        assert _parse_resname("table_0_r0_c0_para0") == (
            None, None, (0, 0, 0, 0),
        )
        assert _parse_resname("table_0_r0_c0_para12") == (
            None, None, (0, 0, 0, 12),
        )

    def test_malformed_table_resname_is_none(self):
        assert _parse_resname("table_x_r1_c0") == (None, None, None)
        assert _parse_resname("table_0_r0_c0_paraX") == (None, None, None)

    def test_para_index_unchanged(self):
        assert _parse_resname("para_index_3") == (3, None, None)

    def test_non_body_unchanged(self):
        assert _parse_resname("non_body_5") == (None, 5, None)

    def test_none_resname(self):
        assert _parse_resname(None) == (None, None, None)


class TestPositionalTableCellBackfill:
    """Duplicate cell text must still receive each unit's OWN target."""

    def test_duplicate_cell_text_each_gets_own_target(self, tmp_path: Path):
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0", "IP67", "ZH-PRIMARY-IP67"),
                    _unit("2", "table_0_r1_c0", "IP67", "ZH-SECONDARY-IP67"),
                    _unit("3", "table_0_r0_c1", "Yes", "ZH-YES"),
                ]
            )
        )

        root = _run_docx_backfill(tmp_path, IP67_DOCUMENT, xliff)
        cells = _cells(root)

        assert _cell_text(cells[(0, 0, 0)]) == "ZH-PRIMARY-IP67"
        assert _cell_text(cells[(0, 1, 0)]) == "ZH-SECONDARY-IP67"
        assert _cell_text(cells[(0, 0, 1)]) == "ZH-YES"

    def test_source_runs_cleared_no_leftovers(self, tmp_path: Path):
        body = (
            "<w:tbl><w:tr>"
            + _cell("ignored", runs=["IP", "67"])
            + "</w:tr></w:tbl>"
        )
        xliff = _xliff(_unit("1", "table_0_r0_c0", "IP67", "翻译"))
        root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)
        assert _cell_text(_cells(root)[(0, 0, 0)]) == "翻译"

    def test_out_of_range_cell_falls_back_without_crashing(self, tmp_path: Path):
        # table_9 does not exist → positional path declines, conversion still
        # succeeds (falls through to the text-match path).
        xliff = _xliff(_unit("1", "table_9_r0_c0", "No", "ZH-NO"))
        root = _run_docx_backfill(tmp_path, IP67_DOCUMENT, xliff)
        cells = _cells(root)
        # Text-match fallback applied the unique "No" translation.
        assert _cell_text(cells[(0, 1, 1)]) == "ZH-NO"


class TestNestedTableNotDescended:
    """Cell resolution uses direct ``./w:p`` paragraphs only."""

    def test_nested_table_untouched(self, tmp_path: Path):
        nested = (
            "<w:tbl><w:tr>"
            + _cell("NESTED")
            + "</w:tr></w:tbl>"
        )
        body = (
            "<w:tbl><w:tr>"
            + f"<w:tc><w:p><w:r><w:t>OUTER</w:t></w:r></w:p>{nested}</w:tc>"
            + "</w:tr></w:tbl>"
        )
        xliff = _xliff(_unit("1", "table_0_r0_c0", "OUTER", "ZH-OUTER"))
        root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)

        tcs = list(root.iter(f"{{{W_NS}}}tc"))
        outer, nested = tcs[0], tcs[1]

        outer_direct = "".join(
            t.text or ""
            for p in outer.findall(f"{{{W_NS}}}p")
            for t in p.iter(f"{{{W_NS}}}t")
        )
        assert outer_direct == "ZH-OUTER"

        nested_text = "".join(t.text or "" for t in nested.iter(f"{{{W_NS}}}t"))
        assert nested_text == "NESTED"


class TestParagraphScopedTableCellBackfill:
    """OPP#80 Wave 1: ``_para{p}`` units write exactly one cell paragraph."""

    def test_para_unit_writes_only_target_paragraph(self, tmp_path: Path):
        body = _table_with_cell(_multi_paragraph_cell(["Yes", "tail"]))
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0_para0", "Yes", "ZH-YES"),
                    _unit("2", "table_0_r0_c0_para1", "tail", "ZH-TAIL"),
                ]
            )
        )
        root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)
        assert _paragraph_texts(_cells(root)[(0, 0, 0)]) == ["ZH-YES", "ZH-TAIL"]

    def test_para_unit_leaves_sibling_paragraph_untouched(self, tmp_path: Path):
        body = _table_with_cell(_multi_paragraph_cell(["A", "B", "C"]))
        xliff = _xliff(_unit("1", "table_0_r0_c0_para1", "B", "ZH-B"))
        root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)
        assert _paragraph_texts(_cells(root)[(0, 0, 0)]) == ["A", "ZH-B", "C"]

    def test_para_out_of_range_skips_and_warns(self, tmp_path: Path, caplog):
        body = _table_with_cell(_multi_paragraph_cell(["Yes", "tail"]))
        doc = _document_xml(body)

        direct_root = etree.fromstring(doc.encode("utf-8"))
        with caplog.at_level(logging.WARNING):
            applied = backfill_by_table_cell(
                direct_root, 0, 0, 0, "ZH-X",
                lambda _t: [], lambda t: t, 9,
            )
        assert applied is False
        assert _paragraph_texts(_cells(direct_root)[(0, 0, 0)]) == ["Yes", "tail"]
        assert any("out of range" in r.getMessage() for r in caplog.records)

        caplog.clear()
        xliff = _xliff(_unit("1", "table_0_r0_c0_para9", "Yes", "ZH-X"))
        with caplog.at_level(logging.WARNING):
            root = _run_docx_backfill(tmp_path, doc, xliff)
        assert _paragraph_texts(_cells(root)[(0, 0, 0)]) == ["Yes", "tail"]
        assert any("out of range" in r.getMessage() for r in caplog.records)

    def test_para_duplicate_keeps_first_and_warns(self, tmp_path: Path, caplog):
        body = _table_with_cell(_multi_paragraph_cell(["A", "B"]))
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0_para0", "A", "FIRST"),
                    _unit("2", "table_0_r0_c0_para0", "A", "SECOND"),
                ]
            )
        )
        with caplog.at_level(logging.WARNING):
            root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)
        paragraphs = _paragraph_texts(_cells(root)[(0, 0, 0)])
        assert paragraphs[0] == "FIRST"
        assert "SECOND" not in "".join(paragraphs)
        assert any("duplicate" in r.getMessage().lower() for r in caplog.records)

    def test_mixed_bare_and_para_never_falls_back_to_whole_cell(
        self, tmp_path: Path, caplog
    ):
        body = _table_with_cell(_multi_paragraph_cell(["A", "B", "C"]))
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0", "ABC", "ZH-WHOLE"),
                    _unit("2", "table_0_r0_c0_para1", "B", "ZH-B"),
                ]
            )
        )
        with caplog.at_level(logging.WARNING):
            root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)
        paragraphs = _paragraph_texts(_cells(root)[(0, 0, 0)])
        assert paragraphs == ["A", "ZH-B", "C"]
        assert "ZH-WHOLE" not in "".join(paragraphs)
        assert any("bare" in r.getMessage().lower() for r in caplog.records)

    def test_bare_resname_unchanged_regression(self, tmp_path: Path):
        body = _table_with_cell(_multi_paragraph_cell(["A", "B"]))
        xliff = _xliff(_unit("1", "table_0_r0_c0", "A\nB", "ZH-WHOLE"))
        root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)
        # Legacy whole-cell: run 0 gets the target, the tail run is cleared.
        assert _paragraph_texts(_cells(root)[(0, 0, 0)]) == ["ZH-WHOLE", ""]

    def test_para_on_runless_paragraph_skips_and_warns(
        self, tmp_path: Path, caplog
    ):
        runless_cell = (
            "<w:tc>"
            '<w:p><w:r><w:t xml:space="preserve">A</w:t></w:r></w:p>'
            "<w:p><w:r><w:rPr/></w:r></w:p>"
            "</w:tc>"
        )
        body = _table_with_cell(runless_cell)
        xliff = _xliff(_unit("1", "table_0_r0_c0_para1", "q", "ZH"))
        with caplog.at_level(logging.WARNING):
            root = _run_docx_backfill(tmp_path, _document_xml(body), xliff)
        assert _paragraph_texts(_cells(root)[(0, 0, 0)]) == ["A", ""]
        assert any("no runs" in r.getMessage() for r in caplog.records)

    def test_para_bx_ex_formats_target_paragraph_only(self):
        body = _table_with_cell(_multi_paragraph_cell(["A", "B", "C"]))
        root = etree.fromstring(_document_xml(body).encode("utf-8"))

        formatted = etree.Element(f"{{{W_NS}}}r")
        etree.SubElement(formatted, f"{{{W_NS}}}t").text = "FMT"

        applied = backfill_by_table_cell(
            root, 0, 0, 0, '<bx id="1"/>B<ex id="1"/>',
            lambda _t: [formatted], lambda t: t, 1,
        )

        assert applied is True
        paras = _cells(root)[(0, 0, 0)].findall(f"{{{W_NS}}}p")
        assert [t.text for t in paras[0].iter(f"{{{W_NS}}}t")] == ["A"]
        assert [t.text for t in paras[2].iter(f"{{{W_NS}}}t")] == ["C"]
        assert "FMT" in [t.text for t in paras[1].iter(f"{{{W_NS}}}t")]
