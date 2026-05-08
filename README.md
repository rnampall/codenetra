# Codenetra (नेत्र)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

> *Netra* — Sanskrit for "the eye." Codenetra is the eye on every repo.

**GitLab hygiene & compliance scanner.** Point it at a GitLab group, get a
markdown report back in under a minute — with an executive scorecard, RAG
status, weakest-rule analysis, biggest-lever recommendation, and a tiered
breakdown of 38 best-practice checks. Ships as a CLI *and* a local web app.

Reads metadata only. Source code is never read or stored.

## Features

- **38 built-in rules** across 5 tiers (Baseline, Security, Universal best
  practice, High-leverage, Modern / AI-era) — distilled from public
  practices at top engineering orgs
- **Custom rules** — override or extend the built-ins via YAML or Markdown
  uploaded in the web UI or passed with `--rules`
- **Executive scorecard** — single pass-rate %, RAG status, per-tier
  progress bars, top-3 weakest rules + repos, biggest-lever recommendation
- **Async web UI** with live progress page, persisted scan history
- **Concurrent scanner** — handles multi-hundred-repo orgs with auto-retries
  on transient GitLab errors
- **Markdown report** — readable in any markdown viewer, with collapsible
  per-tier and per-repo sections
- **Resilient** — degrades gracefully when individual GitLab endpoints are
  forbidden by your token's scope

## What it checks

Out of the box, every repo is checked against **38 hygiene rules** spanning
governance, documentation, supply chain, code quality, security, and modern
AI-era tooling. Empty and archived repos are skipped.

The full rule set is defined declaratively in
[`codenetra/rules_data/builtin_rules.yaml`](./codenetra/rules_data/builtin_rules.yaml) —
take a look there for the complete list with fix hints.

The rules group into:

- **Governance & security** — branch protection, required reviewers,
  SECURITY.md, CODEOWNERS, signed-commit policy
- **Documentation** — README, description, CONTRIBUTING, CHANGELOG, code
  of conduct, ADR folder, API spec
- **Supply chain & quality** — lockfiles, dependency-update automation,
  linter / formatter / EditorConfig / pre-commit configs, .gitignore
- **Process** — CI workflow, status checks before merge, MR + issue
  templates, default branch is `main`
- **Hygiene** — recent activity, repo size within budget
- **Modern / AI-era** — CLAUDE.md / AGENTS.md / .cursorrules,
  devcontainer config, one-command setup (Make / Just / Task)

## Customising the rules

You can override or extend the built-in 32 rules by passing your own
file — either YAML directly, or Markdown with a fenced ```yaml block.

### CLI

```bash
codenetra scan --group acme-corp --rules my-rules.yaml
codenetra scan --group acme-corp --rules my-rules.md
```

### Web UI

The scan form has a collapsible "Custom rules" panel — upload a file or
paste YAML directly. The custom rules apply to that scan only.

### File format

```yaml
version: 1

config:
  readme_min_bytes: 1000        # override built-in defaults
  activity_window_days: 60

rules:
  # Disable a built-in rule entirely
  - key: has_test_directory
    enabled: false

  # Override a built-in (any subset of fields)
  - key: has_substantial_readme
    min_bytes: 1000
    fix_hint: "Our org requires READMEs ≥ 1 KB."

  # Add a brand-new rule
  - key: has_runbook
    title: Has on-call runbook
    tags: [ops]
    check: file_exists
    paths: [docs/runbook.md, RUNBOOK.md]
    fix_hint: Add a runbook describing on-call procedures.
```

Available `check:` primitives: `file_exists`, `file_size_min`,
`directory_has_files`, `project_field_truthy`, `project_field_equals`,
`project_field_not_in`, `activity_within`, `protected_default_branch`,
`required_reviewers`, `repo_size_max`, `signed_commits`.

## Install

Requires Python 3.9 or newer.

```bash
git clone https://github.com/rnampall/codenetra.git
cd codenetra
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

(Once published to PyPI: `pip install codenetra`.)

## Configure

```bash
cp .env.example .env
# edit .env and set GITLAB_URL + GITLAB_TOKEN
```

**Token recommendation.** Use a **Group Access Token** when possible — it's scoped to a single group, doesn't expire when a person leaves, and only needs Reporter role with `read_api` + `read_repository`. Fall back to a Personal Access Token if you don't have Maintainer rights on the group. Both work identically with this tool.

## Run

```bash
# Scan an entire group (and its subgroups)
codenetra scan --group acme-corp
# → writes report.md, prints a summary

codenetra scan --group acme-corp/platform --output platform-report.md

# Scan a single project
codenetra scan --project acme-corp/platform/api-gateway --output api-gateway-report.md

# Limit a group scan to top-level only (skip subgroups)
codenetra scan --group acme-corp --no-subgroups

# Faster: more concurrent workers (default 8)
codenetra scan --group acme-corp --workers 16
```

`--group` and `--project` are mutually exclusive — pass exactly one.

## Local web UI

A lightweight FastAPI-based UI is included for users who'd rather not use the CLI.

```bash
codenetra serve
# → opens on http://127.0.0.1:8765
#
# Custom port / bind address:
codenetra serve --port 9000
codenetra serve --host 0.0.0.0  # expose to your LAN — careful with tokens
```

The web UI uses the same scan engine as the CLI and persists every scan to
`~/.codenetra/reports/<slug>.md`, so the index page lists recent scans you can
re-open. There's no auth and no database — by default it's bound to localhost only.

Exit code is 0 if all repos are compliant, 1 if any repo failed any rule, 2 on configuration errors. Useful for wiring into a scheduled GitLab pipeline.

## Sample report

See [sample-report.md](./sample-report.md) for what the markdown output looks
like, and [preview/report.html](./preview/report.html) for the rendered HTML
view served by the local web app.

## Roadmap

Things on the wishlist (not yet built):

- Scheduled scans with historical trend lines
- "Post report as a GitLab issue" button (one-click handoff)
- Multi-group comparison mode
- Per-rule severity weighting in the scorecard
- GitHub support (currently GitLab-only)

PRs welcome — see [CONTRIBUTING.md](./CONTRIBUTING.md).

## Contributing

Bug reports, feature requests, and pull requests are very welcome. Please
read [CONTRIBUTING.md](./CONTRIBUTING.md) and the [Code of Conduct](./CODE_OF_CONDUCT.md)
before opening an issue or PR. For security vulnerabilities, please follow
[SECURITY.md](./SECURITY.md) instead of filing a public issue.

## Acknowledgements

Codenetra was inspired by [Codatus](https://codatus.com), a GitHub compliance
scanner with a similar one-shot, metadata-only philosophy. The rule set borrows
from publicly-documented practices at engineering teams who write about their
OSS standards (Spotify, Zalando, AWS, GitHub, and many others).

## License

[MIT](./LICENSE).
