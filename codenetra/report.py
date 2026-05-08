"""Render a ScanReport as markdown."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from codenetra import __version__
from codenetra.scanner import ScanReport


_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(disabled_extensions=("j2",)),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_markdown(report: ScanReport) -> str:
    template = _env.get_template("report.md.j2")
    compliant = [o for o in report.outcomes if o.is_compliant]
    non_compliant = [
        o for o in report.outcomes if not o.is_skipped and not o.is_compliant
    ]
    skipped = [o for o in report.outcomes if o.is_skipped]
    non_compliant.sort(key=lambda o: (-len(o.failing_rules), o.path))

    compliant_pct = (
        round(report.compliant_count / report.scanned_count * 100)
        if report.scanned_count
        else 0
    )

    show_scorecard = (
        report.scope_label == "Group" and report.scanned_count > 0
    )

    return template.render(
        report=report,
        summary_rows=report.summary_rows(),
        tier_summaries=report.tier_summaries(),
        rules=report.rules,
        rules_by_key={r.key: r for r in report.rules},
        compliant_outcomes=compliant,
        non_compliant_outcomes=non_compliant,
        skipped_outcomes=skipped,
        compliant_pct=compliant_pct,
        version=__version__,
        # scorecard inputs
        show_scorecard=show_scorecard,
        rag=report.rag_status(),
        security_rag=report.security_rag(),
        weakest_rules=report.weakest_rules(3),
        weakest_repos=report.weakest_repos(3),
        biggest_lever=report.biggest_lever(),
    )
