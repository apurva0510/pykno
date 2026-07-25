# Pykno

Pykno is a local-first, object-preserving command-line PDF compressor for macOS. It contains no networking code, performs compression entirely on your Mac, and never overwrites an original file.

The recommended engine uses local Python packages:

- pikepdf, which embeds the QPDF PDF parser/writer;
- Pillow, which decodes, resizes, and JPEG-encodes raster images.

These packages do not upload or remotely process documents. Installation tools may contact their package indexes to download dependencies; PDF processing itself makes no network requests.

## Install

Until versioned packages are published, clone the repository and install an isolated command with `pipx`:

```Shell
pipx install .
pykno --version
```

For development, create the repository-local environment:

```Shell
./setup-pykno
```

This creates an isolated `.venv` and installs an editable `pykno` command.

## Quick start

```Shell
./pykno confidential.pdf
```

The default balanced mode caps full-page images at 144 dpi and uses 72 percent JPEG quality. It writes confidential-compressed.pdf beside the original.

For stronger compression with explicit image settings:

```Shell
./pykno --preset small scan.pdf
./pykno --preset print report.pdf
```

Batch processing and a separate output folder:

```Shell
./pykno --preset balanced --output ./compressed one.pdf two.pdf three.pdf
```

Custom image settings:

```Shell
./pykno --dpi 110 --quality 62 confidential.pdf
```

Choose the processing engine through the same command:

```Shell
./pykno --engine objects document.pdf
./pykno --engine quartz document.pdf
./pykno --engine auto document.pdf
```

Run ./pykno --help for every option.

## Finder Quick Action

After installing Pykno somewhere outside `~/Documents`, add the configurable Finder action with:

```Shell
pykno --install-quick-action
```

Right-click one or more PDFs and choose **Quick Actions → Compress with Pykno**. The action prompts once for Small, Balanced, Print, or custom DPI and JPEG quality settings.

Remove it with:

```Shell
pykno --uninstall-quick-action
```

Finder restricts workflows from reading development tools inside `~/Documents`. A package-manager installation avoids that macOS privacy boundary.

## How it works

The Python engine finds raster image XObjects, decodes eligible unmasked 8-bit RGB or grayscale images, and calculates effective resolution from each image's page-space transformation matrix. It accounts for shared images and images nested in Form XObjects, downsamples images above the requested resolution, and replaces them with locally encoded JPEG streams only when the replacement is smaller. Malformed or untraceable content falls back to a conservative page-size cap.

Pages are not redrawn. Text, vectors, links, forms, annotations, outlines, metadata, page boxes, and encryption are preserved.

Images with transparency, masks, unusual bit depths, or unsupported color modes are deliberately left unchanged.

The Quartz redraw implementation is built into the main command:

```Shell
./pykno --engine quartz --preset small document.pdf
```

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

```Shell
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Platform support

The object-preserving engine is Python-based and designed to remain portable. The Quartz fallback and Finder Quick Action require macOS. On other platforms, `--engine auto` safely remains on the object engine and `--engine quartz` reports that Quartz is unavailable.

The Quartz helper compiles on first use and requires the Xcode Command Line Tools.

## Known limitations

- Images with masks, transparency, unusual bit depths, or unsupported color modes are skipped.
- The object engine does not perform OCR, bitonal conversion, or vector-stream simplification.
- The Quartz engine may discard links, forms, outlines, attachments, tags, and other document-level structures.
- Effective-DPI analysis handles transformation matrices and nested Form XObjects, but malformed or untraceable content uses a conservative fallback.
- Compression is lossy for images that are replaced with JPEG. Keep the original PDF.

## Scope

This is an independent clean-room implementation based on documented PDF and macOS APIs plus observable application behavior. It contains no copied proprietary source code or bundled third-party application assets.

## Development

Run the test suite with:

```Shell
.venv/bin/python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidance and [SECURITY.md](SECURITY.md) for responsible vulnerability reporting.

## License

Pykno is available under the [Apache License 2.0](LICENSE).
