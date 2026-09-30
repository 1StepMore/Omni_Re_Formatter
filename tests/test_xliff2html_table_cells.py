"""Issue A: positional table-cell backfill for xliff2html.

OPP emits one trans-unit per table cell with resname
``table_{t}_r{r}_c{c}``.  The text-match fallback collapses repeated cell
text, so positional write-back is required.  These tests also pin the no-op
contract when the XLIFF carries no ``table_...`` resname.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lxml import html as lxml_html

from orf.channels.xliff2html import XLIFF2HTMLConverter
from orf.channels.xliff2html.writer import _TABLE_RESNAME_RE, backfill_table_cells
from orf.converters.options import ConverterOptions


HTML_TEMPLATE = """<!DOCTYPE html>
<html><body>
<table>
  <tr><td>IP67</td><td>Yes</td></tr>
  <tr><td>IP67</td><td>No</td></tr>
</table>
</body></html>"""


def _xliff(units: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        '  <file original="t" source-language="en" target-language="zh">\n'
        "    <body>\n" + units + "\n    </body>\n  </file>\n</xliff>\n"
    )


def _unit(uid: str, resname: str, source: str, target: str) -> str:
    return (
        f'      <trans-unit id="{uid}" resname="{resname}">\n'
        f'        <source>{source}</source>\n'
        f'        <target>{target}</target>\n'
        "      </trans-unit>"
    )


def _run(tmp_path: Path, html: str, xliff_content: str) -> str:
    template = tmp_path / "template.html"
    template.write_text(html, encoding="utf-8")
    xlf = tmp_path / "t.xlf"
    xlf.write_text(xliff_content, encoding="utf-8")
    output = tmp_path / "out.html"

    result = XLIFF2HTMLConverter().convert(
        template, xlf, output, options=ConverterOptions()
    )
    assert result.success is True, result.errors
    return output.read_text(encoding="utf-8")


def _cell_texts(html_str: str) -> list[str]:
    tree = lxml_html.fromstring(html_str)
    return ["".join(td.itertext()).strip() for td in tree.xpath("//td")]


class TestHTMLTableCellBackfill:
    """Each duplicated cell must receive its OWN target."""

    def test_duplicate_cell_text_each_gets_own_target(self, tmp_path: Path):
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0", "IP67", "甲-IP67"),
                    _unit("2", "table_0_r1_c0", "IP67", "乙-IP67"),
                ]
            )
        )
        output = _run(tmp_path, HTML_TEMPLATE, xliff)
        assert _cell_texts(output) == ["甲-IP67", "Yes", "乙-IP67", "No"]

    def test_missing_cell_is_skipped_without_raising(self, tmp_path: Path):
        # Sources deliberately absent from the DOM so the legacy text-match
        # path cannot mask the positional result.
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r9_c0", "ZZZ", "甲"),
                    _unit("2", "table_0_r0_c9", "QQQ", "乙"),
                ]
            )
        )
        output = _run(tmp_path, HTML_TEMPLATE, xliff)
        # Neither out-of-range unit writes anything.
        assert _cell_texts(output) == ["IP67", "Yes", "IP67", "No"]

    def test_table_free_xliff_is_noop(self, tmp_path: Path):
        xliff = _xliff(
            _unit("1", "para_index_0", "Unrelated", "翻译")
        )
        output = _run(tmp_path, HTML_TEMPLATE, xliff)
        assert _cell_texts(output) == ["IP67", "Yes", "IP67", "No"]


class TestBackfillTableCellsDirect:
    """Unit-level contract of ``backfill_table_cells``."""

    def test_returns_zero_when_no_table_resname(self):
        root = lxml_html.fromstring(HTML_TEMPLATE)
        xliff = _xliff(_unit("1", "para_index_0", "IP67", "翻译"))
        assert backfill_table_cells(root, {"1": "翻译"}, xliff) == 0
        assert _cell_texts(str(lxml_html.tostring(root, encoding="unicode"))) == [
            "IP67", "Yes", "IP67", "No",
        ]

    def test_attribute_order_agnostic(self):
        # resname BEFORE id must still be parsed.
        root = lxml_html.fromstring(HTML_TEMPLATE)
        xliff = _xliff(
            '<trans-unit resname="table_0_r0_c0" id="7">'
            "<source>IP67</source><target>甲</target></trans-unit>"
        )
        assert backfill_table_cells(root, {"7": "甲"}, xliff) == 1
        assert _cell_texts(str(lxml_html.tostring(root, encoding="unicode")))[0] == "甲"

    def test_strips_inline_tags(self):
        root = lxml_html.fromstring(HTML_TEMPLATE)
        xliff = _xliff(_unit("1", "table_0_r0_c0", "IP67", "甲"))
        # Translator's value with leftover XLIFF inline markup.
        translations = {"1": '<bx id="1"/>甲<ex id="1"/>'}
        assert backfill_table_cells(root, translations, xliff) == 1
        assert _cell_texts(str(lxml_html.tostring(root, encoding="unicode")))[0] == "甲"


class TestTableResnameGrammar:
    """The optional ``_para{p}`` group parses (OPP#80 Wave 0B)."""

    def test_bare_resname_has_no_para_group(self):
        match = _TABLE_RESNAME_RE.match("table_0_r0_c0")
        assert match is not None
        assert match.group(1, 2, 3, 4) == ("0", "0", "0", None)

    def test_para_resname_parses_para_group(self):
        match0 = _TABLE_RESNAME_RE.match("table_0_r0_c0_para0")
        assert match0 is not None
        assert match0.group(1, 2, 3, 4) == ("0", "0", "0", "0")

        match12 = _TABLE_RESNAME_RE.match("table_0_r0_c0_para12")
        assert match12 is not None
        assert match12.group(1, 2, 3, 4) == ("0", "0", "0", "12")

    def test_malformed_para_resname_does_not_match(self):
        assert _TABLE_RESNAME_RE.match("table_0_r0_c0_paraX") is None

    def test_para_resname_still_writes_positional_cell(self):
        # Reaching written == 1 proves the inline 4-group unpack and the
        # ``int(para_str)`` conversion executed without raising.
        root = lxml_html.fromstring(HTML_TEMPLATE)
        xliff = _xliff(_unit("1", "table_0_r0_c0_para0", "IP67", "甲"))
        assert backfill_table_cells(root, {"1": "甲"}, xliff) == 1
        assert _cell_texts(str(lxml_html.tostring(root, encoding="unicode")))[0] == "甲"


class TestBackfillTableCellsParagraphUnits:
    """OPP#80 Wave 1: paragraph-scoped writes (CONTRACT.md §1.1)."""

    @staticmethod
    def _root(cell_html: str):
        return lxml_html.fromstring(f"<table><tr>{cell_html}</tr></table>")

    @staticmethod
    def _paragraphs(root) -> list[str]:
        return ["".join(p.itertext()) for p in root.xpath("//td//p")]

    def test_para_unit_writes_only_target_block(self):
        root = self._root("<td><p>P0</p><p>P1</p></td>")
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0_para0", "P0", "译-P0"),
                    _unit("2", "table_0_r0_c0_para1", "P1", "译-P1"),
                ]
            )
        )
        assert backfill_table_cells(root, {"1": "译-P0", "2": "译-P1"}, xliff) == 2
        assert self._paragraphs(root) == ["译-P0", "译-P1"]

    def test_para_unit_leaves_sibling_block_untouched(self):
        root = self._root("<td><p>A</p><p>B</p><p>C</p></td>")
        xliff = _xliff(_unit("1", "table_0_r0_c0_para1", "B", "译-B"))
        assert backfill_table_cells(root, {"1": "译-B"}, xliff) == 1
        out = str(lxml_html.tostring(root, encoding="unicode"))
        assert self._paragraphs(root) == ["A", "译-B", "C"]
        assert out.count("<p>") == 3

    def test_para_out_of_range_skips_and_warns(self, caplog):
        root = self._root("<td><p>A</p></td>")
        xliff = _xliff(_unit("1", "table_0_r0_c0_para5", "A", "译-A"))
        with caplog.at_level(logging.WARNING):
            assert backfill_table_cells(root, {"1": "译-A"}, xliff) == 0
        assert self._paragraphs(root) == ["A"]
        assert any("out of range" in r.getMessage() for r in caplog.records)

    def test_para_duplicate_keeps_first_and_warns(self, caplog):
        root = self._root("<td><p>A</p><p>B</p></td>")
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0_para1", "B", "第一"),
                    _unit("2", "table_0_r0_c0_para1", "B", "第二"),
                ]
            )
        )
        with caplog.at_level(logging.WARNING):
            assert backfill_table_cells(root, {"1": "第一", "2": "第二"}, xliff) == 1
        assert self._paragraphs(root) == ["A", "第一"]
        assert any("duplicate" in r.getMessage() for r in caplog.records)

    def test_mixed_bare_and_para_never_falls_back_to_whole_cell(self, caplog):
        root = self._root("<td><p>A</p><p>B</p></td>")
        xliff = _xliff(
            "\n".join(
                [
                    _unit("1", "table_0_r0_c0", "A\nB", "裸译"),
                    _unit("2", "table_0_r0_c0_para0", "A", "译-A"),
                ]
            )
        )
        with caplog.at_level(logging.WARNING):
            assert backfill_table_cells(root, {"1": "裸译", "2": "译-A"}, xliff) == 1
        out = str(lxml_html.tostring(root, encoding="unicode"))
        assert self._paragraphs(root) == ["译-A", "B"]
        assert "裸译" not in out
        assert any("bare unit" in r.getMessage() for r in caplog.records)

    def test_bare_resname_unchanged_regression(self):
        root = self._root("<td><p>A</p><p>B</p></td>")
        xliff = _xliff(_unit("1", "table_0_r0_c0", "A\nB", "裸译"))
        assert backfill_table_cells(root, {"1": "裸译"}, xliff) == 1
        assert self._paragraphs(root) == []
        assert "".join(root.xpath("//td")[0].itertext()) == "裸译"

    def test_inline_only_cell_para0_targets_whole_cell(self):
        root = self._root("<td>a<br/>b</td>")
        xliff = _xliff(_unit("1", "table_0_r0_c0_para0", "ab", "整译"))
        assert backfill_table_cells(root, {"1": "整译"}, xliff) == 1
        assert "".join(root.xpath("//td")[0].itertext()) == "整译"
        assert root.xpath("//td//br") == []
