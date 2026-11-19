# Security policy

shipr handles server credentials and writes to production servers, so we take reports seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Report it privately through the repository's
*Security → Report a vulnerability* page (GitHub private vulnerability reporting).

Include the shipr version (`shipr --version`), your OS and Python version, the steps to reproduce,
and the impact. We aim to acknowledge reports within 7 days and to publish a fix or mitigation
before any public disclosure.

## Supported versions

Security fixes go to the latest released minor version.

## Scope

In scope: credential leaks, host key verification bypass, writes or deletes outside `remote_path`,
uploading files the built-in excludes should block, and lock or atomicity failures that could
corrupt a deploy.

Out of scope: commands that you write yourself in `shipr.yml`. That file is trusted code; see
[docs/security.md](docs/security.md).
