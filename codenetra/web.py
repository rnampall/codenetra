"""FastAPI app — local web UI for Codenetra.

Designed to run on the user's laptop, bound to 127.0.0.1. Reuses the entire
scan / rules / report pipeline from the CLI; this module only adds a thin
HTML-rendering and form-handling layer.

Reports are persisted to ~/.codenetra/reports/<slug>.md so re-opening the app
shows recent scans on the index page.

Scans run in a background thread. POST /scan returns immediately with a
303 redirect to the progress page; the page polls /scan/<id>/status every
500 ms and redirects to the rendered report when complete.
"""
from __future__ import annotations

import os
import re
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# FastAPI / markdown must be imported at module level — not inside get_app() —
# because FastAPI resolves route-handler type annotations via typing.get_type_hints,
# which looks up names in the *module* globals. With `from __future__ import
# annotations` enabled, locally-imported names like `Request` aren't visible to
# that resolver, and FastAPI silently treats them as plain query parameters.
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
)
from fastapi.templating import Jinja2Templates
import markdown as md_lib

from codenetra import __version__
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
    load_rules_from_text,
)
from codenetra.scanner import scan_projects


REPORTS_DIR = Path.home() / ".codenetra" / "reports"
TEMPLATES_DIR = Path(__file__).parent / "templates" / "web"


# ---------- in-memory scan-job store ----------

@dataclass
class ScanJob:
    """A single in-flight or completed scan, kept in memory.

    No DB — the local web app is single-process and ephemeral. Reports get
    persisted to disk under REPORTS_DIR; jobs themselves disappear on
    process restart, which is fine because the user can navigate to the
    saved report directly.

    Lifecycle:
        queued        — created, thread not yet running
        enumerating   — background thread is paginating projects from GitLab
                        (total_repos climbs as we discover them)
        running       — enumeration done; scanner.scan_projects is processing
        completed     — report written to disk; slug set
        failed        — error set; finished_at set
    """
    job_id: str
    state: str = "queued"
    target_label: str = ""
    scope_label: str = ""
    rules_source: str = ""
    total_repos: int = 0
    completed_repos: int = 0
    last_finished_repo: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    slug: str = ""
    error: str = ""


_JOBS: dict[str, ScanJob] = {}
_JOBS_LOCK = threading.Lock()


def _create_job() -> ScanJob:
    job = ScanJob(job_id=uuid.uuid4().hex[:12])
    with _JOBS_LOCK:
        _JOBS[job.job_id] = job
    return job


def _get_job(job_id: str) -> Optional[ScanJob]:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _update_job(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        for key, value in fields.items():
            setattr(job, key, value)


def _bump_progress(job_id: str, finished_path: str) -> None:
    """Increment completed_repos and remember the just-finished repo path.
    Called from threadpool workers; must be lock-guarded."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.completed_repos += 1
        job.last_finished_repo = finished_path


# ---------- helpers ----------

def _slug(scope_label: str, target_path: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9-]+", "-", target_path.lower()).strip("-")
    return f"{ts}-{scope_label.lower()}-{safe}"


def _list_recent_reports(limit: int = 15) -> list[dict]:
    if not REPORTS_DIR.exists():
        return []
    files = sorted(
        REPORTS_DIR.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "slug": f.stem,
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M"
            ),
            "size_kb": round(f.stat().st_size / 1024, 1),
        }
        for f in files[:limit]
    ]


# ---------- FastAPI app ----------

def get_app():
    """Build and return a configured FastAPI app."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="codenetra", version=__version__)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        load_dotenv()
        builtin = load_builtin_rules()
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "gitlab_url": os.environ.get("GITLAB_URL") or "https://gitlab.com",
                "token_present": bool(os.environ.get("GITLAB_TOKEN")),
                "recent_reports": _list_recent_reports(),
                "builtin_rule_count": len(builtin.rules),
                "version": __version__,
            },
        )

    @app.post("/scan")
    async def post_scan(
        request: Request,
        scope: str = Form(...),
        path: str = Form(...),
        workers: int = Form(8),
        no_subgroups: Optional[str] = Form(None),
        rules_file: Optional[UploadFile] = File(None),
        rules_text: Optional[str] = Form(None),
    ):
        path = path.strip()
        if not path:
            return _error(templates, request, "Please provide a group or project path.")
        if scope not in ("group", "project"):
            return _error(templates, request, f"Unknown scope: {scope!r}")
        workers = max(1, min(32, int(workers)))

        # Load rules — uploaded file beats pasted text beats built-in.
        try:
            loaded_rules = await _resolve_rules(rules_file, rules_text)
        except (ValueError, OSError) as e:
            return _error(templates, request, f"Could not load custom rules: {e}")

        load_dotenv()
        env = {
            "GITLAB_URL": os.environ.get("GITLAB_URL"),
            "GITLAB_TOKEN": os.environ.get("GITLAB_TOKEN"),
        }
        try:
            config = GitLabConfig.from_env(env)
        except Exception as e:  # noqa: BLE001
            return _error(templates, request, f"Configuration error: {clean_error_message(e)}")

        # Create the job and redirect immediately. ALL the heavy work —
        # connect, fetch group, paginate projects (which can take tens of
        # seconds on big orgs), then scan — happens in the background thread.
        # That way the user sees the live progress page within milliseconds
        # instead of staring at a blank loading screen.
        job = _create_job()
        _update_job(
            job.job_id,
            target_label=path,                     # provisional — refined once group is fetched
            scope_label="Project" if scope == "project" else "Group",
            rules_source=loaded_rules.source_label,
            state="enumerating",
            started_at=datetime.now(timezone.utc),
        )

        thread = threading.Thread(
            target=_run_full_scan_in_background,
            args=(job.job_id, config, scope, path, bool(no_subgroups), loaded_rules, workers),
            daemon=True,
            name=f"codenetra-scan-{job.job_id}",
        )
        thread.start()

        return RedirectResponse(url=f"/scan/{job.job_id}", status_code=303)

    @app.get("/scan/{job_id}", response_class=HTMLResponse)
    def scan_progress_page(request: Request, job_id: str):
        job = _get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Scan not found")
        # If the scan finished before the user got here, jump straight to it.
        if job.state == "completed" and job.slug:
            return RedirectResponse(url=f"/reports/{job.slug}", status_code=303)
        return templates.TemplateResponse(
            "scan_progress.html",
            {
                "request": request,
                "job": job,
                "version": __version__,
            },
        )

    @app.get("/scan/{job_id}/status")
    def scan_status(job_id: str):
        job = _get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Scan not found")
        elapsed = None
        if job.started_at:
            end = job.finished_at or datetime.now(timezone.utc)
            elapsed = (end - job.started_at).total_seconds()
        return JSONResponse({
            "state": job.state,
            "target": job.target_label,
            "scope": job.scope_label,
            "rules_source": job.rules_source,
            "total": job.total_repos,
            "completed": job.completed_repos,
            "last_finished": job.last_finished_repo,
            "elapsed_seconds": elapsed,
            "slug": job.slug,
            "error": job.error,
        })

    @app.get("/reports/{slug}", response_class=HTMLResponse)
    def view_report(request: Request, slug: str):
        path = _safe_report_path(slug)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Report not found")
        md_text = path.read_text(encoding="utf-8")
        html = md_lib.markdown(
            md_text,
            extensions=[
                "tables", "fenced_code", "sane_lists", "attr_list", "md_in_html",
            ],
        )
        return templates.TemplateResponse(
            "report.html",
            {
                "request": request, "slug": slug, "html": html, "version": __version__,
            },
        )

    @app.get("/reports/{slug}.md")
    def download_report(slug: str):
        path = _safe_report_path(slug)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Report not found")
        return FileResponse(
            path, media_type="text/markdown", filename=f"{slug}.md"
        )

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    return app


def _run_full_scan_in_background(
    job_id: str,
    config: GitLabConfig,
    scope: str,
    path: str,
    no_subgroups: bool,
    loaded_rules: LoadedRules,
    workers: int,
) -> None:
    """End-to-end scan in a background thread.

    Steps performed inside the thread (so the HTTP handler returns instantly):
      1. Connect to GitLab (fails fast on bad token / unreachable host).
      2. Resolve the target — fetch_group() or fetch_project().
      3. Enumerate projects in pages, bumping job.total_repos as we go.
      4. Run the concurrent scan.
      5. Render and persist the report.

    All exceptions are caught and stored on the job record as a clean message.
    """
    try:
        gl = connect(config)
    except Exception as e:  # noqa: BLE001
        _fail(job_id, f"Auth failed: {clean_error_message(e)}")
        return

    # ---- step 2: resolve target ----
    try:
        if scope == "project":
            project = fetch_project(gl, path)
            summaries_iter = iter([project])
            target_label = project.path_with_namespace
            scope_label = "Project"
            estimated_total = 1
        else:
            group = fetch_group(gl, path)
            target_label = group.full_path
            scope_label = "Group"
            estimated_total = 0  # unknown until enumeration finishes
            summaries_iter = list_group_projects(
                group, include_subgroups=not no_subgroups
            )
    except Exception as e:  # noqa: BLE001
        _fail(job_id, f"Could not load {scope} {path!r}: {clean_error_message(e)}")
        return

    _update_job(
        job_id,
        target_label=target_label,
        scope_label=scope_label,
        total_repos=estimated_total,
    )

    # ---- step 3: paginate and grow total_repos as we discover ----
    summaries: list = []
    try:
        for proj in summaries_iter:
            summaries.append(proj)
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job:
                    job.total_repos = len(summaries)
    except Exception as e:  # noqa: BLE001
        _fail(job_id, f"Could not list projects in {target_label!r}: {clean_error_message(e)}")
        return

    if not summaries:
        _fail(job_id, "No projects found for that target.")
        return

    # ---- step 4: run scan ----
    _update_job(job_id, state="running")
    try:
        report = scan_projects(
            gl,
            summaries,
            target_path=target_label,
            loaded_rules=loaded_rules,
            workers=workers,
            scope_label=scope_label,
            on_progress=lambda finished_path: _bump_progress(job_id, finished_path),
        )
    except Exception as e:  # noqa: BLE001
        _fail(job_id, f"Scan failed: {clean_error_message(e)}")
        traceback.print_exc()
        return

    # ---- step 5: render + persist ----
    try:
        markdown_text = render_markdown(report)
        slug = _slug(scope_label, target_label)
        (REPORTS_DIR / f"{slug}.md").write_text(markdown_text, encoding="utf-8")
        _update_job(
            job_id,
            state="completed",
            slug=slug,
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:  # noqa: BLE001
        _fail(job_id, f"Could not write report: {clean_error_message(e)}")
        traceback.print_exc()


def _fail(job_id: str, message: str) -> None:
    _update_job(
        job_id,
        state="failed",
        error=message,
        finished_at=datetime.now(timezone.utc),
    )


# ---------- helpers ----------

def _safe_report_path(slug: str) -> Path:
    """Defend against path traversal — slug must be a bare filename."""
    if "/" in slug or ".." in slug or slug.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid slug")
    return REPORTS_DIR / f"{slug}.md"


def _error(templates, request, message: str, status_code: int = 400):
    """Render the shared error page."""
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "error": message, "version": __version__},
        status_code=status_code,
    )


async def _resolve_rules(
    rules_file: Optional[UploadFile],
    rules_text: Optional[str],
) -> LoadedRules:
    """Decide which rule set to use for this scan.

    Precedence:
      1. Uploaded file (if a non-empty filename was provided)
      2. Pasted text (if non-whitespace)
      3. Built-in rules
    """
    if rules_file is not None and rules_file.filename:
        raw = await rules_file.read()
        text = raw.decode("utf-8", errors="replace")
        return load_rules_from_text(text, filename=rules_file.filename)

    if rules_text and rules_text.strip():
        filename = "pasted.md" if rules_text.lstrip().startswith("#") else "pasted.yaml"
        return load_rules_from_text(rules_text, filename=filename)

    return load_builtin_rules()
