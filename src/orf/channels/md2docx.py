"""Markdown to DOCX conversion channel using Pandoc."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from orf.converters.base import BaseConverter, ConversionResult
from orf.converters.chunked_md_converter import ChunkedMDConverter
from orf.converters.atomic import atomic_write_bytes, atomic_write_text
from orf.parsers.manifest import Manifest
from orf.parsers.frontmatter import FrontmatterMetadata
from orf.logging import get_logger
from orf.converters.options import ConverterOptions

logger = get_logger("channel.md2docx")

_CONVERSION_TIMEOUT = int(os.environ.get("ORF_CONVERSION_TIMEOUT", "300"))

# Fixed ZIP member timestamp for reproducible sidecar archives: identical
# bytes must pack to identical archives regardless of on-disk mtimes.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_OLIMG_GLOB = "OLIMG_*"


def _deterministic_pandoc_env(source_path: Path) -> dict[str, str]:
    """Env for pandoc with SOURCE_DATE_EPOCH derived from the input bytes.

    Pandoc stamps ``docProps/core.xml`` (and its archive entries) with the
    wall clock, so two runs of the same conversion produced different DOCX
    bytes. The epoch derives from the INPUT CONTENT (same mechanism as the
    EPUB channel, ``md2epub._deterministic_pandoc_env``): an mtime-based
    pin made the DOCX bytes a function of the source file's mtime, and
    apply-md stages every conversion through a fresh temp sibling whose
    mtime changes per run — so re-building the same content in a suite or
    a different directory yielded different bytes. A content-derived epoch
    is invariant to the filesystem, mtimes, timezone and rebuilds:
    identical content packs to identical DOCX bytes.
    """
    env = dict(os.environ)
    try:
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        env["SOURCE_DATE_EPOCH"] = str(int(digest[:8], 16) % (2**31 - 1))
    except OSError:
        pass
    return env


IMAGE_PATTERN = re.compile(r'!\[([^\]]*)\]\((data:image/([^;]+);base64,([^)]+))\)')
IMAGE_REF_PATTERN = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
OLIMG_PATTERN = re.compile(r'OLIMG\d+')


class MD2DOCXConverter(BaseConverter):
    """Markdown to DOCX converter using Pandoc."""

    def __init__(
        self,
        manifest: Optional[Manifest] = None,
        frontmatter: Optional[FrontmatterMetadata] = None,
        reference_docx: Optional[Path | str] = None,
        chunk_size: int = 65536,
    ) -> None:
        super().__init__(manifest, frontmatter)
        self.reference_docx = Path(reference_docx) if reference_docx else None
        self.chunk_size = chunk_size

    @property
    def supported_format(self) -> str:
        return "DOCX"

    def validate_input(self, input_path: Path | str) -> bool:
        input_path = Path(input_path)
        return input_path.exists() and input_path.suffix.lower() == ".md"

    def convert(
        self,
        input_path: Path | str,
        output_path: Path | str,
        options: ConverterOptions | None = None,
    ) -> ConversionResult:
        input_path = Path(input_path)
        output_path = Path(output_path)
        opts = options or ConverterOptions()

        if not self.validate_input(input_path):
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Invalid input file: {input_path}"],
            )

        temp_dir = None
        md_path = input_path

        if opts.separate_images:
            # Default mode: extract images into organized output alongside the DOCX.
            images_dir = (
                Path(opts.images_dir)
                if opts.images_dir
                else output_path.parent / "images"
            )
            md_path, _ = self._extract_images_separately(
                input_path, output_path, images_dir,
                images_json_data=opts.images_data,
            )
            logger.info(
                "Separated images to %s, stripped MD at %s",
                images_dir, md_path,
            )
        else:
            # Legacy embed mode: base64 images get rewritten to temp files
            # so Pandoc embeds them in the DOCX.
            base64_images = self._find_base64_images(input_path)
            if base64_images:
                md_path, temp_dir = self._preprocess_md_images(input_path)
                logger.info(
                    "Extracted %d base64 images to temp directory",
                    len(base64_images),
                )

        # text-only mode: strip all image references and OLIMG placeholders
        # so pandoc produces a clean text-only DOCX.
        if opts.text_only or opts.separate_images:
            raw = md_path.read_text(encoding="utf-8")
            stripped = OLIMG_PATTERN.sub("", raw)
            stripped = IMAGE_REF_PATTERN.sub("", stripped)
            if stripped != raw:
                text_only_path = md_path.parent / f"{md_path.stem}_textonly{md_path.suffix}"
                atomic_write_text(text_only_path, stripped.strip())
                md_path = text_only_path
                logger.info(
                    "Text-only mode: stripped %d chars of image references",
                    len(raw) - len(stripped),
                )

        # For large MD files, use chunked conversion to avoid OOM.
        if md_path.stat().st_size > self.chunk_size:
            logger.info(
                "File size %d bytes exceeds chunk threshold %d, using chunked conversion",
                md_path.stat().st_size, self.chunk_size,
            )
            return self._convert_chunked(md_path, output_path, opts, source_path=input_path)

        cmd = [
            "pandoc",
            str(md_path),
            "-o", str(output_path),
            "--to", "docx",
            "--from", "markdown-smart",
        ]

        template = opts.template or self.reference_docx
        if template:
            cmd.extend(["--reference-doc", str(template)])

        cmd[1] = str(md_path.resolve())
        cmd[3] = str(output_path.resolve())

        try:
            logger.info(f"Running: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                cwd=str(md_path.parent),
                timeout=_CONVERSION_TIMEOUT,
                env=_deterministic_pandoc_env(input_path),
            )

            logger.debug(f"Pandoc output: {result.stdout}")
            if result.stderr:
                if opts.text_only or opts.separate_images:
                    logger.debug(f"Pandoc stderr: {result.stderr}")
                else:
                    logger.warning(f"Pandoc stderr: {result.stderr}")

            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={"tool": "pandoc", "cmd": " ".join(cmd)},
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Pandoc timed out after {_CONVERSION_TIMEOUT}s")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Pandoc timed out after {_CONVERSION_TIMEOUT}s"],
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Pandoc failed: {e.stderr}")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Pandoc error: {e.stderr}"],
            )
        except FileNotFoundError:
            logger.error("Pandoc not found in PATH")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=["Pandoc not installed or not in PATH"],
            )
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _convert_chunked(
        self,
        md_path: Path,
        output_path: Path,
        opts: ConverterOptions,
        source_path: Path | None = None,
    ) -> ConversionResult:
        """Convert a large MD file by splitting into chunks and processing each via pandoc.

        Uses :class:`ChunkedMDConverter` to split the MD by ``##`` H2 headers,
        converts each chunk through a separate pandoc invocation, then merges
        the resulting DOCX fragments into a single output file.
        """
        splitter = ChunkedMDConverter(
            chunk_size=self.chunk_size,
            manifest=self.manifest,
            frontmatter=self.frontmatter,
        )
        chunks = splitter._split_chunks(md_path)

        if not chunks:
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=["Input file produced no chunks"],
            )

        chunk_temp_dir = Path(tempfile.mkdtemp(prefix="orf_md_chunks_"))
        pandoc_env = _deterministic_pandoc_env(source_path or md_path)
        try:
            docx_chunks: list[Path] = []
            for chunk in chunks:
                chunk_md = chunk_temp_dir / f"chunk_{chunk.index:04d}.md"
                chunk_md.write_text(chunk.content, encoding="utf-8")

                chunk_docx = chunk_temp_dir / f"chunk_{chunk.index:04d}.docx"
                cmd = [
                    "pandoc",
                    str(chunk_md),
                    "-o", str(chunk_docx),
                    "--to", "docx",
                    "--from", "markdown-smart",
                ]
                template = opts.template or self.reference_docx
                if template:
                    cmd.extend(["--reference-doc", str(template)])

                logger.info("Running chunk %d: %s", chunk.index, " ".join(cmd))
                subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=_CONVERSION_TIMEOUT, env=pandoc_env)
                docx_chunks.append(chunk_docx)

            merge_cmd = ["pandoc"] + [str(p) for p in docx_chunks] + [
                "-o", str(output_path),
            ]
            logger.info(
                "Merging %d DOCX chunks: %s",
                len(docx_chunks), " ".join(merge_cmd),
            )
            subprocess.run(merge_cmd, capture_output=True, text=True, check=True, timeout=_CONVERSION_TIMEOUT, env=pandoc_env)

            return ConversionResult(
                output_path=output_path,
                success=True,
                metadata={
                    "tool": "pandoc",
                    "cmd": " ".join(merge_cmd),
                    "chunked": True,
                    "chunk_count": len(chunks),
                },
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Chunked pandoc timed out after {_CONVERSION_TIMEOUT}s")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Pandoc timed out after {_CONVERSION_TIMEOUT}s"],
            )
        except subprocess.CalledProcessError as e:
            logger.error("Chunked pandoc conversion failed: %s", e.stderr)
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=[f"Pandoc error in chunked conversion: {e.stderr}"],
            )
        except FileNotFoundError:
            logger.error("Pandoc not found in PATH")
            return ConversionResult(
                output_path=output_path,
                success=False,
                errors=["Pandoc not installed or not in PATH"],
            )
        finally:
            shutil.rmtree(chunk_temp_dir, ignore_errors=True)

    def _find_base64_images(self, md_path: Path) -> list[dict[str, Any]]:
        """Find all base64 data URI images in MD file."""
        images = []
        content = md_path.read_text(encoding="utf-8", errors="ignore")
        for match in IMAGE_PATTERN.finditer(content):
            images.append({
                "alt": match.group(1),
                "full_uri": match.group(2),
                "mime_type": f"image/{match.group(3)}",
                "base64_data": match.group(4),
            })
        return images

    def _preprocess_md_images(self, md_path: Path) -> tuple[Path, Path]:
        """Extract base64 images to files and rewrite MD references.

        Returns:
            Tuple of (modified_md_path, temp_dir_path)
        """
        content = md_path.read_text(encoding="utf-8", errors="ignore")
        temp_dir = Path(tempfile.mkdtemp(prefix="orf_md_images_"))
        images_dir = temp_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        def replace_base64(match: re.Match) -> str:
            alt = match.group(1)
            mime_ext = match.group(3)
            base64_data = match.group(4)
            try:
                img_bytes = base64.b64decode(base64_data)
            except (ValueError, TypeError):
                logger.exception("Failed to decode base64 image data, returning raw match")
                return match.group(0)
            img_hash = hashlib.md5(img_bytes).hexdigest()[:12]
            ext = "png" if mime_ext == "png" else mime_ext
            filename = f"image_{img_hash}.{ext}"
            filepath = images_dir / filename
            filepath.write_bytes(img_bytes)
            return f"![{alt}]({filepath.as_posix()})"

        new_content = IMAGE_PATTERN.sub(replace_base64, content)
        new_md_path = temp_dir / md_path.name
        new_md_path.write_text(new_content, encoding="utf-8")

        return new_md_path, temp_dir

    def _extract_images_separately(
        self,
        input_path: Path,
        output_path: Path,
        images_dir: Path,
        images_json_data: list[dict] | None = None,
    ) -> tuple[Path, None]:
        """Extract all images to ``images_dir``, produce ``images.zip`` and ``images.json``.

        Uses OPP ``images_json_data`` (from the pipeline's ``images.json``) as the
        primary image source.  Images are written to ``images_dir/``, packed into
        ``images.zip``, and metadata is written to ``images.json`` — all three live
        alongside the output DOCX.

        Returns:
            Tuple of (stripped_md_path, None) where stripped_md_path is a copy of
            the input MD with all ``![...]()`` references removed.
        """
        output_dir = output_path.parent
        images_dir.mkdir(parents=True, exist_ok=True)
        content = input_path.read_text(encoding="utf-8", errors="ignore")

        # Build image set from OPP images_json_data (primary).
        img_entries: list[dict] = []
        decoded_count = 0
        if images_json_data:
            for img in images_json_data:
                try:
                    b64_data = img.get("data_base64") or img.get("data")
                    if not b64_data:
                        continue
                    img_bytes = base64.b64decode(b64_data)
                    mime = img.get("mime_type", "image/png")
                    # Derive extension from mime type
                    ext = mime.split("/")[-1] if "/" in mime else "png"
                    if ext == "jpeg":
                        ext = "jpg"
                    img_hash = hashlib.md5(img_bytes).hexdigest()[:12]
                    filename = f"OLIMG_{decoded_count:04d}_{img_hash}.{ext}"
                    filepath = images_dir / filename
                    atomic_write_bytes(filepath, img_bytes)
                    img_entries.append({
                        "filename": filename,
                        "mime_type": mime,
                        "paragraph_index": img.get("paragraph_index"),
                        "size_bytes": len(img_bytes),
                        "md5_hash": img_hash,
                    })
                    decoded_count += 1
                except Exception as e:
                    logger.warning("Failed to decode OPP image entry: %s", e)

        # Also try to resolve image file refs in the MD content (fallback).
        # Some OPP outputs write image files to a directory next to the MD.
        for match in IMAGE_REF_PATTERN.finditer(content):
            ref = match.group(2)

            # Already handled via base64 from images_json_data — skip.
            if ref.startswith("data:image/"):
                continue

            # Try resolving as a local file relative to input_path.
            try:
                img_path = (input_path.parent / ref).resolve()
                if img_path.exists():
                    data = img_path.read_bytes()
                    img_hash = hashlib.md5(data).hexdigest()[:12]
                    ext = img_path.suffix.lstrip(".") or "png"
                    # Deduplicate by hash
                    if not any(e["md5_hash"] == img_hash for e in img_entries):
                        filename = f"OLIMG_{decoded_count:04d}_{img_hash}.{ext}"
                        filepath = images_dir / filename
                        atomic_write_bytes(filepath, data)
                        img_entries.append({
                            "filename": filename,
                            "mime_type": f"image/{ext}",
                            "paragraph_index": None,
                            "size_bytes": len(data),
                            "md5_hash": img_hash,
                        })
                        decoded_count += 1
            except Exception as e:
                logger.warning("Failed to resolve image ref %s: %s", ref, e)

        keep = {e["filename"] for e in img_entries}
        for stale in images_dir.glob(_OLIMG_GLOB):
            if stale.name not in keep:
                stale.unlink(missing_ok=True)

        # Write manifest images.json alongside output.
        manifest_path = output_dir / "images.json"
        manifest_payload = {
            "images": img_entries,
            "total": len(img_entries),
            "source": "OPP extracted via md2docx separation",
        }
        atomic_write_bytes(
            manifest_path,
            json.dumps(manifest_payload, indent=2).encode("utf-8"),
        )
        logger.info("Wrote image manifest to %s (%d images)", manifest_path, len(img_entries))

        # Pack images into images.zip.
        zip_path = output_dir / "images.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in sorted(img_entries, key=lambda e: e["filename"]):
                filepath = images_dir / entry["filename"]
                if not filepath.exists():
                    continue
                info = zipfile.ZipInfo(entry["filename"], date_time=_ZIP_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, filepath.read_bytes())
        atomic_write_bytes(zip_path, buffer.getvalue())
        logger.info("Packed %d images into %s", len(img_entries), zip_path)

        # Strip ALL image references from MD.
        stripped_content = content
        stripped_content = OLIMG_PATTERN.sub("", stripped_content)
        stripped_content = IMAGE_REF_PATTERN.sub("", stripped_content)
        stripped_content = stripped_content.strip()

        stripped_path = output_dir / f"{input_path.stem}.stripped.md"
        atomic_write_bytes(stripped_path, stripped_content.encode("utf-8"))

        return stripped_path, None

    def inject_images(
        self,
        skeleton_path: Path | str,
        images: list,
        output_path: Path | str,
    ) -> tuple[list, list]:
        """MD2DOCX uses pre-processing approach for images.

        Images embedded in MD as base64 data URIs are extracted to temp files
        during convert() and rewritten as local file references before Pandoc.
        No post-conversion injection needed.

        The `images` parameter (from images_json) is NOT used in MD pipeline.
        MD2DOCX extracts images directly from base64 in the markdown content.

        For precise paragraph-level image placement, use the XLIFF pipeline instead.
        This method returns ([], images) meaning all images are treated as orphaned.

        Returns:
            tuple[list, list]: First list is empty (no injected images), second list
                              contains all images as orphaned.
        """
        logger.debug(
            "MD2DOCX handles images via pre-processing. "
            "Base64 images are extracted and rewritten as local file references before Pandoc."
        )
        return ([], images)
