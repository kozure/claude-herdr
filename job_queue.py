"""job_queue.py — filesystem job intake queue (plan Task 4).

Implements spec §6 item 6: job briefs as markdown files under jobs/
named YYYYMMDD-HHMMSS-<slug>.md (second resolution — same-minute
submissions collide at minute resolution; same-second tiebreak is
lexicographic filename order, deterministic).

Protocol: STATUS: line (queued/active/held/done); Alfred works one
active job at a time; the operator preempts by editing/reordering files.
Privileged handoffs (§7c2) carry PRIVILEGED: yes + source_agent.

worker_limit (Q4): caps concurrent herdr sessions backing Alfred's
promoted jobs. N5 scope disclosure: this is an Alfred-side cap only —
direct herdr usage by other agents (Q1=(c), D5) is uncapped; a true
system-wide cap would require daemon changes (spec §13, out of scope).

NOTE: the plan named this file queue.py; renamed to job_queue.py because
queue.py shadows the stdlib module that pytest itself imports at startup
(the local file can never win under pytest). Deviation recorded in the
plan status note.
"""
import os
import re
import tomllib
from datetime import datetime

SLUG_RE = re.compile(r"[^a-z0-9-]+")


class Job:
    """One job brief file."""

    def __init__(self, path):
        self.path = path
        # basename minus .md: YYYYMMDD-HHMMSS-<slug...> (slug may contain -)
        stem = os.path.basename(path)[:-3] if path.endswith(".md") \
            else os.path.basename(path)
        parts = stem.split("-", 2)
        self.ts = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else stem
        self.slug = parts[2] if len(parts) >= 3 else stem
        self.status = None
        for line in open(path).read().splitlines():
            m = re.match(r"STATUS:\s*(\S+)", line)
            if m:
                self.status = m.group(1)
                break

    def __repr__(self):
        return f"<Job {self.slug} {self.status}>"


def _sanitize_slug(name):
    slug = SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "job"


def create_job(jobs_dir, slug, brief, privileged=False, source_agent=None):
    """Write a new queued job brief. Returns the file path."""
    jobs_dir = os.fspath(jobs_dir)
    os.makedirs(jobs_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"{ts}-{_sanitize_slug(slug)}.md"
    path = os.path.join(jobs_dir, fname)
    lines = [
        f"# job: {_sanitize_slug(slug)}",
        f"STATUS: queued",
    ]
    if privileged:
        lines.append("PRIVILEGED: yes")
    if source_agent:
        lines.append(f"source_agent: {source_agent}")
    lines += ["", brief.rstrip(), ""]
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return path


def list_jobs(jobs_dir):
    """All non-done job briefs in FIFO order (filename sort; same-second
    tiebreak is lexicographic — deterministic)."""
    jobs_dir = os.fspath(jobs_dir)
    if not os.path.isdir(jobs_dir):
        return []
    out = []
    for name in sorted(os.listdir(jobs_dir)):
        if not name.endswith(".md"):
            continue
        p = os.path.join(jobs_dir, name)
        if os.path.isfile(p):
            out.append(Job(p))
    return out


def claim_next(jobs_dir, worker_limit=None, active_sessions=0):
    """Claim the FIFO-first queued job, if allowed.

    Returns the claimed Job (file marked STATUS: active), or None when
    another job is already active, nothing is queued, or the worker cap
    is reached (refusal leaves the file untouched — queued).
    """
    ordered = list_jobs(jobs_dir)
    # One active job at a time: if anything is active, refuse.
    if any(j.status == "active" for j in ordered):
        return None
    if worker_limit is not None and active_sessions >= worker_limit:
        return None
    for job in ordered:
        if job.status == "queued":
            _set_status(job.path, "active")
            job.status = "active"
            return job
    return None


def _set_status(path, status):
    text = open(path).read()
    text = re.sub(r"^STATUS:\s*\S+", f"STATUS: {status}", text,
                  count=1, flags=re.M)
    with open(path, "w") as fh:
        fh.write(text)


def mark_held(jobs_dir, slug, reason):
    """Set STATUS: held + reason on the job with this slug."""
    for job in list_jobs(jobs_dir):
        if job.slug == slug:
            text = open(job.path).read()
            text = re.sub(r"^STATUS:\s*\S+", "STATUS: held", text,
                          count=1, flags=re.M)
            if "reason:" not in text:
                text += f"\nreason: {reason}\n"
            else:
                text = re.sub(r"^reason:.*$", f"reason: {reason}", text,
                              count=1, flags=re.M)
            with open(job.path, "w") as fh:
                fh.write(text)
            return True
    return False


def mark_done(jobs_dir, slug):
    """Mark done and archive to jobs/done/."""
    for job in list_jobs(jobs_dir):
        if job.slug == slug:
            done_dir = os.path.join(os.fspath(jobs_dir), "done")
            os.makedirs(done_dir, exist_ok=True)
            _set_status(job.path, "done")
            dest = os.path.join(done_dir, os.path.basename(job.path))
            os.replace(job.path, dest)
            return dest
    return None


def count_active_sessions(client):
    """Number of live herdr sessions backing promoted jobs.

    Tolerates herdr being down (returns 0 — the cap then never blocks,
    which is the safe direction for a soft cap: losing the counter must
    not wedge the queue).
    """
    try:
        sessions = client.agent_list() or []
        return len(sessions)
    except Exception:                            # noqa: BLE001
        return 0


def load_worker_limit(toml_path):
    """worker_limit from [queue] section of liveness.toml; default 3."""
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
        return int(data.get("queue", {}).get("worker_limit", 3))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return 3
