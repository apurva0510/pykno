# Contributing

Thanks for helping improve Pykno.

## Development setup

Pykno currently targets macOS for its complete feature set. The object engine is Python-based; Quartz integration requires the Xcode Command Line Tools.

```Shell
./setup-pykno
.venv/bin/python -m unittest discover -s tests -v
```

## Pull requests

- Keep object preservation and local processing as default design constraints.
- Add regression coverage for behavior changes.
- Do not add network calls to the compression path.
- Never commit confidential PDFs or documents without redistribution rights.
- Use synthetic fixtures where possible and describe the structural edge case being tested.
- Treat Quartz as an explicitly destructive fallback, not the default engine.

## Reporting bugs

Include the command, macOS and Python versions, engine, and sanitized output. Do not attach a sensitive source PDF to a public issue. Create a minimal synthetic reproduction or use the private security-reporting path described in `SECURITY.md`.
