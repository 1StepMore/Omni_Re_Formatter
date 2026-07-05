"""Backfill-matching functions for the XLIFF2DOCX converter.

Contains all paragraph-matching and text-backfill logic, including the
E2E-07 fuzzy matching algorithm.  Every function here is standalone
(takes all dependencies explicitly) so they can be called from the
XLIFF2DOCXConverter delegate methods.
"""

from __future__ import annotations

import re
from typing import Any

from lxml import etree

from orf.logging import get_logger
from orf.skeleton.inline_formatting import InlineElement

from ._ns import WORD_NS_MAP, W_NS, FUZZY_MATCH_THRESHOLD

logger = get_logger("channel.xliff2docx.matcher")


def _distribute_text_across_runs(runs: list, target_text: str) -> None:
    """Distribute target_text across runs, preserving newlines.

    Instead of flattening all text into runs[0] and clearing the rest
    (which loses ``\\n``), this helper splits on newlines and distributes
    one line per run.  If there are more lines than runs, extra lines are
    appended (with ``\\n``) to the last run.  If there are more runs than
    lines, the tail runs are cleared.

    ORF#12: fixes newline flattening in xliff2docx backfill.
    """
    lines = target_text.split('\n')
    for i, line in enumerate(lines):
        if i < len(runs):
            runs[i].text = line
        else:
            runs[-1].text += '\n' + line
    for r in runs[len(lines):]:
        r.text = ""


def collect_all_paragraphs(root: etree._Element) -> list[etree._Element]:
    """Collect ALL ``w:p`` elements mirroring OPP's ``result.paragraphs`` order.

    OPP's ``extract_paragraphs`` builds ``result.paragraphs`` in this exact order:
      1. Body-level ``<w:p>`` (via ``doc.paragraphs``, non-empty only)
      2. Table cell ``<w:p>`` (via ``body//w:tc//w:p``)
      3. Textbox ``<w:p>`` (via ``body//w:txbxContent//w:p``, deduplicated by text)

    ``non_body_N`` in the XLIFF resname is the GLOBAL index into this flat
    list, NOT an index into non-body-only paragraphs.

    Returns:
        List of ``w:p`` elements in OPP extraction order.
    """
    paragraphs: list[etree._Element] = []
    w_tag = f"{{{W_NS}}}"
    seen_texts: set[str] = set()
    body = root.find("w:body", WORD_NS_MAP)

    # 1. Body-level <w:p> — non-empty only
    for p in body.xpath("./w:p", namespaces=WORD_NS_MAP):
        text = "".join(
            t.text or ""
            for t in p.iter(f"{w_tag}t")
            if not any(
                anc.tag == f"{w_tag}txbxContent"
                for anc in t.iterancestors()
            )
        ).strip()
        if text:
            paragraphs.append(p)

    # 2. Table cell paragraphs
    for tc in root.iter(f"{w_tag}tc"):
        for p in tc.iter(f"{w_tag}p"):
            paragraphs.append(p)

    # 3. Textbox paragraphs — preserve ALL entries for correct non_body_N
    # index matching.  OPP extracts every textbox paragraph at a distinct
    # non_body_N position; dedup by text (previous behaviour) broke the
    # positional contract, causing 9 units to fall past the end of the
    # paragraph list.  ORF#37.
    for txbx in root.iter(f"{w_tag}txbxContent"):
        for p in txbx.iter(f"{w_tag}p"):
            text = "".join(
                t.text or "" for t in p.iter(f"{w_tag}t")
            ).strip()
            if text:
                paragraphs.append(p)

    return paragraphs


def backfill_by_position(
    body_paragraphs: list[etree._Element],
    para_index: int,
    target_text: str,
    build_formatted_runs_fn: Any,
    strip_inline_tags_fn: Any,
) -> bool:
    """Phase B.2: apply target_text to the ``w:p`` at the given index.

    Uses position-based lookup via ``resname="para_index_N"``.
    Bypasses all text-matching heuristics.

    BX/EX safety: if target_text contains ``<bx .../>`` / ``<ex .../>``
    tags, they are parsed into proper DOCX ``<w:rPr>`` formatted runs.
    """
    if not (0 <= para_index < len(body_paragraphs)):
        logger.warning(
            "resname para_index=%d out of range (have %d body-level paragraphs)",
            para_index, len(body_paragraphs),
        )
        return False
    para = body_paragraphs[para_index]
    runs = para.xpath(".//w:t", namespaces=WORD_NS_MAP)
    if not runs:
        return False

    # BX/EX leak fix: parse inline tags into formatted DOCX runs
    formatted_runs = build_formatted_runs_fn(target_text)
    has_real_inline_tags = bool(
        re.search(
            r"<\s*/?\s*bx\b|<\s*/?\s*ex\b",
            target_text,
        )
    )
    if formatted_runs and has_real_inline_tags:
        target_run = runs[0]
        parent = target_run.getparent()
        if parent is not None:
            insert_pos = list(parent.getparent()).index(parent) if parent.getparent() is not None else -1
            if insert_pos >= 0:
                parent_para = parent.getparent()
                for i, fr in enumerate(formatted_runs):
                    parent_para.insert(
                        list(parent_para).index(parent) + i,
                        fr,
                    )
                target_run.text = ""
                for r in runs[1:]:
                    r.text = ""
                return True
            # Fallback: strip tags if parent manipulation fails
            runs[0].text = strip_inline_tags_fn(target_text)
            for r in runs[1:]:
                r.text = ""
            return True

    # No bx/ex tags — plain text path
    _distribute_text_across_runs(runs, target_text)
    return True


def backfill_by_non_body_position(
    paragraphs: list[etree._Element],
    para_idx: int,
    target_text: str,
    build_formatted_runs_fn: Any,
    strip_inline_tags_fn: Any,
) -> bool:
    """Phase B.3: apply target_text to the ``w:p`` at the given flat-list index.

    Used when the OPP source has ``resname="non_body_N"``.
    The index is the GLOBAL position in OPP's flat ``result.paragraphs`` list.
    """
    if not (0 <= para_idx < len(paragraphs)):
        logger.warning(
            "non_body paragraph index %d out of range (have %d total paragraphs)",
            para_idx, len(paragraphs),
        )
        return False
    para = paragraphs[para_idx]
    runs = para.xpath("./w:r/w:t", namespaces=WORD_NS_MAP)
    if not runs:
        return False

    # BX/EX leak fix: parse inline tags into formatted DOCX runs
    formatted_runs = build_formatted_runs_fn(target_text)
    has_real_inline_tags = bool(
        re.search(
            r"<\s*/?\s*bx\b|<\s*/?\s*ex\b",
            target_text,
        )
    )
    if formatted_runs and has_real_inline_tags:
        target_run = runs[0]
        parent = target_run.getparent()
        if parent is not None:
            parent_para = parent.getparent()
            if parent_para is not None:
                for i, fr in enumerate(formatted_runs):
                    parent_para.insert(
                        list(parent_para).index(parent) + i,
                        fr,
                    )
                target_run.text = ""
                for r in runs[1:]:
                    r.text = ""
                return True
            runs[0].text = strip_inline_tags_fn(target_text)
            for r in runs[1:]:
                r.text = ""
            return True

    _distribute_text_across_runs(runs, target_text)
    return True


def backfill_fallback_textboxes(
    root: etree._Element,
    chinese_to_target: dict[str, str],
) -> None:
    """Apply translations to ``<mc:Fallback>`` textbox paragraphs.

    Choice-branch textbox paragraphs are collected and translated by
    ``backfill_by_non_body_position``, but their Fallback-branch counterparts
    (with identical original text) are skipped by the text-based dedup.
    This function finds Fallback textbox paragraphs and applies the same
    translation from the pre-built mapping.
    """
    w_tag = f"{{{W_NS}}}"
    WORD_NS_MAP_LOCAL = {"w": W_NS}

    for txbx in root.iter(f"{w_tag}txbxContent"):
        parent = txbx.getparent()
        in_fallback = False
        while parent is not None:
            if parent.tag.endswith("Fallback"):
                in_fallback = True
                break
            parent = parent.getparent()
        if not in_fallback:
            continue

        for p in txbx.iter(f"{w_tag}p"):
            text = "".join(t.text or "" for t in p.iter(f"{w_tag}t")).strip()
            if not text:
                continue
            target_text = chinese_to_target.get(text)
            if target_text is None:
                continue
            runs = p.xpath("./w:r/w:t", namespaces=WORD_NS_MAP_LOCAL)
            if runs:
                _distribute_text_across_runs(runs, target_text)


def fallback_backfill(target_text: str) -> bool:
    """Last-resort fallback: log a warning and skip.

    The previous implementation wrote the LLM target to the FIRST
    non-empty paragraph in the document, clobbering unrelated content.
    The new behavior leaves the OPP source paragraph untouched.
    """
    logger.warning(
        "No matching paragraph for LLM target; skipping. "
        "Original OPP source text is preserved. Target: %r",
        target_text[:80],
    )
    return False


def backfill_translation(
    root: etree._Element,
    body_paragraphs: list[etree._Element],
    source_text: str,
    target_text: str,
    inline_elements: list[InlineElement],
    wt_text_map: dict[str, etree._Element],
    body_paragraph_text_map: dict[str, etree._Element],
    all_paragraph_text_map: dict[str, etree._Element],
    build_formatted_runs_fn: Any,
    strip_inline_tags_fn: Any,
) -> bool:
    r"""Backfill a single translation into the parsed document root.

    POST_MORTEM ORF-2: matching is now layered:
      1. Try exact match via pre-built O(1) lookup indexes.
      2. Try SequenceMatcher ratio >= FUZZY_MATCH_THRESHOLD for cases
         where the LLM rephrased (punctuation, missing/extra words).
      3. Last resort: skip so we never clobber unrelated content.
    """
    if not source_text and not target_text:
        return False

    found = False
    source_stripped = re.sub(r'<[^>]+>', '', source_text) if source_text else ""
    source_normalized = re.sub(r"\s+", " ", source_stripped).strip()

    if inline_elements:
        found = backfill_with_inline_elements(
            root, source_normalized, target_text, inline_elements,
            all_paragraph_text_map,
            build_formatted_runs_fn,
        )
    else:
        # Pattern C: O(1) exact match on individual <w:t> elements
        t_elem = wt_text_map.get(source_normalized)
        if t_elem is None:
            t_elem = wt_text_map.get(source_stripped)
        if t_elem is not None:
            found = True
            t_elem.text = target_text

    if not found:
        # Pattern A: O(1) exact match on body paragraphs
        p = body_paragraph_text_map.get(source_normalized)
        if p is None:
            p = body_paragraph_text_map.get(source_stripped)
        if p is not None:
            found = backfill_split_runs(
                p, source_normalized, target_text,
                build_formatted_runs_fn,
            )

    if not found:
        found = fuzzy_backfill(
            root, source_normalized, target_text,
            FUZZY_MATCH_THRESHOLD,
        )

    if not found:
        found = fallback_backfill(target_text)

    return found


def fuzzy_backfill(
    root: etree._Element,
    source_normalized: str,
    target_text: str,
    threshold: float = FUZZY_MATCH_THRESHOLD,
) -> bool:
    """Apply target_text to the paragraph whose text is most similar to
    source_normalized.  Returns True if applied.

    Uses three layered signals, in order:
      1. SequenceMatcher ratio on whitespace-normalized text.
      2. CJK character-set Jaccard similarity.
      3. Plain SequenceMatcher ratio without normalization.
    """
    import difflib
    paragraphs = root.xpath("//w:p", namespaces=WORD_NS_MAP)
    cjk_re = __import__("re").compile(r"[\u4e00-\u9fff]")
    source_chars = set(cjk_re.findall(source_normalized))

    best_para = None
    best_score = 0.0
    for p in paragraphs:
        runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
        para_text = "".join(r.text or "" for r in runs)
        para_norm = re.sub(r"\s+", " ", para_text).strip()
        if not para_norm or not source_normalized:
            continue
        sm_ratio = difflib.SequenceMatcher(None, source_normalized, para_norm).ratio()
        para_chars = set(cjk_re.findall(para_norm))
        jaccard = (
            len(source_chars & para_chars) / len(source_chars | para_chars)
            if (source_chars | para_chars) else 0.0
        )
        raw_ratio = difflib.SequenceMatcher(None, source_normalized, para_text).ratio()
        score = max(sm_ratio, jaccard, raw_ratio)
        if score > best_score:
            best_score = score
            best_para = p

    if best_para is not None and best_score >= threshold:
        runs = best_para.xpath(".//w:t", namespaces=WORD_NS_MAP)
        if not runs:
            return False
        para_text = "".join(r.text or "" for r in runs)
        if len(para_text.strip()) < 4:
            return False
        _distribute_text_across_runs(runs, target_text)
        return True
    return False


def backfill_with_inline_elements(
    root: etree._Element,
    source_normalized: str,
    target_text: str,
    inline_elements: list[InlineElement],
    all_paragraph_text_map: dict[str, etree._Element],
    build_formatted_runs_fn: Any,
) -> bool:
    """Backfill when source has inline formatting tags.

    Finds paragraphs where the source text (with inline tags stripped) matches,
    then applies translations preserving inline structure.
    """
    found = False
    # Pattern B: O(1) exact match on all paragraphs
    p = all_paragraph_text_map.get(source_normalized)
    if p is not None:
        found = backfill_split_runs(
            p, source_normalized, target_text,
            build_formatted_runs_fn,
        )
        if found:
            return found

    # Fallback: sequential scan
    for p in root.xpath("//w:p", namespaces=WORD_NS_MAP):
        text_runs = p.xpath(".//w:t", namespaces=WORD_NS_MAP)
        text_content = "".join(t.text or "" for t in text_runs)

        if source_normalized in text_content:
            found = backfill_split_runs(
                p, source_normalized, target_text,
                build_formatted_runs_fn,
            )
            if found:
                break

    return found


def backfill_split_runs(
    paragraph: etree._Element,
    source_normalized: str,
    target_text: str,
    build_formatted_runs_fn: Any,
) -> bool:
    """Backfill text that may be split across multiple ``<w:t>`` runs.

    Finds the first run containing source_normalized, replaces it with
    target_text, and clears subsequent runs.  Strips XLIFF bx/ex tags and
    applies proper DOCX run formatting.

    E2E-07 fix: when exact match fails, try fuzzy matching if the lengths
    are close (within 5 chars).  This handles cases where the XLIFF source
    contains content that doesn't exactly match the DOCX paragraph.
    """
    runs = paragraph.xpath(".//w:t", namespaces=WORD_NS_MAP)
    concat = "".join(r.text or "" for r in runs)

    match_pos = concat.find(source_normalized)
    if match_pos >= 0:
        # Exact match — use it
        pass
    elif len(source_normalized) > 3 and abs(len(source_normalized) - len(concat)) <= 5:
        # E2E-07 fix: fuzzy match when lengths are close
        import difflib
        best_ratio = 0.0
        best_pos = -1
        # FIXED: step=1 for sub-character-level accuracy
        for start in range(len(concat) - len(source_normalized) + 1):
            window = concat[start:start + len(source_normalized) + 5]
            ratio = difflib.SequenceMatcher(None, source_normalized, window[:len(source_normalized)]).ratio()
            if ratio > best_ratio and ratio >= 0.85:
                best_ratio = ratio
                best_pos = start
        if best_pos >= 0:
            match_pos = best_pos
            logger.debug("E2E-07 fuzzy match: ratio=%.2f pos=%d", best_ratio, best_pos)
    else:
        return False

    if match_pos < 0:
        return False

    target_run_idx = None
    for i, r in enumerate(runs):
        run_text = r.text or ""
        run_start = concat.find(run_text, match_pos) if run_text else -1
        if run_start <= match_pos < run_start + len(run_text) or run_start < 0:
            target_run_idx = i
            break

    if target_run_idx is None:
        return False

    formatted_runs = build_formatted_runs_fn(target_text)
    if formatted_runs:
        target_run = runs[target_run_idx]
        parent = target_run.getparent()
        if parent is not None:
            insert_pos = list(parent).index(target_run)
            for fr in formatted_runs:
                parent.insert(insert_pos, fr)
                insert_pos += 1
            target_run.text = ""
            for j in range(target_run_idx + 1, len(runs)):
                runs[j].text = ""
        return True

    runs[target_run_idx].text = target_text
    for j in range(target_run_idx + 1, len(runs)):
        runs[j].text = ""
    return True
