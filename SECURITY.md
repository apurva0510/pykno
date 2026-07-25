# Security Policy

## Supported versions

Security fixes are currently made against the latest release and the `main` branch while Pykno remains pre-1.0.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting feature for the repository. Do not open a public issue containing an exploit, password, private document, or sensitive PDF metadata.

Include:

- the affected Pykno version or commit;
- the engine and command used;
- the expected and observed behavior;
- a minimal, non-sensitive reproducer when possible;
- whether the issue can overwrite, disclose, or corrupt data.

Pykno does not need network access to process PDFs. Treat any unexpected network activity during compression as a security issue.
