# Changelog

All notable changes to Codenetra will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-01

### Added

- Initial release.
- 38 built-in compliance rules across 5 tiers: Baseline, Security, Universal
  best practice, High-leverage, Modern / AI-era.
- 11 check primitives (file existence, size, project-field equality,
  protected-branch / approval-rule / push-rule introspection, etc.).
- Custom rule loading from YAML or Markdown — override/extend the built-ins
  via `--rules` (CLI) or upload (web UI).
- CLI scanner with concurrent per-repo workers, Rich progress bar with ETA,
  and a Rich-based terminal report (hero compliance panel, per-tier progress
  bars, critical-issues callout, biggest-lever recommendation).
- Async web UI on `127.0.0.1:8765` with FastAPI: form-based scan submission,
  live progress page polling a JSON status endpoint, persisted reports under
  `~/.codenetra/reports/`.
- Markdown report with executive scorecard, RAG status, weakest-rule and
  weakest-repo insights, biggest-lever recommendation, collapsible per-tier
  detail table, and per-repo non-compliant breakdown.
- Resilience: `python-gitlab` retry-on-transient-error, sanitized error
  messages with hints, graceful degradation when individual endpoints (push
  rules, statistics) are forbidden.

[Unreleased]: https://github.com/rnampall/codenetra/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rnampall/codenetra/releases/tag/v0.1.0
