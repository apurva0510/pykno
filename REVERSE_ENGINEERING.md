# PDFOptim 6.0 - Reverse-engineering report

Date inspected: 2026-07-24

Target: `/Applications/PDFOptim.app`

This report describes observable behavior, public bundle metadata, resources, linked libraries, Objective-C runtime metadata, and likely implementation structure. It does not cover bypassing App Store licensing, modifying the signed application, or reproducing proprietary source code verbatim.

## Executive summary

PDFOptim is a native, sandboxed AppKit application written primarily in Objective-C/Objective-C++. It ships as a universal Intel/Apple Silicon binary, requires macOS 12+, and uses PDFKit for viewing. Its custom compression engine statically embeds substantial PDF and image-processing code, including QPDF and Poppler/Xpdf-derived components plus JPEG, JPEG 2000, PNG, and TIFF codecs.

The app has two compression paths:

1. **Graphics filter (recommended):** parses the existing PDF, finds raster image objects, decodes them, optionally resamples and changes their color model, JPEG-encodes them, and replaces them while preserving the rest of the PDF. A separate content-stream pass reduces vector precision/detail. Optional cleanup removes alternate images, page thumbnails, the structure tree, and output intents.
2. **Quartz filter:** builds a Quartz filter dictionary and asks Apple's Quartz/Core Graphics printing pipeline to redraw and rewrite the whole document. This can transform images, text, and vector colors, but is less structurally conservative.

The application shell is a conventional AppKit document/batch UI: add PDFs and folders, compress automatically in background operation queues, retain multiple compressed variants, compare original/output using synchronized PDFKit views, and export through several destination policies.

## Confirmed bundle facts

| Property | Value |
|---|---|
| Bundle ID | `com.RootRiseTechnologies.PDFOptim` |
| App version | `6.0` |
| Build | `2026.0124.1643` |
| Architectures | `arm64`, `x86_64` |
| Minimum OS | macOS 12.0 |
| SDK / build tools | macOS 26 SDK, Xcode 26 |
| UI technology | AppKit storyboards and NIBs |
| Viewer | PDFKit |
| Persistence | `NSUserDefaults`; an effectively empty Core Data model is bundled |
| Automation | Two Intents/Shortcuts actions |
| Distribution | Mac App Store receipt and StoreKit IAP |

The executable links only Apple dynamic frameworks. The non-Apple PDF/image libraries are statically linked into the roughly 5 MB executable.

The sandbox entitlements allow:

- app-scoped and document-scoped security bookmarks;
- read/write access to files explicitly selected by the user;
- outbound network access, likely for StoreKit/support-related app functionality.

## Embedded technology

### Apple frameworks

- AppKit and Foundation: application and UI
- PDFKit: original/compressed preview and navigation
- Core Graphics, Core Image, ImageIO, CoreText, Quartz, QuartzCore: rendering and image/color transformations
- Accelerate: numeric/image operations
- StoreKit: subscriptions and lifetime purchase
- Intents: Shortcuts integration
- WebKit: in-app web/support/purchase content
- Security and IOKit: keychain/system support

### Statically embedded components

Binary strings and symbols establish the presence of:

- **QPDF** object model and writer (`QPDF`, `QPDFObjectHandle`, `QPDFWriter`, stream parsing, page-tree optimization, encryption handling);
- **Poppler/Xpdf lineage** (`GfxColorSpace` subclasses, XRef/PDF parsing, Xpdf diagnostics);
- JPEG and JPEG 2000 support (`libjpeg`-style symbols and OpenJPEG messages);
- libpng (the embedded diagnostic identifies an old 1.6.2 lineage);
- libtiff, including JPEG-in-TIFF support.

No evidence was found for Ghostscript or MuPDF.

## Compression model

### Manual option schema

The user options are persisted under these keys:

```text
COMPRESS_FilterType
COMPRESS_Quality
COMPRESS_Resolution
COMPRESS_Colorspace
COMPRESS_GraphicsLevel
COMPRESS_RemoveAlternateImages
COMPRESS_RemovePageThumbnailImages
COMPRESS_RemoveDocumentStructureHierarchy
COMPRESS_RemoveDocumentOutputIntent
```

Factory defaults are:

```text
mode: automatic, medium
manual filter: Graphics
JPEG quality: 50
resolution: 72 dpi
color model: keep original
vector optimization: none (-1)
all destructive cleanup switches: off
preset: "72 dpi, Medium quality"
```

The bundled manual preset grid is:

| Resolution | Quality |
|---:|---:|
| 50 dpi | 50% |
| 72 dpi | 0%, 50%, 100% |
| 144 dpi | 0%, 50%, 100% |
| 200 dpi | 0%, 80%, 100% |

The quality value is passed as a percentage at the application layer. The codec layer presumably normalizes it to its own range.

Color model enum exposed to Shortcuts:

```text
1 keep original
2 RGB
3 CMYK
4 Gray
5 Bitonal (Graphics filter only)
```

Vector optimization enum exposed to Shortcuts:

```text
100 none
5   level 1
4   level 2
3   level 3
2   level 4
1   level 5
```

The inverse numeric mapping strongly suggests the stored number is a precision/tolerance parameter: smaller values discard more numeric precision and produce more aggressive simplification.

### Graphics filter: reconstructed pipeline

The following is a high-confidence behavioral reconstruction, not recovered source:

```text
open input with robust PDF parser
validate/decrypt with supplied password when necessary
walk page tree
for each page:
    walk Resources/XObject recursively
    for each image XObject:
        preserve or resolve masks and associated metadata
        decode DCT, JPX, Flate, and other supported image streams
        determine effective pixel dimensions and page-space DPI
        if image DPI exceeds requested DPI:
            resample while preserving aspect ratio
        if requested:
            convert to RGB, CMYK, gray, or 1-bit bitonal
        unless bitonal:
            encode raster as JPEG at requested quality
        replace image stream and update Width/Height/ColorSpace/BitsPerComponent/Filter

    if vector optimization enabled:
        parse page content stream operators
        reduce excess coordinate/control-point precision according to level
        rewrite and recompress content streams

    optionally remove page /Thumb
    optionally remove alternate image entries

optionally remove catalog /StructTreeRoot
optionally remove catalog /OutputIntents
rewrite PDF through QPDF-style writer
```

Evidence supporting this reconstruction:

- The vendor manual explicitly says raster contents are converted/redrawn to JPEG and replaced back into the PDF.
- QPDF object parsing and writing are embedded.
- Poppler/Xpdf color-space and image decoders are embedded.
- The executable contains the relevant PDF dictionary keys: `/Resources`, `/XObject`, `/Image`, `/DCTDecode`, `/JPXDecode`, `/Thumb`, `/StructTreeRoot`, and `/OutputIntents`.
- Image and color conversion APIs are present, including bitmap retagging and CGImage creation.
- The Graphics path is documented as preserving document properties, unlike the Quartz redraw path.

Important edge handling implied by release notes includes JPEG 2000 YUV inputs, unusual page sizes, recursive XObjects, malformed PDFs, cancellation, password prompts, and cloud-backed file coordination.

### Quartz filter: reconstructed pipeline

The bundled `CompressionFilter.qfilter` uses Apple's standard Quartz filter format. Its baseline values are:

```text
ImageCompression: ImageJPEGCompress
Compression Quality: 1.0
ImageResolution: 144
ImageScaleInterpolate: 3
TargetColorSpace: 0
```

At runtime the app appears to clone/mutate this property dictionary through `quartzFilterWithProperties:` and feed it to the Quartz PDF rewrite path. The filter can change the color model of all rendered color content, including text and vectors. Bitonal and vector optimization are not supported by this path.

### Automatic modes

The public intent exposes four modes with stable integer values:

| Value | Mode | Documented behavior |
|---:|---|---|
| 1 | Extreme | Bitonal images, original DPI, maximum vector optimization; intended for scans |
| 2 | High | RGB, good JPEG quality, low resolution, maximum vector optimization |
| 3 | Medium | RGB, best JPEG quality, medium resolution, medium vector optimization; recommended |
| 4 | Low | Original color model, excellent quality, high resolution, low vector optimization |

The exact numeric quality/DPI selections for modes 2-4 are computed internally after raster-property analysis and are not stored in the public preset plist. Treat fixed numeric mappings as an approximation unless established through black-box output tests.

The phrase “analyze raster image contents and properties” and the 5.1.1 fix for unusual page sizes suggest automatic mode estimates effective image DPI from image pixel dimensions relative to each image's rendered transform/page dimensions, then avoids upsampling and selects an appropriate cap.

## Application architecture

Key Objective-C classes recovered from runtime metadata:

```text
RRTApplication
CompressionMethodsViewController
FilesListCollectionViewController
FilesListTableViewController
CompressOptionControlsViewController
DestinationOptionControlsViewController
RRTPDFComparatorWindowController
RRTPDFComparatorViewController
RRTComparatorPDFView
RRTProgressViewController
RRTPasswordConfirmationViewController
PDFOptimIntentHandler
PDFOptimAutoIntentHandler
RRTInAppPurchaseController
RRTPreferencesWindowController
```

Notable compressor API surface:

```objective-c
+ SharedCompressionOperationQueue
+ CompareOptionsWithOptimizedFileOption:
+ startCompressionForPDF:withProgressController:
- initWithFileDelegate:
- compressPDF:
```

This supports the following component flow:

```text
file/folder ingestion
    -> file list model/controller
    -> shared compression operation queue
    -> per-file progress and variant collection
    -> PDFKit comparator (source vs selected variant)
    -> destination/export coordinator
```

Compression starts immediately after adding a file. Changing options restarts compression for the selected PDF. The shared queue enables parallel compression for Premium users and cancellation when files are removed.

## UI/workflow inventory

- Batch file collection with list/grid presentation
- Add files, folders, nested folders, drag/drop, Dock drop, and Open With
- Sorting by name, date, original size, page count, compressed size, and compression percentage
- Sidebar for auto/manual compression and destination controls
- Determinate per-file progress
- Retention of multiple output variants for a file
- Side-by-side synchronized PDF views with page and zoom controls
- Export selected/all and drag compressed output to Finder/other apps
- Replace original, ask for a folder, or persist a default folder bookmark
- Preserve directory hierarchy, creation time, and modification time
- Collision policy: overwrite or numeric suffix
- Optional recoverable “replace original” behavior by moving the source to Trash first

## Shortcuts contract

Two custom intents are bundled:

1. `PDFOptimAutoIntent`: accepts multiple PDFs and one of the four automatic compression modes.
2. `PDFOptimIntent`: accepts multiple PDFs plus filter, quality, DPI, color model, vector level, and the four cleanup flags.

Both return multiple compressed PDF files and support background execution. The action itself is Premium-gated by the app.

## Clean-room reimplementation plan

A functionally equivalent macOS app can be built without copying the original UI or proprietary code:

1. **Core library:** C++ or Swift wrapper over QPDF for safe object-tree rewrite; use PDFium/Poppler or ImageIO for decoding image XObjects.
2. **Raster transformer:** normalize supported PDF image filters, calculate effective DPI from the current transformation matrix, resize with Accelerate/Core Image, color-convert with ColorSync/Core Graphics, and encode JPEG/CCITT Group 4 as appropriate.
3. **Vector optimizer:** tokenize content streams and quantize numeric operands while never changing operator order. Begin conservatively; graphics-state and text matrices need special care.
4. **Auto analyzer:** inventory raster count, pixel area, effective DPI, colorspaces, scan-likeness, and vector-stream byte share; map the result to safe caps by quality tier.
5. **Writer:** preserve outlines, links, forms, metadata, page boxes, rotations, annotations, and encryption unless the user requests otherwise. Recompress eligible streams and remove unreachable objects.
6. **macOS shell:** SwiftUI or AppKit, PDFKit comparator, actor/operation-queue based jobs, security-scoped bookmarks, and App Intents.
7. **Verification:** structural validation with `qpdf --check`, pixel-difference rendering, selectable-text/link/form regression checks, and corpus tests for malformed/encrypted/JPX/transparency-heavy PDFs.

The riskiest feature is vector simplification. Image recompression is relatively straightforward; safely rewriting arbitrary content streams without changing appearance requires a broad regression corpus.

## Confidence and remaining unknowns

High confidence:

- framework/library composition;
- two-engine architecture;
- option schema and preset values;
- high-level image replacement pipeline;
- PDF keys removed by advanced options;
- batch/comparator/export architecture.

Medium confidence:

- effective-DPI analysis in automatic mode;
- vector levels being numeric precision/tolerance controls;
- exact division of labor between Poppler-derived and QPDF code.

Unknown without controlled black-box tests or deeper disassembly:

- exact automatic quality and resolution formulas;
- resampling kernel and sharpening behavior;
- bitonal thresholding/dithering algorithm;
- vector quantization formula per level;
- whether every image is converted to JPEG or certain masks/1-bit images retain lossless/CCITT encoding;
- exact QPDF/Poppler fork versions.

## Evidence sources

- Installed application bundle metadata, resource property lists, intent definition, NIB/storyboard inventory, Mach-O load commands, Objective-C runtime metadata, and printable binary strings.
- Bundled `PDFOptim User Manual.pdf`, version 6.0.
- [PDFOptim App Store listing](https://apps.apple.com/us/app/pdfoptim-the-pdf-compressor/id621276561?mt=12), including product description and release history.

