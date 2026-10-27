"""Format Detection Engine for ORF Phase 2.

Detects document format using a priority chain:
1. manifest.json (from OPP) - explicit format field
2. Magic bytes - binary signatures in file header
3. Extension fallback - file extension matching
4. ZIP structure (skeleton) - internal file paths

Raises FormatDetectionError when detection fails at all stages.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from orf.error_handlers.conversion_error import FormatDetectionError
from orf.logging import get_logger
from orf.parsers.manifest import find_manifest, parse_manifest

from orf.detection.magic_bytes import MAGIC_SIGNATURES, _ZIP_MAGIC, _disambiguate_zip_format

logger = get_logger("detection.format_detector")

# File extension to format mapping for fallback
EXTENSION_MAP: dict[str, str] = {
    ".pdf": "PDF",
    ".docx": "DOCX",
    ".pptx": "PPTX",
    ".odt": "ODT",
    ".epub": "EPUB",
    ".rtf": "RTF",
    ".html": "HTML",
    ".htm": "HTML",
    ".md": "MD",
    ".markdown": "MD",
}


class FormatDetector:
    """Detects document format via manifest, magic bytes, or extension.

    Usage:
        detector = FormatDetector()
        fmt = detector.detect("document.md")
        fmt = detector.detect_from_file("document.docx")
        fmt = detector.detect_from_manifest("document_manifest.json")
    """

    def detect_from_manifest(self, manifest_path: Path | str) -> str:
        """Detect format from manifest.json source information.

        Args:
            manifest_path: Path to manifest.json

        Returns:
            Format name string (e.g., "DOCX", "PDF")

        Raises:
            FormatDetectionError: If manifest cannot be parsed or lacks format
        """
        manifest_path = Path(manifest_path)

        try:
            manifest = parse_manifest(manifest_path)
        except Exception as e:
            logger.debug("Failed to parse manifest %s: %s", manifest_path, e, exc_info=True)
            raise FormatDetectionError(
                str(manifest_path), f"Failed to parse manifest: {e}"
            )

        detected_format = manifest.source.format.upper()

        if not detected_format:
            raise FormatDetectionError(
                str(manifest_path), "No format field in manifest source"
            )

        logger.debug("Detected format '%s' from manifest '%s'", detected_format, manifest_path)
        return detected_format

    def detect_from_file(self, file_path: Path | str) -> str:
        """Detect format by reading magic bytes from file header.

        Args:
            file_path: Path to file to inspect

        Returns:
            Format name string (e.g., "DOCX", "PDF")

        Raises:
            FormatDetectionError: If file cannot be read or signature unknown
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise FormatDetectionError(str(file_path), "File does not exist")

        try:
            with open(file_path, "rb") as f:
                header = f.read(16)
        except OSError as e:
            raise FormatDetectionError(str(file_path), f"Cannot read file: {e}")

        if header.startswith(_ZIP_MAGIC):
            fmt = _disambiguate_zip_format(file_path)
            if fmt != "zip/unknown":
                logger.debug(
                    "Detected format '%s' via ZIP disambiguation in '%s'",
                    fmt, file_path,
                )
                return fmt.upper()
            raise FormatDetectionError(
                str(file_path), "Unknown ZIP-based format (no matching internal structure)"
            )

        for fmt_name, magic in MAGIC_SIGNATURES.items():
            if header.startswith(magic):
                logger.debug("Detected format '%s' via magic bytes in '%s'", fmt_name, file_path)
                return fmt_name

        raise FormatDetectionError(
            str(file_path), "Unknown file format (no magic bytes match)"
        )

    def detect(self, file_path: Path | str) -> str:
        """Detect format using priority chain: manifest -> magic bytes -> extension.

        Args:
            file_path: Path to file to detect

        Returns:
            Format name string (e.g., "DOCX", "PDF")

        Raises:
            FormatDetectionError: If all detection methods fail
        """
        file_path = Path(file_path)

        # 1. Try manifest.json first
        manifest_path = find_manifest(file_path)
        if manifest_path is not None:
            try:
                return self.detect_from_manifest(manifest_path)
            except FormatDetectionError:
                logger.debug("Manifest detection failed for '%s', trying next method", file_path)

        # 2. Try magic bytes
        if file_path.exists() and not file_path.is_dir():
            try:
                return self.detect_from_file(file_path)
            except FormatDetectionError:
                logger.debug("Magic bytes detection failed for '%s', trying extension fallback", file_path)

        # 3. Extension fallback
        ext = file_path.suffix.lower()
        if ext in EXTENSION_MAP:
            fmt = EXTENSION_MAP[ext]
            logger.debug("Detected format '%s' via extension fallback for '%s'", fmt, file_path)
            return fmt

        raise FormatDetectionError(
            str(file_path),
            f"Cannot detect format: no manifest, magic bytes unknown, unknown extension '{ext}'"
        )

    def detect_from_skeleton(self, skeleton_path: Path | str) -> str:
        """Detect source format from skeleton.zip internal ZIP structure.

        Analyzes the internal file paths within a skeleton ZIP to determine
        the original document format. This is used by XLIFF backfill channels
        to determine the appropriate image injection strategy.

        Args:
            skeleton_path: Path to the skeleton ZIP file

        Returns:
            Format name string (e.g., "DOCX", "PPTX", "EPUB", "HTML")

        Raises:
            FormatDetectionError: If skeleton cannot be read or format unknown
        """
        skeleton_path = Path(skeleton_path)

        if not skeleton_path.exists():
            raise FormatDetectionError(str(skeleton_path), "Skeleton file does not exist")

        try:
            with zipfile.ZipFile(skeleton_path, "r") as zf:
                namelist = zf.namelist()

            # DOCX: word/document.xml
            if "word/document.xml" in namelist:
                logger.debug("Detected DOCX format from skeleton structure")
                return "DOCX"

            # PPTX: ppt/presentation.xml
            if "ppt/presentation.xml" in namelist:
                logger.debug("Detected PPTX format from skeleton structure")
                return "PPTX"

            # XLSX: xl/workbook.xml
            if "xl/workbook.xml" in namelist:
                logger.debug("Detected XLSX format from skeleton structure")
                return "XLSX"

            # EPUB: mimetype file + OEBPS/ or EPUB Content/
            if "mimetype" in namelist and any(
                f.startswith("OEBPS/") or f.startswith("Content/")
                for f in namelist
            ):
                logger.debug("Detected EPUB format from skeleton structure")
                return "EPUB"

            # ODF (ODT): mimetype + content.xml (LibreOffice format)
            if "mimetype" in namelist and "content.xml" in namelist:
                logger.debug("Detected ODT format from skeleton structure")
                return "ODT"

            # EML: OPP packages the raw message plus an `eml_map.json` sidecar
            # at the archive root (unlike DOCX/PPTX, the message is not a
            # format-native zip tree, so the sidecar is the signature). Keep
            # this after the zip-native formats above so a renamed skeleton
            # cannot be mistaken for EML.
            if "message.eml" in namelist and "eml_map.json" in namelist:
                logger.debug("Detected EML format from skeleton structure")
                return "EML"

            # HTML: index.html or other top-level HTML files
            if any(
                f.lower().endswith((".html", ".htm")) and "/" not in f
                for f in namelist
            ):
                logger.debug("Detected HTML format from skeleton structure")
                return "HTML"

            # Try second-level HTML files
            if any(
                f.lower().endswith((".html", ".htm")) and f.count("/") == 1
                for f in namelist
            ):
                logger.debug("Detected HTML format from skeleton structure (subdirectory)")
                return "HTML"

            raise FormatDetectionError(
                str(skeleton_path),
                "Cannot detect format: no known internal structure"
            )

        except zipfile.BadZipFile as e:
            raise FormatDetectionError(
                str(skeleton_path), f"Invalid ZIP file: {e}"
            )
        except OSError as e:
            raise FormatDetectionError(
                str(skeleton_path), f"Cannot read skeleton file: {e}"
            )