# Security Policy

Codenetra reads metadata only — your source code is never read or stored.

## Reporting a vulnerability

If you discover a security vulnerability in Codenetra, **please do not open a
public GitHub issue.** Instead, report it privately by either:

- Opening a [private security advisory](https://github.com/rnampall/codenetra/security/advisories/new)
  on GitHub, or
- Emailing the maintainer directly (see the email listed in the project's
  GitHub profile).

When reporting, please include:

- A clear description of the vulnerability
- Steps to reproduce
- The version of Codenetra you tested against
- Any relevant logs or proof-of-concept code

We aim to acknowledge security reports within **3 business days** and to ship
a fix or mitigation within **14 days** for confirmed vulnerabilities.

## Scope

In-scope concerns:

- Token leakage (e.g. logging GitLab PATs, exposing them in error messages)
- Path traversal in the saved-report endpoint
- SSRF or unintended HTTP requests from the scanner
- Code execution from a malicious `rules.yaml` / `rules.md` upload

Out-of-scope:

- Bugs in `python-gitlab` or other third-party dependencies — please report
  those upstream
- Misconfiguration of the user's local GitLab instance
- Issues that require Codenetra to be exposed on the public internet
  (it's designed for `localhost` use)

## Supported versions

Codenetra is pre-1.0; we support the latest released version only. If you're
running an older release, please upgrade before reporting.
