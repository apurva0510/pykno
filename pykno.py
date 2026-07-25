#!/usr/bin/env python3
"""Object-preserving, entirely local PDF image compressor."""

from __future__ import annotations

import argparse
import getpass
import math
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterator

import pikepdf
from PIL import Image


__version__ = "0.1.0"


@dataclass(frozen=True)
class Settings:
    dpi: int
    quality: int


PRESETS = {
    "small": Settings(dpi=96, quality=50),
    "balanced": Settings(dpi=144, quality=72),
    "print": Settings(dpi=220, quality=85),
}

# PDFs are untrusted binary input. These limits allow high-resolution scanned
# pages while preventing a single document from requesting unbounded decoding
# or recursive Form traversal.
MAX_IMAGE_PIXELS = 50_000_000
MAX_DOCUMENT_PIXELS = 250_000_000
MAX_FORM_DEPTH = 64
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pykno",
        description=(
            "Compress raster images inside PDFs without uploading anything. "
            "Text, vectors, links, outlines, forms, annotations, and page geometry "
            "are preserved because pages are not redrawn."
        )
    )
    p.add_argument("inputs", nargs="*", type=Path, help="PDF files to process")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
    service = p.add_mutually_exclusive_group()
    service.add_argument(
        "--install-quick-action",
        action="store_true",
        help="install the configurable Finder Quick Action",
    )
    service.add_argument(
        "--uninstall-quick-action",
        action="store_true",
        help="remove the Finder Quick Action",
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
    if "/" in args.suffix or "\\" in args.suffix or "\0" in args.suffix:
        raise RuntimeError("--suffix must not contain path separators")
    directory = args.output.expanduser() if args.output else source.parent
    return directory / f"{source.stem}{args.suffix}.pdf"


def resolved_output_path(path: Path) -> Path:
    """Resolve the output directory without following the final path entry."""
    expanded = path.expanduser()
    return expanded.parent.resolve() / expanded.name


def reject_output_symlink(destination: Path) -> None:
    """Never allow --force to turn a predictable output symlink into data loss."""
    if destination.is_symlink():
        raise RuntimeError(f"refusing to replace an output symlink: {destination}")


def install_output(temporary: Path, destination: Path, *, force: bool) -> None:
    """Atomically install a validated output, respecting no-overwrite mode."""
    reject_output_symlink(destination)
    if force:
        # The final component is intentionally unresolved, so a symlink created
        # after the check is replaced rather than followed.
        os.replace(temporary, destination)
        return
    try:
        # The temporary file is in the same directory. Creating a hard link is
        # an atomic create-if-absent operation on supported local filesystems.
        os.link(temporary, destination)
    except FileExistsError:
        raise RuntimeError(f"output exists (use --force): {destination}") from None
    except OSError as exc:
        raise RuntimeError(f"could not atomically install output: {exc}") from exc
    temporary.unlink()


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


def page_dimension_cap(pdf: pikepdf.Pdf, dpi: int) -> tuple[int, int]:
    """Conservative cap: requested DPI for the largest full page in the PDF."""
    largest_width = 0.0
    largest_height = 0.0
    for page in pdf.pages:
        box = [float(v) for v in page.MediaBox]
        user_unit = float(page.obj.get("/UserUnit", 1))
        largest_width = max(largest_width, abs(box[2] - box[0]) * user_unit)
        largest_height = max(largest_height, abs(box[3] - box[1]) * user_unit)
    return (
        max(1, math.ceil(largest_width * dpi / 72.0)),
        max(1, math.ceil(largest_height * dpi / 72.0)),
    )


def object_identity(obj: pikepdf.Object) -> tuple[int, int] | int:
    identity: tuple[int, int] | int = obj.objgen
    return id(obj) if identity == (0, 0) else identity


def _image_placements(
    content: pikepdf.Page | pikepdf.Object,
    resources: pikepdf.Object,
    initial_matrix: pikepdf.Matrix,
    active_forms: set[tuple[int, int] | int],
    depth: int = 0,
) -> Iterator[tuple[pikepdf.Object, pikepdf.Matrix]]:
    """Yield images and their page-space matrices, including nested forms."""
    if depth >= MAX_FORM_DEPTH:
        raise RuntimeError(
            f"PDF Form nesting exceeds the safety limit ({MAX_FORM_DEPTH})"
        )
    stack = [initial_matrix]
    for instruction in pikepdf.parse_content_stream(
        content, operators="q Q cm Do"
    ):
        operator = str(instruction.operator)
        operands = instruction.operands
        if operator == "q":
            stack.append(stack[-1])
        elif operator == "Q":
            if len(stack) > 1:
                stack.pop()
        elif operator == "cm":
            if len(operands) == 6:
                stack[-1] = stack[-1] @ pikepdf.Matrix(*operands)
        elif operator == "Do" and operands:
            xobjects = resources.get("/XObject")
            if xobjects is None:
                continue
            drawn = xobjects.get(operands[0])
            if drawn is None:
                continue
            subtype = drawn.get("/Subtype")
            if subtype == pikepdf.Name.Image:
                yield drawn, stack[-1]
            elif subtype == pikepdf.Name.Form:
                identity = object_identity(drawn)
                if identity in active_forms:
                    continue
                form_resources = drawn.get("/Resources", resources)
                form_matrix = pikepdf.Matrix(
                    *drawn.get("/Matrix", (1, 0, 0, 1, 0, 0))
                )
                yield from _image_placements(
                    drawn,
                    form_resources,
                    stack[-1] @ form_matrix,
                    active_forms | {identity},
                    depth + 1,
                )


def image_dimension_caps(
    pdf: pikepdf.Pdf, dpi: int
) -> dict[tuple[int, int] | int, tuple[int, int]]:
    """Calculate pixel caps from each image's largest rendered placement."""
    caps: dict[tuple[int, int] | int, tuple[int, int]] = {}
    for page in pdf.pages:
        try:
            resources = page.resources
        except AttributeError:
            continue
        user_unit = float(page.obj.get("/UserUnit", 1))
        try:
            placements = _image_placements(
                page, resources, pikepdf.Matrix.identity(), set()
            )
            for image, matrix in placements:
                width_points = math.hypot(matrix.a, matrix.b) * user_unit
                height_points = math.hypot(matrix.c, matrix.d) * user_unit
                cap = (
                    max(1, math.ceil(width_points * dpi / 72.0)),
                    max(1, math.ceil(height_points * dpi / 72.0)),
                )
                identity = object_identity(image)
                previous = caps.get(identity, (0, 0))
                caps[identity] = (
                    max(previous[0], cap[0]),
                    max(previous[1], cap[1]),
                )
        except (
            pikepdf.PdfError,
            TypeError,
            ValueError,
            NotImplementedError,
            RuntimeError,
            RecursionError,
        ):
            # Malformed content is still handled by the conservative fallback.
            continue
    return caps


def _resource_images(
    resources: pikepdf.Object,
    active_forms: set[tuple[int, int] | int] | None = None,
    depth: int = 0,
) -> Iterator[pikepdf.Object]:
    """Yield resource images without unbounded recursive Form traversal."""
    if depth >= MAX_FORM_DEPTH:
        return
    active = active_forms or set()
    try:
        xobjects = resources.get("/XObject")
        entries = () if xobjects is None else xobjects.values()
        for obj in entries:
            subtype = obj.get("/Subtype")
            if subtype == pikepdf.Name.Image:
                yield obj
            elif subtype == pikepdf.Name.Form:
                identity = object_identity(obj)
                if identity in active:
                    continue
                form_resources = obj.get("/Resources", resources)
                yield from _resource_images(
                    form_resources,
                    active | {identity},
                    depth + 1,
                )
    except (pikepdf.PdfError, TypeError, ValueError, RuntimeError, RecursionError):
        return


def raw_length(obj: pikepdf.Object) -> int:
    try:
        return max(0, int(obj.get("/Length", 0)))
    except (pikepdf.PdfError, TypeError, ValueError):
        return 0


def image_pixel_count(obj: pikepdf.Object) -> int:
    try:
        width = int(obj.get("/Width", 0))
        height = int(obj.get("/Height", 0))
    except (pikepdf.PdfError, TypeError, ValueError):
        return 0
    return max(0, width) * max(0, height)


def compress_image(
    obj: pikepdf.Object, *, max_size: tuple[int, int], quality: int
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
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with pikepdf.PdfImage(obj).as_pil_image(
                apply_decode_array=True, apply_mask=False
            ) as extracted:
                image = extracted.copy()
    except (
        pikepdf.PdfError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        ValueError,
        OSError,
        NotImplementedError,
    ) as exc:
        return False, before, before, f"decode failed: {type(exc).__name__}"

    if image.mode == "L":
        color_space = pikepdf.Name.DeviceGray
    elif image.mode in {"RGB", "P"}:
        image = image.convert("RGB")
        color_space = pikepdf.Name.DeviceRGB
    else:
        return False, before, before, f"unsupported color mode {image.mode}"

    original_size = image.size
    resized = image.width > max_size[0] or image.height > max_size[1]
    if resized:
        image.thumbnail(max_size, Image.Resampling.LANCZOS)

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

    # Never make the document larger merely to enforce the requested DPI cap.
    if len(jpeg) >= before:
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
    destination = resolved_output_path(destination)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise RuntimeError("input does not exist or is not a PDF file")
    if source == destination:
        raise RuntimeError("refusing to overwrite the input file")
    reject_output_symlink(destination)
    if destination.exists() and not args.force:
        raise RuntimeError(f"output exists (use --force): {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    pdf, password = open_pdf(source, args.password_prompt)
    temporary: Path | None = None
    try:
        encrypted = pdf.is_encrypted
        source_pages = len(pdf.pages)
        fallback_cap = page_dimension_cap(pdf, settings.dpi)
        placement_caps = image_dimension_caps(pdf, settings.dpi)
        visited: set[tuple[int, int] | int] = set()
        decoded_pixels = 0
        stats = {
            "found": 0,
            "changed": 0,
            "before": 0,
            "after": 0,
            "skipped": 0,
        }

        for page in pdf.pages:
            try:
                page_images = _resource_images(page.resources)
            except AttributeError:
                continue
            for obj in page_images:
                identity = object_identity(obj)
                if identity in visited:
                    continue
                visited.add(identity)
                stats["found"] += 1
                pixels = image_pixel_count(obj)
                if (
                    pixels > MAX_IMAGE_PIXELS
                    or decoded_pixels + pixels > MAX_DOCUMENT_PIXELS
                ):
                    before = raw_length(obj)
                    stats["before"] += before
                    stats["after"] += before
                    stats["skipped"] += 1
                    continue
                decoded_pixels += pixels
                changed, before, after, _detail = compress_image(
                    obj,
                    max_size=placement_caps.get(identity, fallback_cap),
                    quality=settings.quality,
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
            with pikepdf.open(temporary, password=password) as check:
                output_pages = len(check.pages)
                if output_pages != source_pages or output_pages == 0:
                    raise RuntimeError(
                        f"validation failed: expected {source_pages} pages, "
                        f"found {output_pages}"
                    )
                syntax_issues = check.check_pdf_syntax()
                if syntax_issues:
                    raise RuntimeError(
                        "output validation reported: " + "; ".join(syntax_issues)
                    )
        except pikepdf.PdfError as exc:
            raise RuntimeError(f"output validation failed: {exc}") from exc

        shutil.copystat(source, temporary)
        install_output(temporary, destination, force=args.force)
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
    destination = resolved_output_path(destination)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise RuntimeError("input does not exist or is not a PDF file")
    if source == destination:
        raise RuntimeError("refusing to overwrite the input file")
    reject_output_symlink(destination)
    if destination.exists() and not (args.force or replace):
        raise RuntimeError(f"output exists (use --force): {destination}")
    if sys.platform != "darwin":
        raise RuntimeError("the Quartz engine is available only on macOS")
    executable = quartz_executable()
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


def quartz_executable() -> Path:
    """Locate Quartz assets in a source checkout or installed Python package."""
    candidates = (
        Path(__file__).resolve().parent / "pykno-quartz",
        Path(sys.prefix) / "share" / "pykno" / "pykno-quartz",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def quick_action_path() -> Path:
    return Path.home() / "Library" / "Services" / "Compress with Pykno.workflow"


def _quick_action_command() -> str:
    module_path = Path(__file__).resolve()
    documents = Path.home() / "Documents"
    if module_path.is_relative_to(documents):
        raise RuntimeError(
            "Finder cannot reliably run code from Documents. Install Pykno with "
            "pipx or Homebrew, then rerun --install-quick-action."
        )
    installed_command = shutil.which("pykno")
    invocation_parts = (
        (installed_command,)
        if installed_command
        else (sys.executable, "-m", "pykno")
    )
    invocation = " ".join(shlex.quote(part) for part in invocation_parts)
    return f'''choice=$(/usr/bin/osascript \\
  -e 'set picked to choose from list {{"Small — 96 dpi / 50%", "Balanced — 144 dpi / 72%", "Print — 220 dpi / 85%", "Custom…"}} with title "Pykno" with prompt "Choose compression settings" default items {{"Balanced — 144 dpi / 72%"}}' \\
  -e 'if picked is false then return "Cancel"' \\
  -e 'return item 1 of picked') || exit 0

case "$choice" in
  "Small —"*) settings=(--preset small) ;;
  "Balanced —"*) settings=(--preset balanced) ;;
  "Print —"*) settings=(--preset print) ;;
  "Custom…")
    dpi=$(/usr/bin/osascript -e 'text returned of (display dialog "Image resolution cap (36–600 dpi)" with title "Pykno — Custom" default answer "144" buttons {{"Cancel", "Continue"}} default button "Continue")') || exit 0
    quality=$(/usr/bin/osascript -e 'text returned of (display dialog "JPEG quality (0–100)" with title "Pykno — Custom" default answer "72" buttons {{"Cancel", "Compress"}} default button "Compress")') || exit 0
    if [[ "$dpi" != <-> || "$dpi" -lt 36 || "$dpi" -gt 600 || "$quality" != <-> || "$quality" -lt 0 || "$quality" -gt 100 ]]; then
      /usr/bin/osascript -e 'display alert "Invalid Pykno settings" message "DPI must be 36–600 and quality must be 0–100." as critical'
      exit 1
    fi
    settings=(--dpi "$dpi" --quality "$quality") ;;
  *) exit 0 ;;
esac

failures=0
for pdf in "$@"; do
  if ! result=$({invocation} --engine auto "${{settings[@]}}" "$pdf" 2>&1); then
    failures=1
    /usr/bin/osascript \\
      -e 'on run argv' \\
      -e 'display alert "Pykno compression failed" message (item 1 of argv) as critical' \\
      -e 'end run' "$result"
  fi
done
exit "$failures"'''


def _quick_action_document(command: str) -> dict[str, object]:
    action_uuid = str(uuid.uuid4()).upper()
    return {
        "actions": [
            {
                "action": {
                    "ActionBundlePath": (
                        "/System/Library/Automator/Run Shell Script.action"
                    ),
                    "ActionName": "Run Shell Script",
                    "ActionParameters": {
                        "CheckedForUserDefaultShell": False,
                        "COMMAND_STRING": command,
                        "inputMethod": 1,
                        "shell": "/bin/zsh",
                        "source": "",
                    },
                    "AMAccepts": {
                        "Container": "List",
                        "Optional": True,
                        "Types": ["com.apple.cocoa.string"],
                    },
                    "AMActionVersion": "2.0.3",
                    "AMApplication": ["Automator"],
                    "AMParameterProperties": {
                        key: {}
                        for key in (
                            "CheckedForUserDefaultShell",
                            "COMMAND_STRING",
                            "inputMethod",
                            "shell",
                            "source",
                        )
                    },
                    "AMProvides": {
                        "Container": "List",
                        "Types": ["com.apple.cocoa.string"],
                    },
                    "BundleIdentifier": "com.apple.RunShellScript",
                    "Class Name": "RunShellScriptAction",
                    "UUID": action_uuid,
                },
                "isViewVisible": True,
            }
        ],
        "AMApplicationBuild": "523",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "connectors": {},
        "workflowMetaData": {
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def _quick_action_info() -> dict[str, object]:
    return {
        "NSServices": [
            {
                "NSMenuItem": {"default": "Compress with Pykno"},
                "NSMessage": "runWorkflowAsService",
                "NSRequiredContext": {
                    "NSApplicationIdentifier": "com.apple.finder"
                },
                "NSSendFileTypes": ["com.adobe.pdf"],
            }
        ]
    }


def refresh_services() -> None:
    pbs = Path("/System/Library/CoreServices/pbs")
    if pbs.is_file():
        subprocess.run([pbs, "-flush"], check=False)
        subprocess.run([pbs, "-update"], check=False)


def install_quick_action() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the Finder Quick Action is available only on macOS")
    destination = quick_action_path()
    if destination.is_symlink():
        raise RuntimeError(f"refusing to install through a symlink: {destination}")
    contents = destination / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(_quick_action_info(), stream)
    with (contents / "document.wflow").open("wb") as stream:
        plistlib.dump(_quick_action_document(_quick_action_command()), stream)
    refresh_services()
    print(f"installed: {destination}")


def uninstall_quick_action() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the Finder Quick Action is available only on macOS")
    destination = quick_action_path()
    if destination.is_symlink():
        raise RuntimeError(f"refusing to remove a symlinked workflow: {destination}")
    if destination.exists():
        shutil.rmtree(destination)
        refresh_services()
        print(f"removed: {destination}")
    else:
        print("Pykno Quick Action is not installed")


def planned_jobs(
    inputs: list[Path], args: argparse.Namespace
) -> list[tuple[Path, Path]]:
    """Resolve batch paths and reject collisions before writing any output."""
    jobs: list[tuple[Path, Path]] = []
    destinations: dict[Path, Path] = {}
    for source_arg in inputs:
        source = source_arg.expanduser()
        destination = resolved_output_path(output_path(source, args))
        previous = destinations.get(destination)
        if previous is not None:
            raise RuntimeError(
                f"multiple inputs target the same output {destination}: "
                f"{previous} and {source}"
            )
        destinations[destination] = source
        jobs.append((source, destination))
    return jobs


def main() -> int:
    args = parser().parse_args()
    if args.install_quick_action or args.uninstall_quick_action:
        try:
            if args.inputs:
                raise RuntimeError("Quick Action commands do not accept PDF inputs")
            if args.install_quick_action:
                install_quick_action()
            else:
                uninstall_quick_action()
        except (RuntimeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if not args.inputs:
        parser().error("provide at least one PDF file")
    try:
        settings = checked_settings(args)
    except ValueError as exc:
        parser().error(str(exc))

    try:
        jobs = planned_jobs(args.inputs, args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    failures = 0
    for source, destination in jobs:
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
                    and sys.platform == "darwin"
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
