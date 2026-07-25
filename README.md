# Pykno

Pykno is a private, object-preserving command-line PDF compressor. It runs entirely on your Mac and contains no networking code. Original files are never overwritten.

The recommended engine uses local Python packages:

- pikepdf, which embeds the QPDF PDF parser/writer;
- Pillow, which decodes, resizes, and JPEG-encodes raster images.

These packages do not upload or remotely process documents. The setup command contacts PyPI only to download and install software into this project's local .venv. PDF processing itself makes no network requests.

## Setup

    ./setup-pykno

## Quick start

    ./pykno confidential.pdf

The default balanced mode caps full-page images at 144 dpi and uses 72 percent JPEG quality. It writes confidential-compressed.pdf beside the original.

For stronger compression with explicit image settings:

    ./pykno --preset small scan.pdf
    ./pykno --preset print report.pdf

Batch processing and a separate output folder:

    ./pykno --preset balanced --output ./compressed one.pdf two.pdf three.pdf

Custom image settings:

    ./pykno --dpi 110 --quality 62 confidential.pdf

Choose the processing engine through the same command:

    ./pykno --engine objects document.pdf
    ./pykno --engine quartz document.pdf
    ./pykno --engine auto document.pdf

Run ./pykno --help for every option.

## How it works

The Python engine finds raster image XObjects, decodes eligible unmasked 8-bit RGB or grayscale images, downsamples images above the requested resolution, and replaces them with locally encoded JPEG streams. Pages are not redrawn. Text, vectors, links, forms, annotations, outlines, metadata, page boxes, and encryption are preserved.

Images with transparency, masks, unusual bit depths, or unsupported color modes are deliberately left unchanged.

The Quartz redraw implementation is built into the main command:

    ./pykno --engine quartz --preset small document.pdf

Quartz handles some image types the object-preserving engine skips, but may discard links, forms, outlines, attachments, tags, and other document-level structures.

The auto engine starts with object-preserving compression. If the document contains images but none can be recompressed, or if the result is not smaller, it also tries the original PDF with Quartz. It compares both local results and keeps the smaller one. The output reports which engine won.

If Quartz is unavailable or cannot open the input, auto mode keeps the valid object-preserving result and reports a warning instead of failing the whole operation.

Neither engine performs OCR. For a locked PDF, add --password-prompt. The password is read securely instead of being placed in shell history.

## Privacy and safety

- There are no HTTP, analytics, telemetry, account, or cloud APIs in the source.
- The recommended engine uses only pikepdf/QPDF, Pillow, and local files.
- Output is first written to a temporary file, reopened, and checked for the same nonzero page count.
- The temporary file is moved into place only after validation.
- Existing output is preserved unless --force is supplied.
- The input path can never be the output path.

For highly sensitive documents, remember that Spotlight, Time Machine, cloud-synced folders, and other system services are separate from this program. Use a nonsynced local directory if those matter to your threat model.

## Python environment

Dependencies are isolated under .venv and listed in requirements.txt. To rebuild the environment:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt

## Scope

This is a clean-room local implementation based on documented macOS APIs and observable PDFOptim behavior. See REVERSE_ENGINEERING.md for the technical analysis and the roadmap for a more advanced object-preserving Graphics engine.
