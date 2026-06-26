"""ORF#12: Tests for _distribute_text_across_runs helper.

Verifies that newline-containing target text is properly distributed
across DOCX <w:t> runs instead of being flattened into a single run.
"""

from __future__ import annotations

import pytest
from lxml import etree

# Import the helper we're about to create (RED phase — will fail until GREEN)
from orf.channels.xliff2docx import _distribute_text_across_runs


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_t(text: str) -> etree._Element:
    """Create a <w:t> element with the given text."""
    t = etree.SubElement(etree.Element(f"{{{W_NS}}}r"), f"{{{W_NS}}}t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return t


class TestDistributeTextAcrossRuns:
    """Unit tests for _distribute_text_across_runs."""

    def test_single_line_single_run(self):
        """Single line text with one run — sets run text directly."""
        runs = [_make_t("old")]
        _distribute_text_across_runs(runs, "new text")
        assert runs[0].text == "new text"

    def test_multiline_distributes_across_runs(self):
        """Two lines with two runs — each run gets one line."""
        runs = [_make_t("line1_old"), _make_t("line2_old")]
        _distribute_text_across_runs(runs, "first\nsecond")
        assert runs[0].text == "first"
        assert runs[1].text == "second"

    def test_multiline_fewer_runs_than_lines(self):
        """Three lines but only two runs — extra lines append to last run."""
        runs = [_make_t("a"), _make_t("b")]
        _distribute_text_across_runs(runs, "one\ntwo\nthree")
        assert runs[0].text == "one"
        assert runs[1].text == "two\nthree"

    def test_single_line_multiple_runs_clears_extras(self):
        """Single line with three runs — remaining runs get empty string."""
        runs = [_make_t("r1"), _make_t("r2"), _make_t("r3")]
        _distribute_text_across_runs(runs, "only one line")
        assert runs[0].text == "only one line"
        assert runs[1].text == ""
        assert runs[2].text == ""

    def test_empty_text(self):
        """Empty target text sets first run to empty, clears rest."""
        runs = [_make_t("old1"), _make_t("old2")]
        _distribute_text_across_runs(runs, "")
        assert runs[0].text == ""
        assert runs[1].text == ""

    def test_preserves_newlines_in_distribution(self):
        """Newlines are preserved in the distributed text."""
        runs = [_make_t("a"), _make_t("b")]
        _distribute_text_across_runs(runs, "line1\nline2\nline3")
        assert runs[0].text == "line1"
        # Extra lines appended with \n to last run
        assert runs[1].text == "line2\nline3"

    def test_exact_match_lines_to_runs(self):
        """When lines == runs count, each run gets exactly one line."""
        runs = [_make_t("x"), _make_t("y"), _make_t("z")]
        _distribute_text_across_runs(runs, "A\nB\nC")
        assert runs[0].text == "A"
        assert runs[1].text == "B"
        assert runs[2].text == "C"

    def test_more_runs_than_lines_clears_tail(self):
        """Four runs but two lines — last two runs get empty string."""
        runs = [_make_t("a"), _make_t("b"), _make_t("c"), _make_t("d")]
        _distribute_text_across_runs(runs, "first\nsecond")
        assert runs[0].text == "first"
        assert runs[1].text == "second"
        assert runs[2].text == ""
        assert runs[3].text == ""
