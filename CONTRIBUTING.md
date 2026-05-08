# Contributing to Codenetra

Thanks for your interest in contributing! Codenetra is a small, focused tool —
keeping it simple is a feature, so the bar for new behaviour is "does this
help most users scan their repos better?"

## Ways to contribute

- **File issues** — bug reports, feature ideas, doc fixes are all welcome.
- **Improve docs** — typos, clarifications, or new examples in the README.
- **Add rules** — propose new compliance rules. Open an issue first so we
  can discuss tier and naming before you write code.
- **Submit fixes** — small fixes (typos, edge-case handling) can go straight
  to a PR.

## Development setup

```bash
git clone https://github.com/rnampall/codenetra.git
cd codenetra
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
```

Run a smoke scan against any GitLab group you have access to (set
`GITLAB_URL` and `GITLAB_TOKEN` in `.env` first):

```bash
codenetra scan --group your-group
codenetra serve   # http://127.0.0.1:8765
```

## Project layout

```
codenetra/
├── codenetra/
│   ├── client.py            # GitLab SDK wrapper, auth, error sanitization
│   ├── rules.py             # Check primitives, RepoContext, Rule dataclass
│   ├── rules_loader.py      # Load YAML / Markdown rule files
│   ├── rules_data/
│   │   └── builtin_rules.yaml   # The 38 default rules
│   ├── scanner.py           # ScanReport, scan orchestration, scorecard math
│   ├── report.py            # Markdown rendering of ScanReport
│   ├── cli.py               # Click CLI + Rich terminal renderer
│   ├── web.py               # FastAPI app + async job store
│   └── templates/           # Jinja2 templates (markdown + web)
├── pyproject.toml
└── tests/
```

## Adding a new rule

1. Open `codenetra/rules_data/builtin_rules.yaml`.
2. Add an entry under the appropriate tier with `key`, `title`, `tier`, `tags`,
   `check`, `fix_hint`, and any check-specific parameters.
3. If the rule needs a new check primitive, add it to `codenetra/rules.py`
   and register it in `CHECK_PRIMITIVES`.
4. Run the verifier scripts to confirm everything still loads.
5. Write a short note in `CHANGELOG.md` under the Unreleased section.

A rule should be:

- **Detectable from metadata only** — Codenetra never reads source code.
- **Universally applicable** — niche rules belong in users' custom rule
  files, not the built-ins.
- **Actionable** — the `fix_hint` should tell the user exactly what to do.

## Code style

- Python 3.9+; the codebase uses `from __future__ import annotations` so
  modern type syntax works everywhere.
- We follow the standard library style — docstrings on public functions,
  inline comments explaining non-obvious decisions, no clever one-liners.
- No new runtime dependencies without discussion in an issue first.

## Submitting a pull request

1. Fork the repo, create a feature branch off `main`.
2. Make your changes.
3. Update `CHANGELOG.md` under the Unreleased section.
4. Open a PR against `main` describing what you changed and why.
5. Be patient — this is a side project; reviews can take a few days.

## Code of Conduct

This project follows the [Contributor Covenant](./CODE_OF_CONDUCT.md). By
participating you agree to abide by its terms.
