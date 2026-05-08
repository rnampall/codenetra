"""Compliance rule primitives.

Rules are no longer hard-coded — they're loaded at runtime from a YAML or
Markdown file (see `rules_loader.py`). This module provides the **check
primitives** that those declarative rules dispatch into.

A rule in YAML looks roughly like this:

    - key: has_security
      title: "Has SECURITY.md"
      tags: [security]
      check: file_exists
      paths: ["SECURITY.md"]
      fix_hint: "Add a SECURITY.md describing how to report vulnerabilities."

The `check:` field names one of the primitives below. Any extra keys are
passed in as parameters to the primitive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional


# ---------- runtime data passed into checks ----------

@dataclass
class RepoContext:
    """Pre-fetched per-repo data the rules read from.

    Populated by `scanner.collect_repo_context`. Keeping all I/O up here
    means each rule function is a pure predicate over already-fetched data.
    """
    project: object  # gitlab.v4.objects.Project (full, with license=True)

    # root_tree: list[{name, path, type}] from the GitLab repository_tree API
    root_tree: list[dict] = field(default_factory=list)
    root_filenames: set[str] = field(default_factory=set)
    root_dirnames: set[str] = field(default_factory=set)

    # path -> size in bytes (0 if missing). Pre-populated for paths the
    # rules ask about.
    file_sizes: dict[str, int] = field(default_factory=dict)

    # path -> True if the file/dir was confirmed to exist (used for
    # non-root-tree lookups that we did via files.get).
    extra_paths_present: set[str] = field(default_factory=set)

    # subdir path -> list of relative names inside it
    subdir_listings: dict[str, list[str]] = field(default_factory=dict)

    # Each entry is a dict: {"name": str, "allow_force_push": bool, ...}
    # Older versions of this code stored bare names; primitives still tolerate
    # that form via _branch_names() below.
    protected_branches: list = field(default_factory=list)
    protected_tags: list[dict] = field(default_factory=list)
    approval_rules: list[dict] = field(default_factory=list)
    legacy_approvals_required: int = 0
    push_rules: dict = field(default_factory=dict)
    repository_size_bytes: int = 0


@dataclass(frozen=True)
class RuleResult:
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class Rule:
    key: str                          # stable identifier, e.g. "has_license"
    title: str                        # display name
    fix_hint: str                     # one-liner shown in the rule reference
    tags: tuple[str, ...]             # e.g. ("security",), ("docs",)
    check: Callable[[RepoContext], RuleResult]
    tier: str = "Custom"              # display group: Baseline / Security / ...
    tier_order: int = 99              # ordinal of `tier` within the configured tier list
    sl_no: int = 0                    # 1-based serial number, assigned by loader


# ---------- helpers ----------

def _exists_in_root(ctx: RepoContext, path: str) -> bool:
    """A path is at root if it's in root_filenames OR root_dirnames OR was
    confirmed via a targeted files.get call."""
    return (
        path in ctx.root_filenames
        or path in ctx.root_dirnames
        or path in ctx.extra_paths_present
    )


def _exists_anywhere(ctx: RepoContext, path: str) -> bool:
    """Looser check — treats subdirectory listings as well."""
    if _exists_in_root(ctx, path):
        return True
    # Check whether `path` is listed as a child of one of the cached
    # subdirectory listings.
    for subdir, names in ctx.subdir_listings.items():
        if path.startswith(subdir.rstrip("/") + "/"):
            child = path[len(subdir.rstrip("/")) + 1 :]
            if child in names:
                return True
    return False


# ---------- check primitives ----------
# Each `_make_X` returns a closure that can be assigned to Rule.check.

def make_file_exists(paths: Iterable[str], **_) -> Callable[[RepoContext], RuleResult]:
    paths = tuple(paths)

    def _check(ctx: RepoContext) -> RuleResult:
        for p in paths:
            if _exists_anywhere(ctx, p):
                return RuleResult(True, f"found {p}")
        # Build a friendly "looked here" message
        if len(paths) == 1:
            return RuleResult(False, f"no {paths[0]}")
        return RuleResult(False, f"none of: {', '.join(paths)}")

    return _check


def make_file_size_min(paths: Iterable[str], min_bytes: int, **_) -> Callable[[RepoContext], RuleResult]:
    paths = tuple(paths)
    min_bytes = int(min_bytes)

    def _check(ctx: RepoContext) -> RuleResult:
        for p in paths:
            if not _exists_anywhere(ctx, p):
                continue
            size = ctx.file_sizes.get(p, 0)
            if size >= min_bytes:
                return RuleResult(True, f"{p}, {size} bytes")
            return RuleResult(False, f"{p} is {size} bytes (need ≥ {min_bytes})")
        return RuleResult(False, f"no {paths[0]}")

    return _check


def make_directory_has_files(directories: Iterable[str], min_files: int = 1, **_) -> Callable[[RepoContext], RuleResult]:
    """Pass if any of the given directories exists with at least `min_files` entries."""
    directories = tuple(directories)
    min_files = int(min_files)

    def _check(ctx: RepoContext) -> RuleResult:
        for d in directories:
            normalized = d.rstrip("/")
            listing = ctx.subdir_listings.get(normalized) or ctx.subdir_listings.get(d)
            if listing is not None and len(listing) >= min_files:
                return RuleResult(True, f"{normalized}/ has {len(listing)} file{'s' if len(listing) != 1 else ''}")
        if len(directories) == 1:
            return RuleResult(False, f"no {directories[0]}/ (or it's empty)")
        return RuleResult(False, f"none of: {', '.join(d.rstrip('/') + '/' for d in directories)}")

    return _check


def make_project_field_truthy(field: str, **_) -> Callable[[RepoContext], RuleResult]:
    field_name = field

    def _check(ctx: RepoContext) -> RuleResult:
        value = getattr(ctx.project, field_name, None)
        if value:
            label = (
                value.get("key", "set") if isinstance(value, dict)
                else (str(value)[:60] + ("..." if len(str(value)) > 60 else ""))
            )
            return RuleResult(True, f"{field_name} = {label}")
        return RuleResult(False, f"{field_name} is empty")

    return _check


def make_project_field_equals(field: str, value: Any, **_) -> Callable[[RepoContext], RuleResult]:
    field_name = field
    target = value

    def _check(ctx: RepoContext) -> RuleResult:
        actual = getattr(ctx.project, field_name, None)
        if actual == target:
            return RuleResult(True, f"{field_name} = {actual!r}")
        return RuleResult(False, f"{field_name} is {actual!r}, expected {target!r}")

    return _check


def make_project_field_not_in(field: str, forbidden: Iterable[Any], **_) -> Callable[[RepoContext], RuleResult]:
    field_name = field
    forbidden_set = set(forbidden)

    def _check(ctx: RepoContext) -> RuleResult:
        actual = getattr(ctx.project, field_name, None)
        if actual not in forbidden_set:
            return RuleResult(True, f"{field_name} = {actual!r}")
        return RuleResult(False, f"{field_name} is {actual!r} (forbidden)")

    return _check


def make_activity_within(max_days: int, field: str = "last_activity_at", **_) -> Callable[[RepoContext], RuleResult]:
    max_days = int(max_days)
    field_name = field

    def _check(ctx: RepoContext) -> RuleResult:
        last = getattr(ctx.project, field_name, None)
        if not last:
            return RuleResult(False, f"no {field_name} recorded")
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return RuleResult(False, f"unparseable {field_name}: {last!r}")
        age = (datetime.now(timezone.utc) - dt).days
        if age <= max_days:
            return RuleResult(True, f"last activity {age} days ago")
        return RuleResult(False, f"last activity {age} days ago (window is {max_days})")

    return _check


def _branch_names(ctx: RepoContext) -> list[str]:
    """Return the names of protected branches, regardless of whether the
    scanner gave us list[str] or list[dict]. Defensive so old fixtures
    still parse."""
    return [
        b if isinstance(b, str) else (b.get("name") if isinstance(b, dict) else None)
        for b in ctx.protected_branches
        if (isinstance(b, str) or isinstance(b, dict))
    ]


def _branch_attr(ctx: RepoContext, name: str, attr: str, default=None):
    """Pull a single attribute (e.g. allow_force_push) from a protected branch
    record. Returns `default` if the entry was a bare string or attr missing."""
    for b in ctx.protected_branches:
        if isinstance(b, dict) and b.get("name") == name:
            return b.get(attr, default)
    return default


def make_protected_default_branch(**_) -> Callable[[RepoContext], RuleResult]:
    def _check(ctx: RepoContext) -> RuleResult:
        default = getattr(ctx.project, "default_branch", None)
        if not default:
            return RuleResult(False, "no default branch")
        if default in _branch_names(ctx):
            return RuleResult(True, f"{default} is protected")
        return RuleResult(False, f"default branch {default} is not protected")

    return _check


def make_default_branch_no_force_push(**_) -> Callable[[RepoContext], RuleResult]:
    def _check(ctx: RepoContext) -> RuleResult:
        default = getattr(ctx.project, "default_branch", None)
        if not default:
            return RuleResult(False, "no default branch")
        if default not in _branch_names(ctx):
            return RuleResult(
                False,
                f"default branch {default} is not protected (force-push allowed by default)",
            )
        force_push = _branch_attr(ctx, default, "allow_force_push", True)
        if force_push is False:
            return RuleResult(True, f"force-push blocked on {default}")
        # `True` or unknown: treat as failing — we want explicit deny.
        return RuleResult(
            False,
            f"force-push allowed on {default} (allow_force_push={force_push})",
        )

    return _check


def make_has_protected_tags(**_) -> Callable[[RepoContext], RuleResult]:
    def _check(ctx: RepoContext) -> RuleResult:
        if not ctx.protected_tags:
            return RuleResult(False, "no protected tags configured")
        names = [t.get("name", "?") for t in ctx.protected_tags if isinstance(t, dict)]
        return RuleResult(
            True,
            f"{len(ctx.protected_tags)} pattern{'s' if len(ctx.protected_tags) != 1 else ''} protected: {', '.join(names[:3])}{'…' if len(names) > 3 else ''}",
        )

    return _check


def make_required_reviewers(min_approvers: int = 1, **_) -> Callable[[RepoContext], RuleResult]:
    min_approvers = int(min_approvers)

    def _check(ctx: RepoContext) -> RuleResult:
        for r in ctx.approval_rules:
            if (r.get("approvals_required") or 0) >= min_approvers:
                return RuleResult(
                    True,
                    f"approval rule '{r.get('name', 'unnamed')}' requires "
                    f"{r['approvals_required']}",
                )
        if ctx.legacy_approvals_required >= min_approvers:
            return RuleResult(
                True, f"legacy approvals_before_merge = {ctx.legacy_approvals_required}"
            )
        return RuleResult(False, f"no approval rule with approvals_required ≥ {min_approvers}")

    return _check


def make_repo_size_max(max_bytes: int, **_) -> Callable[[RepoContext], RuleResult]:
    max_bytes = int(max_bytes)

    def _check(ctx: RepoContext) -> RuleResult:
        size = ctx.repository_size_bytes
        if size == 0:
            return RuleResult(True, "repository_size unavailable; assumed within limit")
        size_mb = size / (1024 * 1024)
        max_mb = max_bytes / (1024 * 1024)
        if size <= max_bytes:
            return RuleResult(True, f"{size_mb:.0f} MB ≤ {max_mb:.0f} MB")
        return RuleResult(False, f"{size_mb:.0f} MB > {max_mb:.0f} MB limit")

    return _check


def make_signed_commits(**_) -> Callable[[RepoContext], RuleResult]:
    def _check(ctx: RepoContext) -> RuleResult:
        rules = ctx.push_rules or {}
        if rules.get("commit_committer_check") or rules.get("reject_unsigned_commits"):
            return RuleResult(True, "commit signing or committer check is enforced")
        return RuleResult(False, "no signed-commit / committer policy on push_rules")

    return _check


# ---------- the dispatch table ----------

CHECK_PRIMITIVES: dict[str, Callable[..., Callable[[RepoContext], RuleResult]]] = {
    "file_exists":                  make_file_exists,
    "file_size_min":                make_file_size_min,
    "directory_has_files":          make_directory_has_files,
    "project_field_truthy":         make_project_field_truthy,
    "project_field_equals":         make_project_field_equals,
    "project_field_not_in":         make_project_field_not_in,
    "activity_within":              make_activity_within,
    "protected_default_branch":     make_protected_default_branch,
    "default_branch_no_force_push": make_default_branch_no_force_push,
    "has_protected_tags":           make_has_protected_tags,
    "required_reviewers":           make_required_reviewers,
    "repo_size_max":                make_repo_size_max,
    "signed_commits":               make_signed_commits,
}


# ---------- evaluation ----------

def evaluate_all(ctx: RepoContext, rules: Iterable[Rule]) -> dict[str, RuleResult]:
    """Run every rule against a context and return keyed results."""
    return {rule.key: rule.check(ctx) for rule in rules}


# ---------- helpers a rule loader will need ----------

def collect_required_paths(rules: Iterable[Rule], path_collector) -> None:
    """Reserved for future use — let the loader/scanner pre-compute which
    paths and subdirs to fetch given the active rule set. Not used yet."""
    pass
