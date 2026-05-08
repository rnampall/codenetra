"""Thin wrapper around python-gitlab.

Centralizes auth and project enumeration so the rule layer can stay focused on
single-project checks.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Iterable

import gitlab
from gitlab.v4.objects import Group, Project


@dataclass(frozen=True)
class GitLabConfig:
    url: str
    token: str

    @classmethod
    def from_env(cls, env: dict) -> "GitLabConfig":
        url = (env.get("GITLAB_URL") or "https://gitlab.com").rstrip("/")
        token = env.get("GITLAB_TOKEN") or ""
        if not token:
            raise RuntimeError(
                "GITLAB_TOKEN is not set. Copy .env.example to .env and fill it in, "
                "or export GITLAB_TOKEN in your shell."
            )
        return cls(url=url, token=token)


def connect(config: GitLabConfig) -> gitlab.Gitlab:
    """Authenticate and return a python-gitlab client.

    Works identically with personal access tokens and group access tokens —
    both authenticate via the PRIVATE-TOKEN header.

    Important configuration:

      * ``retry_transient_errors=True`` — python-gitlab automatically retries
        429 (rate-limit) and 5xx responses with exponential backoff. Without
        this, a single Cloudflare 502 mid-pagination kills a multi-minute
        scan of a large org. With it, the SDK transparently retries.
      * ``obey_rate_limit=True`` — respect GitLab's RateLimit-Reset headers
        rather than blindly hammering when throttled.
      * ``timeout=60`` — give large project listings room to breathe.
      * ``max_retries=15`` — extra cushion for very large org enumerations
        that ride out a longer Cloudflare hiccup.

    We try ``gl.auth()`` as a friendliness probe so a bad token fails fast,
    but we swallow its errors. On GitLab with granular PAT scopes, the
    ``/user`` endpoint that ``auth()`` calls can require ``read_user``, which
    the scan itself doesn't need. If the token can't actually scan, the
    project enumeration step will surface a clear error.
    """
    # Only pass kwargs the installed python-gitlab version actually accepts.
    # Older releases (pre-3.x) don't know about retry_transient_errors,
    # obey_rate_limit, or even per-request timeout. Introspecting the
    # constructor lets us light up the resilience features when available
    # and degrade silently when they aren't, instead of crashing with
    # "unexpected keyword argument".
    base_kwargs = {"url": config.url, "private_token": config.token}
    optional_kwargs = {
        "per_page": 100,
        "retry_transient_errors": True,
        "obey_rate_limit": True,
        "timeout": 60,
    }
    try:
        sig = inspect.signature(gitlab.Gitlab.__init__)
        valid = set(sig.parameters)
    except (TypeError, ValueError):
        valid = set()  # if we can't introspect, fall back to bare call

    accepted_optional = {k: v for k, v in optional_kwargs.items() if k in valid}
    try:
        gl = gitlab.Gitlab(**base_kwargs, **accepted_optional)
    except TypeError:
        # Last-resort fallback: a few exotic builds don't expose a sensible
        # signature. Construct with only the always-supported pair.
        gl = gitlab.Gitlab(**base_kwargs)

    # max_retries is settable on the instance in 3.x+; older versions just
    # ignore the assignment.
    try:
        gl.max_retries = 15
    except Exception:  # noqa: BLE001
        pass

    try:
        gl.auth()
    except Exception:  # noqa: BLE001
        pass
    return gl


def fetch_group(gl: gitlab.Gitlab, group_path: str) -> Group:
    """Resolve a group by full path (e.g. 'acme-corp' or 'acme-corp/platform')."""
    return gl.groups.get(group_path)


def fetch_project(gl: gitlab.Gitlab, project_path: str) -> Project:
    """Resolve a single project by full namespace path.

    Examples: 'acme-corp/api-gateway', 'acme-corp/platform/web-frontend'.
    """
    return gl.projects.get(project_path)


def list_group_projects(
    group: Group, include_subgroups: bool = True
) -> Iterable[Project]:
    """Yield every project in a group.

    Returns lightweight project objects from the group endpoint; the scanner
    re-fetches each project with `gl.projects.get(id, license=True)` to pick up
    fields not included in the listing (license, settings, etc).
    """
    yield from group.projects.list(
        all=True,
        iterator=True,
        include_subgroups=include_subgroups,
        archived=False,
    )


# ---------- error sanitization ----------

import re as _re

_HTML_TAG_RE = _re.compile(r"<[^>]+>")
_WHITESPACE_RE = _re.compile(r"\s+")


def clean_error_message(exc: BaseException, *, max_len: int = 240) -> str:
    """Convert a python-gitlab exception into a short, user-friendly string.

    GitLab's edge (Cloudflare) returns a multi-kilobyte HTML error page on
    502/503/504. python-gitlab embeds the raw body in `str(exc)`, which then
    floods the UI. This helper strips HTML, collapses whitespace, truncates
    to a reasonable length, and adds a hint when we recognize a 502/timeout
    signature so users know to retry with --no-subgroups.
    """
    raw = str(exc).strip()
    cleaned = _HTML_TAG_RE.sub(" ", raw)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()

    looks_like_502 = "502" in raw and ("Bad gateway" in raw or "Cloudflare" in raw)
    looks_like_504 = "504" in raw and "timeout" in raw.lower()
    looks_like_timeout = "timed out" in raw.lower() or "timeout" in raw.lower()

    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"

    hint = ""
    if looks_like_502:
        hint = (
            " GitLab's edge returned 502 (Bad gateway) — typically the upstream "
            "API timed out enumerating a very large group. Try the same scan with "
            "--no-subgroups, or scan a specific subgroup directly."
        )
    elif looks_like_504 or looks_like_timeout:
        hint = (
            " The request timed out. For very large groups, try --no-subgroups "
            "or scan a specific subgroup."
        )

    return f"{cleaned}{hint}".strip()
