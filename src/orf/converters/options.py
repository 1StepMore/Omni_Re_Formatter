"""Typed options dataclass for format conversion.

Consolidates all optional parameters that were previously passed
as ``**options: Any`` through the converter hierarchy into a single
typed dataclass. Each field defaults to ``None`` (or a sensible
default) so converters only need to check fields they care about.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ConverterOptions:
    """Typed options for format conversion.

    All fields are optional — converters that don't use a particular
    option simply ignore it.  The dataclass replaces the previous
    ``**options: Any`` pattern so type checkers can verify option usage.

    ---- Pandoc / MD→format common options ----
    template:       Reference document template (docx/odt).
    css:            Stylesheet for HTML output.
    engine:         PDF engine (``"pandoc"`` or ``"weasyprint"``).
    chinese_font:   Chinese font to embed in PDF (when engine is weasyprint).

    ---- EPUB metadata ----
    title:          EPUB document title.
    author:         EPUB document author.
    lang:           EPUB language code.
    toc:            Generate EPUB table of contents.
    embed_images:   Embed images in the EPUB (vs. linking).

    ---- DOCX layout ----
    separate_images:  Extract images to ``images_dir`` instead of embedding.
    images_dir:       Target directory for extracted images.

    ---- Tabular data ----
    delimiter:      CSV delimiter character (default: ``","``).
    sheet_name:     XLSX sheet name (default: ``"Sheet1"``).

    ---- Jupyter ----
    kernel:         IPYNB kernel name (default: ``"python3"``).

    ---- XLIFF→format common options ----
    xliff_path:     Path to the translated XLIFF file.
    encoding:       XLIFF file encoding (default: ``"utf-8"``).

    ---- DOCX (XLIFF) ----
    segment_mapping:  Pre-computed segment→paragraph mapping.

    ---- EPUB (XLIFF) ----
    preserve_styles:  Preserve inline styles during XLIFF→EPUB merge.

    ---- HTML (XLIFF) ----
    preserve_inline:  Preserve inline elements (default: ``True``).

    ---- PPTX (XLIFF) ----
    slide_mapping:    Pre-computed unit→slide mapping.

    ---- ODF (XLIFF) ----
    source_lang:  Source language code.
    target_lang:  Target language code.
    exclude:      Patterns / elements to exclude from conversion.
    timestamp:    Include a conversion timestamp in the output.
    """

    # common / pandoc
    template: Path | None = None
    css: str | None = None
    engine: str = "weasyprint"
    chinese_font: str | None = None

    # epub metadata
    title: str | None = None
    author: str | None = None
    lang: str | None = None
    toc: bool = False
    embed_images: bool = False

    # docx layout
    separate_images: bool = True
    images_dir: str | None = None
    text_only: bool = False
    images_data: list[dict] | None = None  # OPP images.json payload for separation

    # tabular
    delimiter: str = ","
    sheet_name: str = "Sheet1"

    # jupyter
    kernel: str = "python3"

    # xliff common
    xliff_path: str | None = None
    encoding: str = "utf-8"

    # xliff→docx
    segment_mapping: dict | None = None

    # xliff→epub
    preserve_styles: bool = False

    # xliff→html
    preserve_inline: bool = True

    # xliff→pptx
    slide_mapping: dict | None = None

    # xliff→odf
    source_lang: str | None = None
    target_lang: str | None = None
    exclude: list | None = None
    timestamp: bool = False

    # xliff→pdf (skeleton HTML from OPP extraction)
    skeleton_html: str | None = None
