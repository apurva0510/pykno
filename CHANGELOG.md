# Changelog

All notable changes to Pykno will be documented here.

The format follows Keep a Changelog, and the project intends to use Semantic Versioning.

## [Unreleased]

### Added

- Installable Python console command through `pyproject.toml`.
- Configurable Finder Quick Action installer and uninstaller.
- Explicit non-macOS behavior for Quartz and automatic mode.

## [0.1.0] - 2026-07-24

### Added

- Object-preserving PDF image compression with pikepdf/QPDF and Pillow.
- Effective-DPI analysis using content-stream transformation matrices.
- Support for shared images and images nested in Form XObjects.
- Small, Balanced, and Print presets plus custom DPI and JPEG quality.
- Optional Quartz redraw engine and guarded automatic fallback.
- Output validation, atomic installation, metadata preservation, and batch collision checks.
