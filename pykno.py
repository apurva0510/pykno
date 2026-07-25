#!/usr/bin/env python3
"""Object-preserving, entirely local PDF image compressor."""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pikepdf
from PIL import Image


@dataclass(frozen=True)
class Settings:
    dpi: int
    quality: int


PRESETS = {
    "small": Settings(dpi=96, quality=50),
    "balanced": Settings(dpi=144, quality=72),
    "print": Settings(dpi=220, quality=85),
}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pykno",
        description=(
            "Compress raster images inside PDFs without uploading anything. "
            "Text, vectors, links, outlines, forms, annotations, and page geometry "
            "are preserved because pages are not redrawn."
        )
    )
    p.add_argument("inputs", nargs="+", type=Path, help="PDF files to process")
    p.add_argument(
        "--preset", choices=PRESETS, default="balanced", help="default: balanced"
    )
    p.add_argument(
        "--engine",
        choices=("objects", "quartz", "auto"),
        default="objects",
        help=(
            "objects preserves PDF structure (default); quartz redraws pages; "
            "auto uses Quartz only when object compression cannot help"
        ),
    )
    p.add_argument("--dpi", type=int, help="image resolution cap, 36-600")
    p.add_argument("--quality", type=int, help="JPEG quality, 0-100")
    p.add_argument(
        "--output", type=Path, help="output directory; default is beside each input"
    )
    p.add_argument("--suffix", default="-compressed", help="default: -compressed")
    p.add_argument("--force", action="store_true", help="replace existing outputs")
    p.add_argument(
        "--password-prompt",
        action="store_true",
        help="securely prompt for an input PDF password when needed",
    )
    p.add_argument(
        "--strip-metadata",
        action="store_true",
        help="remove the document Info dictionary and XMP metadata",
    )
    return p


def checked_settings(args: argparse.Namespace) -> Settings:
    base = PRESETS[args.preset]
    dpi = args.dpi if args.dpi is not None else base.dpi
    quality = args.quality if args.quality is not None else base.quality
    if not 36 <= dpi <= 600:
        raise ValueError("--dpi must be between 36 and 600")
    if not 0 <= quality <= 100:
        raise ValueError("--quality must be between 0 and 100")
    return Settings(dpi=dpi, quality=quality)


def output_path(source: Path, args: argparse.Namespace) -> Path:
    directory = args.output.expanduser() if args.output else source.parent
    return directory / f"{source.stem}{args.suffix}.pdf"


def open_pdf(path: Path, prompt: bool) -> tuple[pikepdf.Pdf, str]:
    try:
        return pikepdf.open(path), ""
    except pikepdf.PasswordError:
        if not prompt:
            raise RuntimeError(
                "password-protected PDF; rerun with --password-prompt"
            ) from None
        password = getpass.getpass(f"Password for {path.name}: ")
        try:
            return pikepdf.open(path, password=password), password
        except pikepdf.PasswordError:
            raise RuntimeError("incorrect PDF password") from None


def page_dimension_cap(pdf: pikepdf.Pdf, dpi: int) -> int:
    """Conservative cap: requested DPI for the largest full page in the PDF."""
    largest_points = 0.0
    for page in pdf.pages:
        box = [float(v) for v in page.MediaBox]
        largest_points = max(
            largest_points, abs(box[2] - box[0]), abs(box[3] - box[1])
        )
    return max(1, round(largest_points * dpi / 72.0))


def filter_names(obj: pikepdf.Object) -> set[str]:
    value = obj.get("/Filter")
    if value is None:
        return set()
    if isinstance(value, pikepdf.Array):
        return {str(item) for item in value}
    return {str(value)}


def raw_length(obj: pikepdf.Object) -> int:
    try:
        return len(obj.read_raw_bytes())
    except (pikepdf.PdfError, ValueError):
        return int(obj.get("/Length", 0))


def compress_image(
    obj: pikepdf.Object, *, max_dimension: int, quality: int
) -> tuple[bool, int, int, str]:
    if obj.get("/Subtype") != pikepdf.Name.Image:
        return False, 0, 0, "not an image"
    if obj.get("/ImageMask", False):
        return False, 0, 0, "image mask"
    if obj.get("/SMask") is not None or obj.get("/Mask") is not None:
        return False, 0, 0, "has transparency or mask"
    if int(obj.get("/BitsPerComponent", 8)) != 8:
        return False, 0, 0, "not 8-bit"

    before = raw_length(obj)
    try:
        with pikepdf.PdfImage(obj).as_pil_image(
            apply_decode_array=True, apply_mask=False
        ) as extracted:
            image = extracted.copy()
    except (pikepdf.PdfError, ValueError, OSError, NotImplementedError) as exc:
        return False, before, before, f"decode failed: {type(exc).__name__}"

    if image.mode == "L":
        color_space = pikepdf.Name.DeviceGray
    elif image.mode in {"RGB", "P"}:
        image = image.convert("RGB")
        color_space = pikepdf.Name.DeviceRGB
    else:
        return False, before, before, f"unsupported color mode {image.mode}"

    original_size = image.size
    resized = max(image.size) > max_dimension
    if resized:
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    encoded = BytesIO()
    image.save(
        encoded,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=False,
        subsampling=0 if quality >= 85 else 2,
    )
    jpeg = encoded.getvalue()

    # Avoid generational loss and file growth for an already smaller JPEG.
    if not resized and "/DCTDecode" in filter_names(obj) and len(jpeg) >= before:
        return False, before, before, "existing JPEG is smaller"
    if not resized and len(jpeg) >= before:
        return False, before, before, "replacement is not smaller"

    obj.write(jpeg, filter=pikepdf.Name.DCTDecode)
    obj["/Width"] = image.width
    obj["/Height"] = image.height
    obj["/BitsPerComponent"] = 8
    obj["/ColorSpace"] = color_space
    for key in (
        "/Decode",
        "/DecodeParms",
        "/Matte",
        "/SMaskInData",
    ):
        if obj.get(key) is not None:
            del obj[key]

    detail = (
        f"{original_size[0]}x{original_size[1]} -> {image.width}x{image.height}"
        if resized
        else f"{image.width}x{image.height}"
    )
    return True, before, len(jpeg), detail


def compress_pdf(
    source: Path,
    destination: Path,
    settings: Settings,
    args: argparse.Namespace,
) -> dict[str, int]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise RuntimeError("input does not exist or is not a PDF file")
    if source == destination:
        raise RuntimeError("refusing to overwrite the input file")
    if destination.exists() and not args.force:
        raise RuntimeError(f"output exists (use --force): {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    pdf, password = open_pdf(source, args.password_prompt)
    temporary: Path | None = None
    try:
        encrypted = pdf.is_encrypted
        source_pages = len(pdf.pages)
        max_dimension = page_dimension_cap(pdf, settings.dpi)
        visited: set[tuple[int, int] | int] = set()
        stats = {
            "found": 0,
            "changed": 0,
            "before": 0,
            "after": 0,
            "skipped": 0,
        }

        for page in pdf.pages:
            for obj in page.get_images(recursive=True).values():
                identity: tuple[int, int] | int = obj.objgen
                if identity == (0, 0):
                    identity = id(obj)
                if identity in visited:
                    continue
                visited.add(identity)
                stats["found"] += 1
                changed, before, after, _detail = compress_image(
                    obj, max_dimension=max_dimension, quality=settings.quality
                )
                stats["before"] += before
                stats["after"] += after
                if changed:
                    stats["changed"] += 1
                else:
                    stats["skipped"] += 1

        if args.strip_metadata:
            if pdf.Root.get("/Metadata") is not None:
                del pdf.Root["/Metadata"]
            for key in list(pdf.docinfo.keys()):
                del pdf.docinfo[key]

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        pdf.save(
            temporary,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            encryption=True if encrypted else False,
        )
        pdf.close()

        try:
            check = pikepdf.open(temporary, password=password)
            if len(check.pages) != source_pages or len(check.pages) == 0:
                raise RuntimeError(
                    f"validation failed: expected {source_pages} pages, "
                    f"found {len(check.pages)}"
                )
            syntax_issues = check.check_pdf_syntax()
            if syntax_issues:
                raise RuntimeError(
                    "output validation reported: " + "; ".join(syntax_issues)
                )
            check.close()
        except pikepdf.PdfError as exc:
            raise RuntimeError(f"output validation failed: {exc}") from exc

        os.replace(temporary, destination)
        shutil.copystat(source, destination)
    finally:
        pdf.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return stats


def compress_quartz(
    source: Path,
    destination: Path,
    settings: Settings,
    args: argparse.Namespace,
    *,
    replace: bool = False,
) -> None:
    """Invoke the bundled local macOS Quartz engine."""
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise RuntimeError("input does not exist or is not a PDF file")
    if source == destination:
        raise RuntimeError("refusing to overwrite the input file")
    if destination.exists() and not (args.force or replace):
        raise RuntimeError(f"output exists (use --force): {destination}")
    executable = Path(__file__).resolve().parent / "pykno-quartz"
    if not executable.is_file():
        raise RuntimeError(f"bundled Quartz engine is missing: {executable}")
    command = [
        str(executable),
        "--engine",
        "quartz",
        "--dpi",
        str(settings.dpi),
        "--quality",
        str(settings.quality),
        "--output",
        str(destination.parent),
        "--suffix",
        args.suffix,
    ]
    if args.force or replace:
        command.append("--force")
    command.append(str(source))

    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Quartz compression failed: {detail}")
    if not destination.is_file():
        raise RuntimeError("Quartz compression did not create the expected output")

    try:
        with pikepdf.open(destination) as check:
            syntax_issues = check.check_pdf_syntax()
    except pikepdf.PdfError as exc:
        raise RuntimeError(f"Quartz output validation failed: {exc}") from exc
    if syntax_issues:
        raise RuntimeError(
            "Quartz output validation reported: " + "; ".join(syntax_issues)
        )
    shutil.copystat(source, destination)


def try_quartz_fallback(
    source: Path,
    destination: Path,
    settings: Settings,
    args: argparse.Namespace,
    object_size: int,
) -> str:
    """Try Quartz without sacrificing an already valid object-engine result."""
    backup_fd, backup_name = tempfile.mkstemp(
        prefix=f".{destination.name}.objects.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(backup_fd)
    backup = Path(backup_name)
    try:
        shutil.copy2(destination, backup)
        try:
            compress_quartz(source, destination, settings, args, replace=True)
        except (RuntimeError, OSError, pikepdf.PdfError) as exc:
            os.replace(backup, destination)
            print(
                f"  warning: Quartz fallback unavailable; kept object result: {exc}",
                file=sys.stderr,
            )
            return "objects (auto; Quartz fallback unavailable)"
        if destination.stat().st_size < object_size:
            return "quartz (automatic fallback)"
        os.replace(backup, destination)
        return "objects (auto; Quartz was not smaller)"
    finally:
        backup.unlink(missing_ok=True)


def human_size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1000 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1000
    raise AssertionError


def main() -> int:
    args = parser().parse_args()
    try:
        settings = checked_settings(args)
    except ValueError as exc:
        parser().error(str(exc))

    failures = 0
    for source_arg in args.inputs:
        source = source_arg.expanduser()
        destination = output_path(source, args)
        try:
            before = source.stat().st_size
            stats: dict[str, int] | None = None
            engine_used = args.engine
            if args.engine == "quartz":
                compress_quartz(source, destination, settings, args)
            else:
                stats = compress_pdf(source, destination, settings, args)
                object_size = destination.stat().st_size
                should_fallback = (
                    args.engine == "auto"
                    and stats["found"] > 0
                    and (stats["changed"] == 0 or object_size >= before)
                )
                if should_fallback:
                    engine_used = try_quartz_fallback(
                        source, destination, settings, args, object_size
                    )
                elif args.engine == "auto":
                    engine_used = "objects (auto)"
            after = destination.stat().st_size
            change = (after / before - 1) * 100 if before else 0.0
            print(destination.resolve())
            print(
                f"  {human_size(before)} -> {human_size(after)} ({change:+.1f}%)"
            )
            print(f"  engine: {engine_used}")
            if stats is not None and not engine_used.startswith("quartz"):
                print(
                    f"  images: {stats['changed']} recompressed, "
                    f"{stats['skipped']} unchanged"
                )
            if after >= before:
                print(
                    "  warning: output is not smaller; try --preset small",
                    file=sys.stderr,
                )
        except (RuntimeError, OSError, pikepdf.PdfError) as exc:
            print(f"error: {source}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
