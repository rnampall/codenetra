"""Orchestrate the scan.

Per-repo flow:
  1. Re-fetch the project with `license=True` so `project.license` is populated.
  2. Pull the root tree (one call) — covers most file_exists rules.
  3. For each subdirectory the active rule set declares as `pre_fetch_subdirs`
     (e.g. `.gitlab/`, `docs/`, `.devcontainer/`) — pull its listing with one
     call, but only if the directory exists at root.
  4. For each `extra_root_paths` (e.g. `.gitignore`, `.editorconfig`) — fetch
     a file size if the file appears in the root tree.
  5. Pull protected branches, approval rules, legacy approvals, push rules,
     repository statistics. Best-effort: any 403/404 returns an empty default
     so the rule degrades gracefully.
  6. Run all active rules against the collected context.

API call budget:
  - Roughly 5 calls per repo for the original 11 rules
  - + 1 per subdirectory listed in pre_fetch_subdirs that actually exists
  - + 1 per README/.gitignore size lookup (only if present)
For the default 32-rule set, that's typically 7–10 calls per repo, still
comfortably under GitLab.com's 600 req/min ceiling.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

import gitlab
from gitlab.exceptions import GitlabError

from codenetra.rules import RepoContext, RuleResult, evaluate_all
from codenetra.rules_loader import LoadedRules


# README filename candidates checked for the size-min check.
_README_NAMES = ("README.md", "README.rst", "README.txt", "README")


@dataclass
class RepoOutcome:
    path: str                       # e.g. "acme-corp/api-gateway"
    web_url: str
    archived: bool
    skipped_reason: Optional[str] = None
    results: dict[str, RuleResult] = field(default_factory=dict)

    @property
    def is_skipped(self) -> bool:
        return self.skipped_reason is not None

    @property
    def failing_rules(self) -> list[str]:
        return [key for key, r in self.results.items() if not r.passed]

    @property
    def is_compliant(self) -> bool:
        return not self.is_skipped and not self.failing_rules


@dataclass
class ScanReport:
    group_path: str                # the path that was scanned (group or single project)
    scanned_at: datetime
    outcomes: list[RepoOutcome]
    rules: tuple = field(default_factory=tuple)   # tuple[Rule, ...]
    rules_source_label: str = "built-in"
    scope_label: str = "Group"     # rendered as "{label}: {group_path}" in the header

    @property
    def scanned_count(self) -> int:
        return sum(1 for o in self.outcomes if not o.is_skipped)

    @property
    def compliant_count(self) -> int:
        return sum(1 for o in self.outcomes if o.is_compliant)

    @property
    def skipped_count(self) -> int:
        return sum(1 for o in self.outcomes if o.is_skipped)

    @property
    def total_count(self) -> int:
        return len(self.outcomes)

    @property
    def rules_count(self) -> int:
        return len(self.rules)

    def summary_rows(self) -> list[dict]:
        """One row per rule, grouped by tier, sorted by pass rate within each tier."""
        scanned = [o for o in self.outcomes if not o.is_skipped]
        rows = []
        for rule in self.rules:
            passing = sum(
                1 for o in scanned
                if o.results.get(rule.key) and o.results[rule.key].passed
            )
            failing = len(scanned) - passing
            pass_rate = (passing / len(scanned) * 100) if scanned else 0.0
            rows.append({
                "key": rule.key,
                "sl_no": rule.sl_no,
                "title": rule.title,
                "tier": rule.tier,
                "tier_order": rule.tier_order,
                "passing": passing,
                "failing": failing,
                "pass_rate": pass_rate,
            })
        # Group by tier (declared order), then by pass_rate ascending within tier.
        rows.sort(key=lambda r: (r["tier_order"], r["pass_rate"], r["title"]))
        return rows

    def tier_summaries(self) -> list[dict]:
        """One entry per tier with aggregated pass / fail counts. Useful for
        the per-tier headline strip above the detail table."""
        scanned = [o for o in self.outcomes if not o.is_skipped]
        if not scanned:
            return []
        # Preserve tier order via tier_order.
        seen: dict[str, dict] = {}
        for rule in self.rules:
            entry = seen.setdefault(rule.tier, {
                "tier": rule.tier,
                "tier_order": rule.tier_order,
                "rule_count": 0,
                "total_checks": 0,
                "passing_checks": 0,
            })
            entry["rule_count"] += 1
            entry["total_checks"] += len(scanned)
            entry["passing_checks"] += sum(
                1 for o in scanned
                if o.results.get(rule.key) and o.results[rule.key].passed
            )
        out = sorted(seen.values(), key=lambda e: e["tier_order"])
        for e in out:
            e["pass_rate"] = (
                e["passing_checks"] / e["total_checks"] * 100
                if e["total_checks"] else 0.0
            )
            e["rag"] = self._rag(e["pass_rate"])
        return out

    # ---------- executive scorecard ----------

    @property
    def total_checks(self) -> int:
        return self.scanned_count * self.rules_count

    @property
    def total_passing_checks(self) -> int:
        return sum(
            1 for o in self.outcomes
            if not o.is_skipped
            for r in o.results.values()
            if r.passed
        )

    @property
    def overall_pass_rate(self) -> float:
        return (
            (self.total_passing_checks / self.total_checks * 100)
            if self.total_checks else 0.0
        )

    @property
    def compliance_rate(self) -> float:
        return (
            (self.compliant_count / self.scanned_count * 100)
            if self.scanned_count else 0.0
        )

    @staticmethod
    def _rag(rate: float) -> dict:
        if rate >= 85:
            return {"key": "green",  "emoji": "🟢", "label": "Healthy"}
        if rate >= 60:
            return {"key": "yellow", "emoji": "🟡", "label": "Needs work"}
        return     {"key": "red",    "emoji": "🔴", "label": "Urgent"}

    def rag_status(self) -> dict:
        return self._rag(self.overall_pass_rate)

    @property
    def security_rule_keys(self) -> tuple[str, ...]:
        """All rules tagged 'security' contribute to the security signal."""
        return tuple(r.key for r in self.rules if "security" in r.tags)

    @property
    def security_pass_rate(self) -> float:
        scanned = [o for o in self.outcomes if not o.is_skipped]
        keys = self.security_rule_keys
        if not scanned or not keys:
            return 0.0
        passing = sum(
            1 for o in scanned for k in keys
            if o.results.get(k) and o.results[k].passed
        )
        total = len(scanned) * len(keys)
        return passing / total * 100 if total else 0.0

    def security_rag(self) -> dict:
        return self._rag(self.security_pass_rate)

    def weakest_rules(self, n: int = 3) -> list[dict]:
        return [row for row in self.summary_rows() if row["failing"] > 0][:n]

    def weakest_repos(self, n: int = 3) -> list[dict]:
        non_compliant = [
            o for o in self.outcomes
            if not o.is_skipped and not o.is_compliant
        ]
        non_compliant.sort(key=lambda o: (-len(o.failing_rules), o.path))
        return [
            {
                "path": o.path,
                "web_url": o.web_url,
                "failing_count": len(o.failing_rules),
                "total_rules": self.rules_count,
            }
            for o in non_compliant[:n]
        ]

    def biggest_lever(self) -> Optional[dict]:
        scanned = [o for o in self.outcomes if not o.is_skipped]
        if not scanned:
            return None
        current_compliant = sum(1 for o in scanned if o.is_compliant)
        baseline_pct = current_compliant / len(scanned) * 100

        best_rule = None
        best_pct = baseline_pct
        for rule in self.rules:
            new_compliant = sum(
                1 for o in scanned
                if not o.failing_rules
                or set(o.failing_rules) <= {rule.key}
            )
            pct = new_compliant / len(scanned) * 100
            if pct > best_pct:
                best_pct = pct
                best_rule = rule

        if best_rule is None:
            return None
        affected = sum(
            1 for o in scanned
            if o.results.get(best_rule.key) and not o.results[best_rule.key].passed
        )
        return {
            "rule_title": best_rule.title,
            "rule_key": best_rule.key,
            "affected_repos": affected,
            "current_compliance_pct": baseline_pct,
            "projected_compliance_pct": best_pct,
            "delta_pct": best_pct - baseline_pct,
        }


# ---------- per-repo data collection ----------

def _safe_get_root_tree(project) -> list[dict]:
    try:
        return project.repository_tree(
            ref=project.default_branch, all=True, recursive=False
        )
    except (GitlabError, AttributeError):
        return []


def _safe_get_subdir_tree(project, subdir: str) -> Optional[list[dict]]:
    try:
        return project.repository_tree(
            path=subdir, ref=project.default_branch, all=True, recursive=False
        )
    except (GitlabError, AttributeError):
        return None


def _safe_get_file_size(project, path: str) -> Optional[int]:
    try:
        f = project.files.get(file_path=path, ref=project.default_branch)
        return getattr(f, "size", 0) or 0
    except (GitlabError, AttributeError):
        return None


def _safe_list_protected_branches(project) -> list[dict]:
    """Return one dict per protected branch with the attributes our rules
    need: name, allow_force_push. We don't need the access-level lists, so
    we don't pull them.

    Older python-gitlab versions may not expose `allow_force_push` — in that
    case we record None, and rules treat that as "unknown / fail".
    """
    try:
        return [
            {
                "name": b.name,
                "allow_force_push": getattr(b, "allow_force_push", None),
            }
            for b in project.protectedbranches.list(all=True, iterator=True)
        ]
    except (GitlabError, AttributeError):
        return []


def _safe_list_protected_tags(project) -> list[dict]:
    try:
        return [
            {"name": t.name}
            for t in project.protectedtags.list(all=True, iterator=True)
        ]
    except (GitlabError, AttributeError):
        return []


def _safe_list_approval_rules(project) -> list[dict]:
    try:
        rules = project.approvalrules.list(all=True, iterator=True)
        return [
            {"name": r.name, "approvals_required": getattr(r, "approvals_required", 0)}
            for r in rules
        ]
    except (GitlabError, AttributeError):
        return []


def _safe_get_legacy_approvals(project) -> int:
    try:
        approvals = project.approvals.get()
        return getattr(approvals, "approvals_before_merge", 0) or 0
    except (GitlabError, AttributeError):
        return 0


def _safe_get_push_rules(project) -> dict:
    try:
        pr = project.pushrules.get()
        if pr is None:
            return {}
        return {
            "commit_committer_check": bool(getattr(pr, "commit_committer_check", False)),
            "reject_unsigned_commits": bool(getattr(pr, "reject_unsigned_commits", False)),
        }
    except (GitlabError, AttributeError):
        return {}


def _safe_get_repository_size(project) -> int:
    """project.statistics.repository_size is exposed via `project.statistics`
    on full project objects; not always present on lightweight fetches."""
    try:
        stats = getattr(project, "statistics", None) or {}
        if isinstance(stats, dict):
            return int(stats.get("repository_size", 0) or 0)
        return int(getattr(stats, "repository_size", 0) or 0)
    except (GitlabError, AttributeError, TypeError, ValueError):
        return 0


def collect_repo_context(
    gl: gitlab.Gitlab,
    project_id: int,
    pre_fetch_subdirs: Iterable[str] = (),
    extra_root_paths: Iterable[str] = (),
) -> RepoContext:
    project = gl.projects.get(project_id, license=True, statistics=True)

    root_tree = _safe_get_root_tree(project)
    root_filenames = {e["name"] for e in root_tree if e.get("type") == "blob"}
    root_dirnames = {e["name"] for e in root_tree if e.get("type") == "tree"}

    # README sizing for has_substantial_readme
    file_sizes: dict[str, int] = {}
    for readme in _README_NAMES:
        if readme in root_filenames:
            size = _safe_get_file_size(project, readme)
            if size is not None:
                file_sizes[readme] = size
            break

    # Extra root files we need a size for (e.g. .gitignore for has_gitignore).
    for path in extra_root_paths:
        if path in root_filenames and path not in file_sizes:
            size = _safe_get_file_size(project, path)
            if size is not None:
                file_sizes[path] = size

    # Subdirectory listings — only fetch if the subdir actually exists at root.
    subdir_listings: dict[str, list[str]] = {}
    extra_paths_present: set[str] = set()
    for subdir in pre_fetch_subdirs:
        normalized = subdir.rstrip("/")
        top = normalized.split("/", 1)[0]
        if top not in root_dirnames:
            continue
        listing = _safe_get_subdir_tree(project, normalized)
        if listing is None:
            continue
        names = [e["name"] for e in listing]
        subdir_listings[normalized] = names
        # Mark the subdir itself as present so file_exists rules pointing at it succeed.
        extra_paths_present.add(normalized)

    return RepoContext(
        project=project,
        root_tree=root_tree,
        root_filenames=root_filenames,
        root_dirnames=root_dirnames,
        file_sizes=file_sizes,
        extra_paths_present=extra_paths_present,
        subdir_listings=subdir_listings,
        protected_branches=_safe_list_protected_branches(project),
        protected_tags=_safe_list_protected_tags(project),
        approval_rules=_safe_list_approval_rules(project),
        legacy_approvals_required=_safe_get_legacy_approvals(project),
        push_rules=_safe_get_push_rules(project),
        repository_size_bytes=_safe_get_repository_size(project),
    )


# ---------- top-level scan ----------

def _scan_one(gl: gitlab.Gitlab, project_summary, loaded: LoadedRules) -> RepoOutcome:
    path = project_summary.path_with_namespace
    web_url = getattr(project_summary, "web_url", "")
    archived = bool(getattr(project_summary, "archived", False))

    if archived:
        return RepoOutcome(path=path, web_url=web_url, archived=True, skipped_reason="archived")
    if getattr(project_summary, "empty_repo", False):
        return RepoOutcome(
            path=path, web_url=web_url, archived=False, skipped_reason="repository is empty"
        )

    try:
        ctx = collect_repo_context(
            gl, project_summary.id,
            pre_fetch_subdirs=loaded.pre_fetch_subdirs,
            extra_root_paths=loaded.extra_root_paths,
        )
    except GitlabError as e:
        return RepoOutcome(
            path=path, web_url=web_url, archived=False,
            skipped_reason=f"could not fetch project ({e})",
        )

    if not getattr(ctx.project, "default_branch", None):
        return RepoOutcome(
            path=path, web_url=web_url, archived=False,
            skipped_reason="no default branch",
        )

    return RepoOutcome(
        path=path, web_url=web_url, archived=False,
        results=evaluate_all(ctx, loaded.rules),
    )


def scan_projects(
    gl: gitlab.Gitlab,
    project_summaries: Iterable,
    target_path: str,
    loaded_rules: LoadedRules,
    workers: int = 8,
    on_progress: Optional[Callable[[str], None]] = None,
    scope_label: str = "Group",
) -> ScanReport:
    summaries = list(project_summaries)
    outcomes: list[RepoOutcome] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_scan_one, gl, p, loaded_rules): p for p in summaries}
        for fut in as_completed(futures):
            outcome = fut.result()
            outcomes.append(outcome)
            if on_progress:
                on_progress(outcome.path)

    outcomes.sort(key=lambda o: o.path)
    return ScanReport(
        group_path=target_path,
        scanned_at=datetime.now(timezone.utc),
        outcomes=outcomes,
        rules=loaded_rules.rules,
        rules_source_label=loaded_rules.source_label,
        scope_label=scope_label,
    )


# Back-compat alias.
scan_group = scan_projects
