"""Issue #58: positional table-cell backfill for xliff2pptx.

OPP emits one trans-unit per PPTX table cell with resname
``table_{t}_r{r}_c{c}``, where ``t`` accumulates across slides in presentation
order.  ORF's old text-matching pass keys a source-text dict, so a value
repeated across cells collapses to ONE target and no ordering guarantee exists
for the ``t`` accumulator.  These tests pin the positional write-back, the
``sldIdLst`` ordering, and the fallbacks.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from orf.channels.xliff2pptx import XLIFF2PPTXConverter

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

A = f"{{{A_NS}}}"
P = f"{{{P_NS}}}"
R_ID = f"{{{R_NS}}}id"


# ── XLIFF fixtures (1.2 — the version that carries resname) ────────────────


def _xliff(units: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">\n'
        '  <file original="deck" source-language="en" target-language="zh">\n'
        "    <body>\n" + units + "\n    </body>\n  </file>\n</xliff>\n"
    )


def _unit(uid: str, resname: str, source: str, target: str) -> str:
    return (
        f'      <trans-unit xml:space="preserve" id="{uid}" resname="{resname}">\n'
        f'        <source xml:space="preserve">{source}</source>\n'
        f'        <target xml:space="preserve">{target}</target>\n'
        "      </trans-unit>"
    )


def _plain_unit(uid: str, source: str, target: str) -> str:
    return (
        f'      <trans-unit xml:space="preserve" id="{uid}">\n'
        f'        <source xml:space="preserve">{source}</source>\n'
        f'        <target xml:space="preserve">{target}</target>\n'
        "      </trans-unit>"
    )


# ── PPTX fixtures ─────────────────────────────────────────────────────────

# PPTX is just an OPC ZIP.  These fixtures are hand-built from raw slide XML
# (mirroring tests/test_xliff2pptx_channel.py and
# tests/test_xliff2docx_table_cells.py) so the suite carries no python-pptx
# dependency.  The parts below are the minimum the converter reads: it resolves
# presentation order from p:sldIdLst -> presentation.xml.rels, then walks
# a:tbl/a:tr/a:tc in each slide part.


def _table_cell_xml(value: object) -> str:
    """One a:tc; a list value becomes one paragraph per item."""
    paras = value if isinstance(value, list) else [value]
    body = "".join(
        f"<a:p><a:r><a:t>{para}</a:t></a:r></a:p>" for para in paras
    )
    return f"<a:tc><a:txBody>{body}</a:txBody></a:tc>"


def _table_xml(cells: dict[tuple[int, int], object]) -> str:
    max_row = max(r for r, _c in cells)
    max_col = max(c for _r, c in cells)
    rows = "".join(
        "<a:tr>"
        + "".join(_table_cell_xml(cells.get((r, c), "")) for c in range(max_col + 1))
        + "</a:tr>"
        for r in range(max_row + 1)
    )
    return (
        "<p:graphicFrame><a:graphic><a:graphicData "
        'uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        f"<a:tbl>{rows}</a:tbl>"
        "</a:graphicData></a:graphic></p:graphicFrame>"
    )


def _slide_xml(spec: dict) -> str:
    shapes: list[str] = []
    for text in spec.get("textboxes", []):
        shapes.append(
            "<p:sp><p:txBody><a:p><a:r>"
            f"<a:t>{text}</a:t>"
            "</a:r></a:p></p:txBody></p:sp>"
        )
    for _rows, _cols, cells in spec.get("tables", []):
        shapes.append(_table_xml(cells))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
        "<p:cSld><p:spTree>"
        + "".join(shapes)
        + "</p:spTree></p:cSld></p:sld>"
    )


def _build_deck(path: Path, slides: list[dict]) -> Path:
    """Hand-build a minimal .pptx ZIP.  Each slide spec: {"tables":
    [(rows, cols, {(row, col): text | [paragraph, ...]})], "textboxes": [str]}.

    sldIdLst lists the slides in filename order; ``_reverse_sldid_order`` is the
    helper that breaks that agreement.
    """
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<p:presentation xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
        "<p:sldIdLst>"
        + "".join(
            f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>'
            for i in range(len(slides))
        )
        + "</p:sldIdLst></p:presentation>"
    )
    presentation_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{REL_NS}">'
        + "".join(
            f'<Relationship Id="rId{i + 1}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships/slide" Target="slides/slide{i + 1}.xml"/>'
            for i in range(len(slides))
        )
        + "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="ppt/presentation.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        "</Types>"
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("ppt/presentation.xml", presentation)
        zf.writestr("ppt/_rels/presentation.xml.rels", presentation_rels)
        for i, spec in enumerate(slides):
            zf.writestr(f"ppt/slides/slide{i + 1}.xml", _slide_xml(spec))
    return path


def _minimal_skeleton(path: Path) -> Path:
    """Minimal PPTX ZIP with no ppt/presentation.xml (legacy fixture shape)."""
    slide_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r>"
        "<a:t>Hello World</a:t>"
        "</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    return path


def _reverse_sldid_order(src: Path, dst: Path) -> Path:
    """Swap the first two p:sldId r:id refs so sldIdLst order != filename order."""
    with zipfile.ZipFile(src) as zin:
        data = {name: zin.read(name) for name in zin.namelist()}
    pres = etree.fromstring(data["ppt/presentation.xml"])
    sld_ids = pres.xpath(".//p:sldIdLst/p:sldId", namespaces={"p": P_NS})
    assert len(sld_ids) == 2, "reversal helper expects a 2-slide deck"
    first, second = sld_ids[0].get(R_ID), sld_ids[1].get(R_ID)
    sld_ids[0].set(R_ID, second)
    sld_ids[1].set(R_ID, first)
    data["ppt/presentation.xml"] = etree.tostring(
        pres, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, content in data.items():
            zout.writestr(name, content)
    return dst


# ── read-back helpers ───────────────────────────────────────────────────────


def _read_slide(path: Path, name: str) -> etree._Element:
    with zipfile.ZipFile(path) as zf:
        return etree.fromstring(zf.read(name))


def _cells(root: etree._Element) -> dict[tuple[int, int, int], str]:
    out: dict[tuple[int, int, int], str] = {}
    for t_idx, tbl in enumerate(root.iter(f"{A}tbl")):
        for r_idx, tr in enumerate(tbl.findall(f"{A}tr")):
            for c_idx, tc in enumerate(tr.findall(f"{A}tc")):
                out[(t_idx, r_idx, c_idx)] = "".join(
                    el.text or "" for el in tc.iter(f"{A}t")
                )
    return out


def _run_texts(root: etree._Element) -> list[str]:
    return [
        "".join(el.text or "" for el in run.iter(f"{A}t"))
        for run in root.iter(f"{A}r")
    ]


def _slide_names_in_presentation_order(files: dict[str, bytes]) -> list[str]:
    pres = etree.fromstring(files["ppt/presentation.xml"])
    rels = etree.fromstring(files["ppt/_rels/presentation.xml.rels"])
    rid_to_target = {
        rel.get("Id"): rel.get("Target")
        for rel in rels.findall(f"{{{REL_NS}}}Relationship")
    }
    return [
        "ppt/" + rid_to_target[sld.get(R_ID)]
        for sld in pres.xpath(".//p:sldIdLst/p:sldId", namespaces={"p": P_NS})
    ]


def _reopen_tables(path: Path) -> list[list[list[list[str]]]]:
    """Reopen a .pptx and return, per slide (sldIdLst order), each table grid."""
    with zipfile.ZipFile(path) as zf:
        files = {name: zf.read(name) for name in zf.namelist()}
    slides: list[list[list[list[str]]]] = []
    for name in _slide_names_in_presentation_order(files):
        root = etree.fromstring(files[name])
        slides.append([
            [
                [
                    "".join(el.text or "" for el in tc.iter(f"{A}t"))
                    for tc in tr.findall(f"{A}tc")
                ]
                for tr in tbl.findall(f"{A}tr")
            ]
            for tbl in root.iter(f"{A}tbl")
        ])
    return slides


def _convert(skeleton: Path, tmp_path: Path, xliff_content: str) -> tuple[Path, object]:
    xliff = tmp_path / "translation.xlf"
    xliff.write_text(xliff_content, encoding="utf-8")
    output = tmp_path / "out.pptx"
    result = XLIFF2PPTXConverter().convert(skeleton, xliff, output)
    assert result.success is True, result.errors
    return output, result


# ── tests ───────────────────────────────────────────────────────────────────


class TestParseTableResname:
    def test_table_resname_parsed(self):
        converter = XLIFF2PPTXConverter()
        assert converter._parse_table_resname("table_0_r1_c2") == (0, 1, 2)

    def test_malformed_table_resname_is_none(self):
        converter = XLIFF2PPTXConverter()
        assert converter._parse_table_resname("table_x_r1_c0") is None

    def test_non_table_resname_is_none(self):
        converter = XLIFF2PPTXConverter()
        assert converter._parse_table_resname("para_index_3") is None
        assert converter._parse_table_resname(None) is None


class TestPositionalTableCellBackfill:
    def test_duplicate_cell_text_each_gets_own_target_and_text_pass_skips(
        self, tmp_path: Path
    ):
        # Trap #1: a same-source non-table run must NOT receive a table target.
        skeleton = _build_deck(
            tmp_path / "deck.pptx",
            [{
                "tables": [(2, 2, {(0, 0): "IP67", (1, 0): "IP67",
                                   (0, 1): "Yes", (1, 1): "No"})],
                "textboxes": ["IP67"],
            }],
        )
        xliff = _xliff("\n".join([
            _unit("1", "table_0_r0_c0", "IP67", "ZH-PRIMARY-IP67"),
            _unit("2", "table_0_r1_c0", "IP67", "ZH-SECONDARY-IP67"),
        ]))
        output, _ = _convert(skeleton, tmp_path, xliff)

        grids = _reopen_tables(output)
        assert grids[0][0][0][0] == "ZH-PRIMARY-IP67"
        assert grids[0][0][1][0] == "ZH-SECONDARY-IP67"

        root = _read_slide(output, "ppt/slides/slide1.xml")
        assert _run_texts(root).count("IP67") == 1, (
            "the un-unit'ed text box sharing the table source must be untouched"
        )

    def test_two_slides_tables_map_to_right_slides(self, tmp_path: Path):
        skeleton = _build_deck(
            tmp_path / "deck.pptx",
            [
                {"tables": [(1, 1, {(0, 0): "A1"})]},
                {"tables": [(1, 1, {(0, 0): "B1"})]},
            ],
        )
        xliff = _xliff("\n".join([
            _unit("1", "table_0_r0_c0", "A1", "T-A"),
            _unit("2", "table_1_r0_c0", "B1", "T-B"),
        ]))
        output, _ = _convert(skeleton, tmp_path, xliff)

        assert _cells(_read_slide(output, "ppt/slides/slide1.xml"))[(0, 0, 0)] == "T-A"
        assert _cells(_read_slide(output, "ppt/slides/slide2.xml"))[(0, 0, 0)] == "T-B"

    def test_sldid_order_wins_over_filename_order(self, tmp_path: Path):
        deck = _build_deck(
            tmp_path / "deck.pptx",
            [
                {"tables": [(1, 1, {(0, 0): "AAA"})]},
                {"tables": [(1, 1, {(0, 0): "BBB"})]},
            ],
        )
        reversed_deck = _reverse_sldid_order(deck, tmp_path / "reversed.pptx")
        xliff = _xliff("\n".join([
            _unit("1", "table_0_r0_c0", "AAA", "FIRST"),
            _unit("2", "table_1_r0_c0", "BBB", "SECOND"),
        ]))
        output, _ = _convert(reversed_deck, tmp_path, xliff)

        # sldIdLst now lists slide2 first, so table_0 belongs to slide2.
        assert _cells(_read_slide(output, "ppt/slides/slide2.xml"))[(0, 0, 0)] == "FIRST"
        assert _cells(_read_slide(output, "ppt/slides/slide1.xml"))[(0, 0, 0)] == "SECOND"

    def test_out_of_range_table_warns_without_raising(self, tmp_path: Path):
        skeleton = _build_deck(
            tmp_path / "deck.pptx",
            [{"tables": [(1, 1, {(0, 0): "X"})]}],
        )
        xliff = _xliff(_unit("1", "table_9_r9_c9", "X", "T"))
        with patch("orf.channels.xliff2pptx.logger") as mock_logger:
            output, _ = _convert(skeleton, tmp_path, xliff)

        warned = " ".join(
            str(arg)
            for call in mock_logger.warning.call_args_list
            for arg in call.args
        )
        assert "out of range" in warned
        # Output still opens and the source is untouched (table units never
        # fall back to the text pass).
        assert _reopen_tables(output)[0][0][0][0] == "X"

    def test_multi_paragraph_cell_replaces_all_source(self, tmp_path: Path):
        skeleton = _build_deck(
            tmp_path / "deck.pptx",
            [{"tables": [(1, 1, {(0, 0): ["IP", "67"]})]}],
        )

        xliff = _xliff(_unit("1", "table_0_r0_c0", "IP\n67", "翻译"))
        output, _ = _convert(skeleton, tmp_path, xliff)

        cell_t = [
            el.text or ""
            for tc in _read_slide(output, "ppt/slides/slide1.xml").iter(f"{A}tc")
            for el in tc.iter(f"{A}t")
        ]
        assert "".join(cell_t) == "翻译"
        assert cell_t[1:] == [""] * (len(cell_t) - 1), "tail runs must be cleared"


class TestTextPathFallback:
    def test_paragraph_only_xliff_still_backfills(self, tmp_path: Path):
        skeleton = _build_deck(
            tmp_path / "deck.pptx", [{"textboxes": ["Hello World"]}]
        )
        xliff = _xliff(_plain_unit("1", "Hello World", "你好 世界"))
        output, _ = _convert(skeleton, tmp_path, xliff)

        root = _read_slide(output, "ppt/slides/slide1.xml")
        assert "你好 世界" in _run_texts(root)

    def test_minimal_fixture_without_presentation_falls_back(self, tmp_path: Path):
        skeleton = _minimal_skeleton(tmp_path / "skeleton.pptx")
        xliff = _xliff(_plain_unit("1", "Hello World", "你好 世界"))
        with patch("orf.channels.xliff2pptx.logger") as mock_logger:
            output, _ = _convert(skeleton, tmp_path, xliff)

        warned = " ".join(
            str(arg)
            for call in mock_logger.warning.call_args_list
            for arg in call.args
        )
        assert "falling back" in warned
        root = _read_slide(output, "ppt/slides/slide1.xml")
        assert "你好 世界" in _run_texts(root)
