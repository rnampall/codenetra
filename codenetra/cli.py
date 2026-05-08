"""`codenetra` Click CLI.

Usage:
    codenetra scan --group acme-corp [--output report.md] [--workers 8] [--no-subgroups]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule as RichRule
from rich.text import Text
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from codenetra.client import (
    GitLabConfig,
    clean_error_message,
    connect,
    fetch_group,
    fetch_project,
    list_group_projects,
)
from codenetra.report import render_markdown
from codenetra.rules_loader import (
    LoadedRules,
    load_builtin_rules,
    load_rules_from_path,
)
from codenetra.scanner import ScanReport, scan_projects


console = Console()


# Map a RAG dict to a Rich color name.
_RAG_COLORS = {"green": "green", "yellow": "yellow", "red": "red"}


def _rate_color(pct: float) -> str:
    """Same band thresholds as the scorecard RAG."""
    if pct >= 85:
        return "green"
    if pct >= 60:
        return "yellow"
    return "red"


def _make_bar(pct: float, width: int = 22) -> str:
    """Static Unicode progress bar — full block for filled, light shade for empty."""
    filled = max(0, min(width, int(round(pct / 100 * width))))
    return "█" * filled + "░" * (width - filled)


def _render_hero(report: ScanReport) -> Panel:
    """Big, attention-grabbing compliance headline.

    Shows:
      <BIG %>          ← pass rate, large and coloured
      🟢/🟡/🔴 STATUS  ← RAG label
      compact one-liner with repo count, rule count, compliant count, skipped
    """
    rag = report.rag_status()
    rag_color = _RAG_COLORS.get(rag["key"], "white")

    big = Text()
    big.append(f"\n  {report.overall_pass_rate:.0f}%  ",
               style=f"bold {_rate_color(report.overall_pass_rate)}")
    big.append("PASS RATE\n", style="dim")
    big.append(f"  {rag['emoji']} {rag['label'].upper()}  ",
               style=f"bold {rag_color}")
    big.append("\n\n")
    big.append(
        f"  {report.scanned_count} repo{'s' if report.scanned_count != 1 else ''} scanned"
        f" · {len(report.rules)} rules"
        f" · {report.compliant_count}/{report.scanned_count} fully compliant"
        f" · {report.skipped_count} skipped",
        style="dim",
    )
    big.append("\n")

    return Panel(
        Align.center(big),
        title=f"[bold]{report.scope_label}: {report.group_path}[/]",
        title_align="left",
        subtitle=f"[dim]Scanned {report.scanned_at.strftime('%Y-%m-%d %H:%M UTC')} ·"
                 f" rules: {report.rules_source_label}[/]",
        subtitle_align="right",
        border_style="cyan",
        box=box.HEAVY,
        padding=(0, 2),
    )


def _render_tier_bars(report: ScanReport) -> Panel:
    """Per-tier coloured progress bars — gives an at-a-glance heatmap."""
    rows: list = []
    # Find the longest tier name so columns line up.
    pad = max((len(ts["tier"]) for ts in report.tier_summaries()), default=10)
    for ts in report.tier_summaries():
        bar_color = _rate_color(ts["pass_rate"])
        bar = _make_bar(ts["pass_rate"])
        rag = ts["rag"]
        rag_color = _RAG_COLORS.get(rag["key"], "white")
        rows.append(
            f"  [bold]{ts['tier']:<{pad}}[/]  "
            f"[{bar_color}]{bar}[/]  "
            f"[bold {bar_color}]{ts['pass_rate']:>3.0f}%[/]  "
            f"[dim]{ts['passing_checks']:>3}/{ts['total_checks']:<3}[/]  "
            f"[{rag_color}]{rag['emoji']} {rag['label']}[/]"
        )
    body = "\n".join(rows)
    return Panel(body, title="[bold]By tier[/]", border_style="cyan", padding=(0, 1))


def _render_critical_issues(report: ScanReport) -> Panel:
    """The headline 'what to fix first' panel — security, top-3 fixes, worst repos."""
    rules_by_key = {r.key: r for r in report.rules}
    sec_keys = report.security_rule_keys
    sections: list = []

    # ---- Security signal ----
    sec_pct = report.security_pass_rate
    sec_rag = report.security_rag()
    sec_color = _RAG_COLORS.get(sec_rag["key"], "white")
    sec_lines = [
        f"[bold]🔒 Security signal:[/] "
        f"[bold {_rate_color(sec_pct)}]{sec_pct:.0f}%[/]"
        f" — [{sec_color}]{sec_rag['emoji']} {sec_rag['label']}[/]"
    ]
    # If security is below green, list the worst-performing security rules
    # (limited to 3 so we don't drown the user in detail).
    if sec_pct < 85 and sec_keys:
        sec_rows = [
            row for row in report.summary_rows()
            if row["key"] in sec_keys and row["failing"] > 0
        ][:3]
        for row in sec_rows:
            total = row["passing"] + row["failing"]
            sec_lines.append(
                f"     [dim]#{row['sl_no']}[/] [bold]{row['title']}[/] — "
                f"[red]{row['failing']}/{total}[/] repos failing"
            )
    sections.append("\n".join(sec_lines))

    # ---- Top 3 weakest rules (any tier) ----
    weakest = report.weakest_rules(3)
    if weakest:
        lines = ["[bold]⚡ Top 3 fixes (by impact):[/]"]
        for r in weakest:
            total = r["passing"] + r["failing"]
            lines.append(
                f"     [dim]#{r['sl_no']:>2}[/] [bold]{r['title']}[/] — "
                f"[red]{r['failing']}/{total}[/] repos failing  "
                f"[dim]([{_rate_color(r['pass_rate'])}]{r['pass_rate']:.0f}%[/] pass)[/]"
            )
        sections.append("\n".join(lines))

    # ---- Top 3 weakest repos ----
    weak_repos = report.weakest_repos(3)
    if weak_repos:
        lines = ["[bold]🏚️  Most-broken repos:[/]"]
        for r in weak_repos:
            lines.append(
                f"     • [bold]{r['path']}[/] — "
                f"[red]{r['failing_count']}[/] of {r['total_rules']} rules failing"
            )
        sections.append("\n".join(lines))

    body = "\n\n".join(sections)
    return Panel(body, title="[bold yellow]⚠️  Critical issues[/]",
                 border_style="yellow", padding=(0, 1))


def _render_lever_callout(report: ScanReport) -> Panel | None:
    lever = report.biggest_lever()
    if not lever:
        return None
    body = (
        f"Fixing [bold]{lever['rule_title']}[/] in the "
        f"[bold]{lever['affected_repos']}[/] repos missing it would raise "
        f"compliance from [bold]{lever['current_compliance_pct']:.0f}%[/] "
        f"→ [bold green]{lever['projected_compliance_pct']:.0f}%[/] "
        f"([bold green]+{lever['delta_pct']:.0f} pts[/])."
    )
    return Panel(body, title="[bold]💡 Biggest lever[/]",
                 border_style="green", padding=(0, 1))


def _render_footer(report: ScanReport, output_path: Path) -> Text:
    """Compact final line pointing at the saved markdown report."""
    text = Text()
    text.append("📄 Full report saved: ", style="dim")
    text.append(str(output_path), style="cyan")
    text.append("\n")
    text.append("   ", style="dim")
    text.append(
        "Open it for the rule-by-rule table and per-repo details, "
        "or rerun with --full / --detail to print here.",
        style="dim",
    )
    return text


def _render_insights(report: ScanReport) -> Group:
    blocks: list = []

    weakest = report.weakest_rules(3)
    if weakest:
        lines = ["[bold]Top 3 weakest rules[/]"]
        for r in weakest:
            total = r["passing"] + r["failing"]
            lines.append(
                f"  • [dim]#{r['sl_no']}[/] [bold]{r['title']}[/] — "
                f"{r['failing']}/{total} repos failing "
                f"([{_rate_color(r['pass_rate'])}]{r['pass_rate']:.0f}% pass[/])"
            )
        blocks.append("\n".join(lines))

    weak_repos = report.weakest_repos(3)
    if weak_repos:
        lines = ["[bold]Top 3 weakest repos[/]"]
        for r in weak_repos:
            lines.append(
                f"  • [bold]{r['path']}[/] — "
                f"{r['failing_count']}/{r['total_rules']} rules failing"
            )
        blocks.append("\n".join(lines))

    sec_rag = report.security_rag()
    sec_color = _RAG_COLORS.get(sec_rag["key"], "white")
    blocks.append(
        f"[bold]🔒 Security signal:[/] "
        f"[{_rate_color(report.security_pass_rate)}]"
        f"{report.security_pass_rate:.0f}%[/] — "
        f"[{sec_color}]{sec_rag['emoji']} {sec_rag['label']}[/]"
    )

    lever = report.biggest_lever()
    if lever:
        blocks.append(Panel(
            f"💡 [bold]Biggest lever:[/] fixing [bold]{lever['rule_title']}[/] "
            f"in the {lever['affected_repos']} repos missing it would raise "
            f"compliance from {lever['current_compliance_pct']:.0f}% to "
            f"{lever['projected_compliance_pct']:.0f}% "
            f"(+{lever['delta_pct']:.0f} pts).",
            border_style="yellow", padding=(0, 1),
        ))

    return Group(*blocks)


def _render_detail_table(report: ScanReport) -> Table:
    table = Table(
        title=f"[bold]Detailed breakdown — "
              f"{len(report.rules)} rules across {len(report.tier_summaries())} tiers[/]",
        title_style="cyan",
        show_header=True, header_style="bold cyan",
        box=box.SIMPLE_HEAD, expand=False, pad_edge=False,
    )
    table.add_column("Sl. No.", justify="right", style="dim")
    table.add_column("Tier")
    table.add_column("Rule")
    table.add_column("Pass", justify="right", style="green")
    table.add_column("Fail", justify="right", style="red")
    table.add_column("Rate", justify="right")
    for row in report.summary_rows():
        table.add_row(
            str(row["sl_no"]),
            row["tier"],
            row["title"],
            str(row["passing"]),
            str(row["failing"]),
            f"[{_rate_color(row['pass_rate'])}]{row['pass_rate']:.0f}%[/]",
        )
    return table


def _render_per_repo_failures(report: ScanReport) -> Group:
    """Optional --detail section: every non-compliant repo with its failing rules."""
    rules_by_key = {r.key: r for r in report.rules}
    non_compliant = [
        o for o in report.outcomes
        if not o.is_skipped and not o.is_compliant
    ]
    non_compliant.sort(key=lambda o: (-len(o.failing_rules), o.path))

    blocks: list = [
        f"[bold red]❌ Non-compliant ({len(non_compliant)} repos)[/]",
    ]
    for o in non_compliant:
        lines = [f"\n[bold]{o.path}[/] — {len(o.failing_rules)} failing"]
        for key in o.failing_rules:
            rule = rules_by_key.get(key)
            if not rule:
                continue
            detail = o.results[key].detail
            lines.append(
                f"  • [dim]#{rule.sl_no}[/] {rule.title}"
                + (f" — [dim italic]{detail}[/]" if detail else "")
            )
        blocks.append("\n".join(lines))

    if report.skipped_count:
        skipped = [o for o in report.outcomes if o.is_skipped]
        blocks.append(f"\n[bold yellow]⚠️  Skipped ({report.skipped_count} repos)[/]")
        for o in skipped:
            blocks.append(f"  • [dim]{o.path}[/] — {o.skipped_reason}")

    return Group(*blocks)


def render_terminal_report(
    report: ScanReport,
    output_path: Path,
    *,
    quiet: bool = False,
    full: bool = False,
    detail: bool = False,
) -> None:
    """Print the scan results to the terminal.

    Default flow (high-level + critical only):
      hero compliance panel  →  per-tier progress bars  →  critical issues
      →  biggest lever  →  footer line pointing at report.md

    Flags:
      --quiet/-q  → just the final one-line summary panel
      --full      → also print the 38-row detailed breakdown table
      --detail    → also print per-repo non-compliant breakdown
    """
    if quiet:
        _render_summary_panel(report, output_path)
        return

    # Spacer so the panel doesn't butt up against the progress bar's last frame.
    console.print()

    # Single-project scans skip the executive sections (one repo can't be
    # "ranked" against itself); they only need the detail table.
    is_group = report.scope_label == "Group" and report.scanned_count > 0

    if is_group:
        # 1. Hero — big % + RAG status
        console.print(_render_hero(report))
        console.print()

        # 2. Per-tier progress bars
        console.print(_render_tier_bars(report))
        console.print()

        # 3. Critical issues — security signal + top 3 fixes + worst repos
        console.print(_render_critical_issues(report))
        console.print()

        # 4. Biggest lever (callout)
        lever = _render_lever_callout(report)
        if lever is not None:
            console.print(lever)
            console.print()

    # Optional dense sections
    if full:
        console.print(_render_detail_table(report))
        console.print()
    if detail:
        console.print(_render_per_repo_failures(report))
        console.print()

    # 5. Footer
    console.print(_render_footer(report, output_path))


def _render_summary_panel(report: ScanReport, output_path: Path) -> None:
    pct = (
        round(report.compliant_count / report.scanned_count * 100)
        if report.scanned_count else 0
    )
    summary_text = (
        f"[bold]{report.compliant_count}/{report.scanned_count}[/] compliant "
        f"({pct}%)\n"
        f"[dim]{report.skipped_count} skipped · "
        f"report saved to [cyan]{output_path}[/]"
    )
    title = (
        f"Scan complete — {report.group_path}"
        if report.group_path else "Scan complete"
    )
    console.print(Panel(summary_text, title=title, expand=False))


@click.group()
@click.version_option()
def main() -> None:
    """Codenetra — the eye on every repo. GitLab hygiene & compliance reports."""


@main.command()
@click.option(
    "--group", "group_path", default=None,
    help="GitLab group path, e.g. 'acme-corp' or 'acme-corp/platform'. "
         "Mutually exclusive with --project.",
)
@click.option(
    "--project", "project_path", default=None,
    help="Full project path, e.g. 'acme-corp/platform/api-gateway'. "
         "Scans a single repo. Mutually exclusive with --group.",
)
@click.option(
    "--output", "-o", default="report.md", show_default=True,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Where to write the markdown report.",
)
@click.option(
    "--workers", default=8, show_default=True, type=click.IntRange(1, 32),
    help="Concurrent per-repo workers.",
)
@click.option(
    "--no-subgroups", is_flag=True,
    help="When using --group, scan only direct projects of the group "
         "(skip subgroups). No effect with --project.",
)
@click.option(
    "--gitlab-url", envvar="GITLAB_URL", default=None,
    help="Override the GitLab URL (defaults to $GITLAB_URL or https://gitlab.com).",
)
@click.option(
    "--token", envvar="GITLAB_TOKEN", default=None,
    help="GitLab token (defaults to $GITLAB_TOKEN). Group access token recommended.",
)
@click.option(
    "--rules", "rules_path", default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Custom rules file (.yaml, .yml, or .md). Overrides / extends the built-in rule set.",
)
@click.option(
    "--quiet", "-q", is_flag=True,
    help="Print only the final one-line summary panel (good for CI logs).",
)
@click.option(
    "--full", is_flag=True,
    help="Also print the rule-by-rule detail table to the terminal.",
)
@click.option(
    "--detail", is_flag=True,
    help="Also print the per-repo non-compliant breakdown to the terminal.",
)
def scan(
    group_path: str | None,
    project_path: str | None,
    output: Path,
    workers: int,
    no_subgroups: bool,
    gitlab_url: str | None,
    token: str | None,
    rules_path: Path | None,
    quiet: bool,
    full: bool,
    detail: bool,
) -> None:
    """Scan a GitLab group OR a single project and write a compliance report.

    Exactly one of --group / --project must be provided.
    """
    if (group_path is None) == (project_path is None):
        console.print(
            "[bold red]Error:[/] provide exactly one of --group or --project."
        )
        sys.exit(2)

    # Load rules — built-in by default, or merge a custom file on top.
    try:
        loaded_rules = (
            load_rules_from_path(rules_path) if rules_path else load_builtin_rules()
        )
    except (ValueError, OSError) as e:
        console.print(f"[bold red]Could not load rules:[/] {e}")
        sys.exit(2)
    console.print(
        f"Rules: [cyan]{len(loaded_rules.rules)}[/] active "
        f"({loaded_rules.source_label})"
    )

    load_dotenv()
    env = {
        "GITLAB_URL": gitlab_url or os.environ.get("GITLAB_URL"),
        "GITLAB_TOKEN": token or os.environ.get("GITLAB_TOKEN"),
    }

    try:
        config = GitLabConfig.from_env(env)
    except RuntimeError as e:
        console.print(f"[bold red]Configuration error:[/] {e}")
        sys.exit(2)

    try:
        gl = connect(config)
    except Exception as e:  # noqa: BLE001
        console.print(f"[bold red]Auth failed:[/] {clean_error_message(e)}")
        sys.exit(2)

    # Resolve the scan target into a list of project summaries.
    summaries: list = []
    target_label: str
    scope_label: str

    if project_path:
        try:
            project = fetch_project(gl, project_path)
        except Exception as e:  # noqa: BLE001
            console.print(f"[bold red]Could not load project {project_path!r}:[/] {clean_error_message(e)}")
            sys.exit(2)
        summaries = [project]
        target_label = project.path_with_namespace
        scope_label = "Project"
        console.print(
            f"Scanning project [bold]{target_label}[/] on {config.url}…"
        )
    else:
        assert group_path is not None
        try:
            group = fetch_group(gl, group_path)
        except Exception as e:  # noqa: BLE001
            console.print(f"[bold red]Could not load group {group_path!r}:[/] {clean_error_message(e)}")
            sys.exit(2)
        target_label = group.full_path
        scope_label = "Group"
        console.print(
            f"Scanning group [bold]{target_label}[/] on {config.url} "
            f"(subgroups {'OFF' if no_subgroups else 'ON'}, {workers} workers)…"
        )
        summaries = list(
            list_group_projects(group, include_subgroups=not no_subgroups)
        )

    if not summaries:
        console.print("[yellow]No projects found.[/]")
        sys.exit(0)

    # Rich progress bar with elapsed, ETA, count, percentage, and a live
    # description that updates with the most recently finished repo path.
    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[bold]Scanning[/]"),
        BarColumn(bar_width=None, complete_style="green", finished_style="green"),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        TextColumn("•"),
        TextColumn("[dim]{task.fields[last]}[/]"),
        console=console,
        transient=False,
    ) as progress:
        task_id = progress.add_task(
            "scanning", total=len(summaries), last="…",
        )

        def _on_progress(path: str) -> None:
            # Truncate so very deep paths don't push the bar offscreen.
            short = path if len(path) <= 48 else "…" + path[-47:]
            progress.update(task_id, advance=1, last=f"✓ {short}")

        report = scan_projects(
            gl,
            summaries,
            target_path=target_label,
            loaded_rules=loaded_rules,
            workers=workers,
            on_progress=_on_progress,
            scope_label=scope_label,
        )

    markdown = render_markdown(report)
    output.write_text(markdown, encoding="utf-8")

    # Default: hero compliance panel + tier progress bars + critical issues
    # + biggest lever + footer. `--quiet` collapses to one line; `--full`
    # adds the rule-by-rule table; `--detail` adds the per-repo failures.
    render_terminal_report(
        report, output, quiet=quiet, full=full, detail=detail,
    )

    # Exit nonzero if anything failed, so this can drop into a scheduled
    # GitLab pipeline as a quality gate.
    any_failures = any(o.failing_rules for o in report.outcomes if not o.is_skipped)
    sys.exit(1 if any_failures else 0)


@main.command()
@click.option(
    "--host", default="127.0.0.1", show_default=True,
    help="Bind host. Use 0.0.0.0 to expose to your LAN.",
)
@click.option(
    "--port", default=8765, show_default=True, type=click.IntRange(1, 65535),
    help="Port to listen on.",
)
@click.option(
    "--reload", is_flag=True,
    help="Auto-reload on code changes (dev only — requires uvicorn[standard]).",
)
def serve(host: str, port: int, reload: bool) -> None:
    """Start the local web UI."""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        console.print(
            "[bold red]uvicorn not installed.[/] "
            "Reinstall with the web extras:\n"
            "  pip install -e ."
        )
        sys.exit(2)

    try:
        from codenetra.web import get_app
        get_app()  # validate imports + create reports dir before uvicorn starts
    except ImportError as e:
        console.print(
            f"[bold red]Web app dependencies missing:[/] {e}\n"
            "Reinstall with: pip install -e ."
        )
        sys.exit(2)

    console.print(
        f"[bold green]codenetra[/] serving on [cyan]http://{host}:{port}[/]\n"
        f"[dim]Bound to {host}. Press Ctrl+C to stop.[/]"
    )

    import uvicorn
    if reload:
        # uvicorn's reload mode requires a string import path, not an app instance.
        uvicorn.run(
            "codenetra.web:get_app",
            host=host, port=port, reload=True, factory=True, log_level="info",
        )
    else:
        uvicorn.run(get_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
