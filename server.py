#!/usr/bin/env python3
"""
Agentic OS — FastAPI Backend
Multi-agent orchestration server for opencode, Hermes, agy CLI
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import time
import uuid
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

_scheduler_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_instance
    try:
        from scheduler.scheduler import CronScheduler
        _scheduler_instance = CronScheduler()
        _scheduler_instance.start()
        print("Event-driven scheduler started")
    except Exception as e:
        print(f"Scheduler not available: {e}")
    yield
    if _scheduler_instance:
        try:
            _scheduler_instance.stop()
        except Exception:
            pass

app = FastAPI(title="Agentic OS", version="0.4.0", lifespan=lifespan)

# Load OpenRouter API key from Hermes .env
HERMES_ENV = Path.home() / ".hermes" / ".env"
if HERMES_ENV.exists():
    for line in HERMES_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k == "OPENROUTER_API_KEY":
                os.environ[k] = v  # last value wins (matches shell sourcing)

# CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8080", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# No-cache for dashboard assets — SPA JS is loaded on demand and updated
# frequently during development (v0.4.0). Prevents stale-cached pages.
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/dashboard") or path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

app.add_middleware(NoCacheMiddleware)

BASE_DIR = Path(__file__).parent.resolve()

# ─── FASE 0: Security configuration ──────────────────────────────────────────
# Config is read from the environment first, then from the repo's .env file.
# python-dotenv is NOT a dependency of this project, so this is a minimal
# parser that only handles the KEY=VALUE form already used in .env.example.

def _load_dotenv(path: Path) -> dict:
    """Minimal .env reader. Supports KEY=VALUE, # comments, and quoted values."""
    values = {}
    if not path.exists():
        return values
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key.strip()] = value
    except OSError:
        pass
    return values


_dotenv = _load_dotenv(BASE_DIR / ".env")


def _config(key: str, default: str = "") -> str:
    """Environment wins over .env so a deployment can override without editing files."""
    return os.environ.get(key) or _dotenv.get(key, default)


# AGENCY_RECOVERY_MODE bypasses authentication entirely. It only takes effect
# when explicitly set in the environment, so it requires the same host access
# that reading .env already requires. This is the documented escape hatch for
# the lock-out scenario: enabling auth with a lost token would otherwise make
# the API — including PUT /api/settings — unreachable.
RECOVERY_MODE = os.environ.get("AGENCY_RECOVERY_MODE") == "1"

API_TOKEN = _config("AGENCY_API_TOKEN")
API_TOKEN_FROM_ENV = bool(os.environ.get("AGENCY_API_TOKEN"))
if not API_TOKEN:
    API_TOKEN = secrets.token_urlsafe(32)

WEBHOOK_SECRET = _config("AGENCY_WEBHOOK_SECRET")
# Transition window: unsigned webhooks are accepted for 7 days after deploy so
# existing emitters are not broken instantly. Remove once all senders sign.
ALLOW_UNSIGNED_WEBHOOKS = _config("ALLOW_UNSIGNED_WEBHOOKS") == "1"
WEBHOOK_TOLERANCE_SECONDS = 300

# 77 of 78 endpoints are sync `def`, so FastAPI runs them on the AnyIO
# threadpool (40 threads). /api/chat blocks one thread for up to 180s while a
# CLI subprocess runs. Without this cap, ~40 concurrent requests saturate the
# pool and the entire API stops responding.
MAX_CONCURRENT_AGENT_RUNS = int(_config("AGENCY_MAX_CONCURRENT_RUNS", "8") or "8")
_agent_slots = None


def _get_agent_slots():
    """Lazily create the semaphore so it binds to the running event loop."""
    global _agent_slots
    if _agent_slots is None:
        _agent_slots = asyncio.Semaphore(MAX_CONCURRENT_AGENT_RUNS)
    return _agent_slots


async def acquire_agent_slot(wait_seconds: float = 30.0) -> None:
    """Wait for an execution slot; raise HTTP 429 with Retry-After if none frees up."""
    slots = _get_agent_slots()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=wait_seconds)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Server busy: all {MAX_CONCURRENT_AGENT_RUNS} execution slots "
                f"are occupied."
            ),
            headers={"Retry-After": str(int(wait_seconds))},
        )


def release_agent_slot() -> None:
    slots = _get_agent_slots()
    try:
        slots.release()
    except ValueError:
        pass


# ─── Rate limiting (token bucket, stdlib only) ───────────────────────────────
# slowapi/limits are deliberately NOT added: they are absent from pyproject.toml
# and adding one would mean touching pyproject.toml, Dockerfile and
# docker-compose.yml. This is a small, auditable implementation instead.

class TokenBucketLimiter:
    """Sliding-window rate limiter keyed by (tier, client identity)."""

    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._hits: dict = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> tuple:
        """Return (allowed, retry_after_seconds, remaining)."""
        async with self._lock:
            now = time.monotonic()
            bucket = self._hits.setdefault(key, deque())
            window = self.capacity / self.refill_per_sec if self.refill_per_sec else 3600.0

            while bucket and (now - bucket[0]) > window:
                bucket.popleft()

            if len(bucket) >= self.capacity:
                retry_after = max(1, int(bucket[0] + window - now) + 1)
                return False, retry_after, 0

            bucket.append(now)
            return True, 0, self.capacity - len(bucket)

    def reset(self) -> None:
        self._hits.clear()


# Generous defaults for reads; tight budgets for expensive agent executions.
RATE_LIMITS = {
    "default": TokenBucketLimiter(capacity=120, refill_per_sec=2.0),
    "chat": TokenBucketLimiter(capacity=10, refill_per_sec=0.2),
    "skill": TokenBucketLimiter(capacity=20, refill_per_sec=0.5),
    "admin": TokenBucketLimiter(capacity=30, refill_per_sec=0.5),
}

RATE_LIMIT_PREFIXES = (
    ("/api/chat", "chat"),
    ("/api/skills", "skill"),
    ("/api/backup", "admin"),
    ("/api/settings", "admin"),
    ("/api/plugins", "admin"),
)


def _rate_tier(path: str) -> str:
    for prefix, tier in RATE_LIMIT_PREFIXES:
        if path.startswith(prefix):
            return tier
    return "default"

# ─── Models ───────────────────────────────────────────────────────

class BrainUpdate(BaseModel):
    content: str

class SkillRunRequest(BaseModel):
    input: Optional[str] = ""
    agent: Optional[str] = "auto"

class ScheduleJobRequest(BaseModel):
    name: str
    skill: str
    cron: str
    enabled: bool = True

class SettingsUpdate(BaseModel):
    settings: dict

class BackupRestoreRequest(BaseModel):
    file: str

class ChatRequest(BaseModel):
    agent: str
    message: str

# ─── Helper Functions ─────────────────────────────────────────────

def read_file(path: Path):
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")

def write_file(path: Path, content: str):
    path.write_text(content, encoding="utf-8")
    return True

def list_dir(path: Path):
    if not path.exists():
        return []
    return sorted([p.name for p in path.iterdir() if not p.name.startswith(".") and p.is_file()])

def get_timestamp():
    return datetime.now(timezone.utc).isoformat()

def append_audit(entry: dict):
    audit_file = BASE_DIR / "audit" / "audit.log"
    entry["timestamp"] = get_timestamp()
    entry["id"] = str(uuid.uuid4())[:8]
    # The audit directory is not tracked in git, so on a fresh clone (or in CI)
    # appending would raise FileNotFoundError and fail the request that was
    # trying to record the audit entry.
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

def safe_resolve(base: Path, user_path: str) -> Path:
    """Resolve a user-supplied path relative to base, preventing traversal.

    Uses Path.is_relative_to(), NOT str.startswith(). A str.startswith check
    is a *text* prefix match, so a sibling directory sharing a name prefix
    passes it: with base=C:/data and user_path='../data-evil/x', the resolved
    path C:/data-evil/x satisfies startswith('C:/data'). is_relative_to()
    compares whole path components, so data-evil != data is correctly rejected.
    """
    root = base.resolve()
    resolved = (root / user_path).resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(400, "Invalid path")
    return resolved


def safe_extractall(tar: tarfile.TarFile, path: Path):
    """Extract a tar archive with path-traversal and symlink protection.

    filter='data' rejects absolute member paths, '..' traversal, and links that
    escape the destination. A manual check of member.name is NOT sufficient on
    its own: a tarball can contain a symlink pointing outside the destination
    followed by a file written through that link, which never appears as a
    suspicious member name. filter='data' covers that case, so the manual loop
    that used to live here has been removed.

    Requires Python 3.12+ (this project targets 3.14.7).
    """
    root = path.resolve()
    tar.extractall(path=root, filter="data")


# ─── Git argument-injection guard ──────────────────────────────────
# `git diff --output=<path>` writes an arbitrary file and `--upload-pack=<cmd>`
# executes a command, so a bare pass-through of a user-supplied ref is
# RCE-adjacent. Git treats any argument starting with '-' as a flag, so the
# allowlist below must reject those outright.
_GIT_REF_RE = re.compile(r"^(?:HEAD|@{1,2}|[A-Za-z0-9_./^~-]{1,255})$")


def validate_git_ref(ref: str) -> str:
    """Reject any git ref that could be parsed as a flag rather than a revision."""
    if not ref or len(ref) > 255:
        raise HTTPException(400, "Invalid git ref")
    if ref.startswith("-"):
        raise HTTPException(400, "Invalid git ref")
    if not _GIT_REF_RE.fullmatch(ref):
        raise HTTPException(400, "Invalid git ref")
    return ref

def validate_identifier(value: str, pattern: str, label: str = "name") -> str:
    """Reject path separators / traversal before using a value in a filesystem path."""
    if not value or not re.fullmatch(pattern, value):
        raise HTTPException(400, f"Invalid {label}")
    return value

# ─── Security Headers Middleware ─────────────────────────────────

class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                ]
                # HSTS is only meaningful over TLS. The server binds loopback
                # and serves plain HTTP, where this header is a no-op that
                # merely hides the absence of TLS. Emit it only when the
                # request actually arrived over HTTPS (e.g. behind a proxy).
                if scope.get("scheme") == "https":
                    extra.append(
                        (b"strict-transport-security",
                         b"max-age=31536000; includeSubDomains")
                    )
                # Only add CSP for non-API routes (dashboard HTML)
                path = scope.get("path", "")
                if not path.startswith("/api/"):
                    csp = (
                        b"default-src 'self'; "
                        b"script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
                        b"font-src 'self' https://fonts.gstatic.com; "
                        b"img-src 'self' data:; "
                        b"connect-src 'self' http://127.0.0.1:* http://localhost:*; "
                        b"frame-ancestors 'none'"
                    )
                    extra.append((b"content-security-policy", csp))
                message["headers"] = list(headers) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)

app.add_middleware(SecurityHeadersMiddleware)

# ─── FASE 0: Auth + Rate limiting middleware ─────────────────────────────────
# These are registered AFTER SecurityHeadersMiddleware because Starlette wraps
# middleware in reverse registration order: the last one added runs first.
# Both are pure-ASGI so they run before routing and require no change to the 78
# existing @app decorators — the project uses no APIRouter, so a global
# `dependencies=[...]` was not an option.

AUTH_HEADER = b"x-agentic-token"


def _json_error(status: int, detail: str, headers: dict = None):
    payload = json.dumps({"detail": detail}).encode("utf-8")
    raw = [(b"content-type", b"application/json"),
           (b"content-length", str(len(payload)).encode())]
    for key, value in (headers or {}).items():
        raw.append((key.encode("latin-1"), str(value).encode("latin-1")))

    async def send_json(send):
        await send({"type": "http.response.start", "status": status, "headers": raw})
        await send({"type": "http.response.body", "body": payload})

    return send_json


class AuthRateLimitMiddleware:
    """Enforces the static API token and per-tier rate limits on /api/*.

    Exempt paths: /api/status (the SPA reads it before it has a token, and it
    exposes nothing sensitive) and non-/api assets (the dashboard itself).
    """

    EXEMPT_PATHS = {"/api/status"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/api/") or path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        provided = headers.get(AUTH_HEADER, b"").decode("latin-1")

        # AGENCY_RECOVERY_MODE=1 is the documented lock-out escape hatch.
        if not RECOVERY_MODE:
            if not provided:
                await _json_error(
                    401, "Missing X-Agentic-Token header.",
                    {"WWW-Authenticate": "ApiKey"},
                )(send)
                return
            # Constant-time compare: a plain == leaks timing information.
            if not hmac.compare_digest(provided, API_TOKEN):
                await _json_error(
                    401, "Invalid API token.",
                    {"WWW-Authenticate": "ApiKey"},
                )(send)
                return

        # Key on the token when present so one client behind a shared NAT
        # cannot exhaust every other client's quota.
        client_ip = (scope.get("client") or ("unknown",))[0]
        identity = provided or client_ip
        tier = _rate_tier(path)
        limiter = RATE_LIMITS[tier]

        allowed, retry_after, remaining = await limiter.check(f"{tier}:{identity}")
        if not allowed:
            await _json_error(
                429,
                f"Rate limit exceeded for tier '{tier}'. Retry in {retry_after}s.",
                {
                    "Retry-After": retry_after,
                    "X-RateLimit-Limit": limiter.capacity,
                    "X-RateLimit-Remaining": 0,
                    "X-RateLimit-Reset": int(time.time()) + retry_after,
                },
            )(send)
            return

        async def send_with_rate_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-ratelimit-limit", str(limiter.capacity).encode()),
                    (b"x-ratelimit-remaining", str(remaining).encode()),
                    (b"x-ratelimit-reset", str(int(time.time() + 60)).encode()),
                ]
            await send(message)

        await self.app(scope, receive, send_with_rate_headers)


app.add_middleware(AuthRateLimitMiddleware)


# ─── Webhook signature verification ─────────────────────────────────────────

def verify_webhook_signature(signature: str, timestamp: str, raw_body: bytes) -> bool:
    """Verify HMAC-SHA256 over f"{timestamp}.{raw_body}".

    Returns True when the webhook may be processed. During the transition
    window (ALLOW_UNSIGNED_WEBHOOKS=1) a request with no signature headers is
    accepted; a request that *does* send them must still verify correctly.
    """
    if not signature or not timestamp:
        # No signature supplied at all — allowed only inside the window.
        return ALLOW_UNSIGNED_WEBHOOKS

    if not WEBHOOK_SECRET:
        print("[SECURITY] Webhook signature supplied but AGENCY_WEBHOOK_SECRET is unset.")
        return False

    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False

    # Anti-replay: reject stale or future-dated timestamps.
    if abs(time.time() - sent_at) > WEBHOOK_TOLERANCE_SECONDS:
        return False

    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

# ─── Agent Discovery (instant filesystem checks) ────────────────────

def check_agent(name: str) -> dict:
    """Instant filesystem-based check. No subprocess needed."""
    try:
        if name == "opencode":
            exists = shutil.which("opencode") is not None
            status = "online" if exists else "offline"
        elif name == "hermes":
            exists = shutil.which("hermes") is not None
            status = "online" if exists else "offline"
        elif name == "agy":
            exists = shutil.which("agy") is not None
            status = "online" if exists else "offline"
        else:
            status = "offline"
    except Exception:
        status = "offline"
    return {"name": name, "status": status}

# ─── Routes: Status ───────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    agents = [check_agent(a) for a in ["opencode", "hermes", "agy"]]
    skills_dir = BASE_DIR / "skills"
    skills = [d.name for d in skills_dir.iterdir()
              if d.is_dir() and not d.name.startswith("_")] if skills_dir.exists() else []
    return {
        "status": "healthy",
        "agents": agents,
        "skills_count": len(skills),
        "uptime": time.time(),
    }

# ─── Routes: Brain ────────────────────────────────────────────────

@app.get("/api/brain")
def list_brain():
    brain_dir = BASE_DIR / "brain"
    if not brain_dir.exists():
        return {}
    files = sorted([p.name for p in brain_dir.iterdir() if p.name.endswith(".md") and p.is_file()])
    brain_data = {}
    for f in files:
        path = brain_dir / f
        brain_data[f] = read_file(path)
    return brain_data

@app.get("/api/brain/{file_name}")
def get_brain_file(file_name: str):
    if ".." in file_name or "/" in file_name:
        raise HTTPException(400, "Invalid file name")
    path = BASE_DIR / "brain" / file_name
    if not path.exists() or path.is_dir():
        raise HTTPException(404, "File not found")
    return {"name": file_name, "content": read_file(path)}

@app.put("/api/brain/{file_name}")
def update_brain_file(file_name: str, data: BrainUpdate):
    if ".." in file_name or "/" in file_name:
        raise HTTPException(400, "Invalid file name")
    path = BASE_DIR / "brain" / file_name
    write_file(path, data.content)
    append_audit({"action": "brain_update", "file": file_name})
    return {"status": "ok", "file": file_name}

# ─── Routes: Skills ───────────────────────────────────────────────

@app.get("/api/skills")
def list_skills():
    skills = []
    for d in sorted((BASE_DIR / "skills").iterdir()):
        if d.is_dir() and not d.name.startswith("_"):
            skill_md = read_file(d / "SKILL.md")
            learnings = read_file(d / "learnings.md")
            eval_data = {}
            eval_path = d / "eval.json"
            if eval_path.exists():
                eval_data = json.loads(eval_path.read_text())
            score_history = []
            score_path = d / "score-history.json"
            if score_path.exists():
                score_history = json.loads(score_path.read_text())
            skills.append({
                "name": d.name,
                "description": skill_md[:200] if skill_md else "",
                "has_learnings": bool(learnings),
                "eval_criteria": eval_data.get("criteria", []),
                "scores": score_history,
            })
    return skills

@app.get("/api/skills/{name}")
def get_skill(name: str):
    validate_identifier(name, r"^[a-zA-Z0-9_-]+$", "skill name")
    path = BASE_DIR / "skills" / name
    if not path.exists():
        raise HTTPException(404, "Skill not found")
    return {
        "name": name,
        "skill": read_file(path / "SKILL.md"),
        "learnings": read_file(path / "learnings.md"),
        "eval": json.loads((path / "eval.json").read_text()) if (path / "eval.json").exists() else {},
        "score_history": json.loads((path / "score-history.json").read_text()) if (path / "score-history.json").exists() else [],
        "context": [f.name for f in (path / "context").iterdir()] if (path / "context").exists() else [],
    }

@app.post("/api/skills/{name}/run")
def run_skill(name: str, req: Optional[SkillRunRequest] = None):
    validate_identifier(name, r"^[a-zA-Z0-9_-]+$", "skill name")
    path = BASE_DIR / "skills" / name
    if not path.exists():
        raise HTTPException(404, "Skill not found")

    agent_choice = req.agent if req else "auto"
    skill_input = req.input if req else ""

    # Read skill files
    skill_md = read_file(path / "SKILL.md")
    learnings = read_file(path / "learnings.md")

    # Determine which agent based on skill type
    if agent_choice == "auto":
        devops_keywords = ["devops", "audit", "deploy", "k8s", "gcp", "infra", "terraform"]
        research_keywords = ["research", "synthesis", "analyze", "search", "compare"]
        if any(k in name for k in devops_keywords):
            agent_choice = "opencode"
        elif any(k in name for k in research_keywords):
            agent_choice = "agy"
        else:
            # Check SKILL.md for explicit agent assignment
            for line in skill_md.split('\n'):
                line = line.strip()
                if "Primary:" in line:
                    candidate = line.split(":")[-1].strip().lower()
                    if candidate in ("opencode", "hermes", "agy"):
                        agent_choice = candidate
                        break
            if agent_choice == "auto":
                agent_choice = "opencode"

    # Build prompt from skill instructions + learnings + user input
    prompt = f"Execute the '{name}' skill.\n\n"
    if skill_md:
        prompt += f"## Skill Instructions\n{skill_md}\n\n"
    if learnings and learnings.strip():
        prompt += f"## Past Learnings\n{learnings}\n\n"
    if skill_input:
        prompt += f"## User Input\n{skill_input}"

    run_id = str(uuid.uuid4())[:8]

    # Execute via agent
    try:
        response_text = execute_agent(agent_choice, prompt)
    except subprocess.TimeoutExpired:
        response_text = f"⏱ Skill '{name}' timed out on agent '{agent_choice}'."
    except FileNotFoundError:
        response_text = f"⚠ Agent '{agent_choice}' CLI not installed. Install it and try again."
    except Exception as e:
        response_text = f"⚠ Error executing skill: {str(e)}"

    # Save output to learnings.md
    timestamp = get_timestamp()[:10]
    existing = read_file(path / "learnings.md")
    new_entry = (
        f"\n## {timestamp} (Run {run_id})\n"
        f"- Agent: {agent_choice}\n"
        f"- Input: {skill_input or '(none)'}\n"
        f"- Output: {response_text[:500]}\n"
    )
    write_file(path / "learnings.md", existing + new_entry)

    # Log execution
    append_audit({
        "action": "skill_run",
        "skill": name,
        "agent": agent_choice,
        "run_id": run_id,
        "output_preview": response_text[:100],
    })

    return {
        "status": "completed",
        "run_id": run_id,
        "skill": name,
        "agent": agent_choice,
        "output": response_text,
        "message": f"Skill '{name}' completed via {agent_choice}",
    }

@app.get("/api/skills/{name}/eval")
def get_skill_eval(name: str):
    validate_identifier(name, r"^[a-zA-Z0-9_-]+$", "skill name")
    path = BASE_DIR / "skills" / name / "score-history.json"
    if not path.exists():
        return {"scores": []}
    return {"scores": json.loads(path.read_text())}

# ─── Routes: Scheduler ────────────────────────────────────────────

@app.get("/api/scheduler/jobs")
def list_jobs():
    jobs_dir = BASE_DIR / "scheduler" / "jobs"
    jobs = []
    for f in sorted(jobs_dir.glob("*.json")):
        jobs.append(json.loads(f.read_text()))
    return jobs

@app.post("/api/scheduler/jobs")
def create_job(job: ScheduleJobRequest):
    jobs_dir = BASE_DIR / "scheduler" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    validate_identifier(job.name, r"^[a-zA-Z0-9 _-]+$", "job name")
    job_data = {
        "id": str(uuid.uuid4())[:8],
        "name": job.name,
        "skill": job.skill,
        "cron": job.cron,
        "enabled": job.enabled,
        "created": get_timestamp(),
        "last_run": None,
        "next_run": None,
    }
    (jobs_dir / f"{job.name.replace(' ', '_')}.json").write_text(
        json.dumps(job_data, indent=2)
    )
    append_audit({"action": "job_created", "job": job.name})
    return job_data

@app.delete("/api/scheduler/jobs/{job_id}")
def delete_job(job_id: str):
    jobs_dir = BASE_DIR / "scheduler" / "jobs"
    for f in jobs_dir.glob("*.json"):
        data = json.loads(f.read_text())
        if data.get("id") == job_id:
            f.unlink()
            append_audit({"action": "job_deleted", "job_id": job_id})
            return {"status": "deleted"}
    raise HTTPException(404, "Job not found")

# ─── Routes: Audit ────────────────────────────────────────────────

@app.get("/api/audit")
def get_audit(limit: int = Query(100, le=500)):
    audit_file = BASE_DIR / "audit" / "audit.log"
    if not audit_file.exists():
        return {"entries": []}
    lines = audit_file.read_text().strip().split("\n")
    entries = [json.loads(l) for l in lines if l.strip()]
    return {"entries": entries[-limit:]}

# ─── Routes: Cost Analytics ───────────────────────────────────────

@app.get("/api/cost")
def get_cost():
    cost_file = BASE_DIR / "data" / "cost-history.json"
    if not cost_file.exists():
        return {"entries": [], "daily_totals": {}, "monthly_projection": 0, "free_tier_alerts": []}
    return json.loads(cost_file.read_text())

@app.post("/api/cost/record")
def record_cost(data: dict):
    cost_file = BASE_DIR / "data" / "cost-history.json"
    cost_data = json.loads(cost_file.read_text()) if cost_file.exists() else \
        {"entries": [], "daily_totals": {}, "monthly_projection": 0, "free_tier_alerts": []}
    cost_data["entries"].append({
        "timestamp": get_timestamp(),
        "agent": data.get("agent", "unknown"),
        "tokens": data.get("tokens", 0),
        "cost": data.get("cost", 0.0),
        "model": data.get("model", "unknown"),
    })
    cost_file.write_text(json.dumps(cost_data, indent=2))
    return {"status": "recorded"}

# ─── Routes: Registry/Plugins ─────────────────────────────────────

@app.get("/api/plugins")
def list_plugins():
    reg_file = BASE_DIR / "registry" / "plugins.json"
    if not reg_file.exists():
        return {"plugins": []}
    return json.loads(reg_file.read_text())

@app.post("/api/plugins/install")
def install_plugin(data: dict):
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Plugin name required")
    reg_file = BASE_DIR / "registry" / "plugins.json"
    reg = json.loads(reg_file.read_text()) if reg_file.exists() else {"plugins": []}
    if any(p["name"] == name for p in reg["plugins"]):
        return {"status": "already_installed"}
    reg["plugins"].append({
        "name": name,
        "installed": get_timestamp(),
        "version": "1.0.0",
    })
    reg_file.write_text(json.dumps(reg, indent=2))
    append_audit({"action": "plugin_installed", "plugin": name})
    return {"status": "installed", "plugin": name}

# ─── Routes: Backup ───────────────────────────────────────────────

@app.get("/api/backups")
def list_backups():
    backup_dir = BASE_DIR / "backups"
    backups = []
    for f in sorted(backup_dir.glob("*.tar.gz"), reverse=True):
        backups.append({
            "name": f.name,
            "size": f.stat().st_size,
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    return backups

@app.post("/api/backup")
def create_backup():
    backup_dir = BASE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"agentic-os-{ts}.tar.gz"
    with tarfile.open(backup_file, "w:gz") as tar:
        for dir_name in ["brain", "skills", "agents", "registry", "standards", "prompts"]:
            d = BASE_DIR / dir_name
            if d.exists():
                tar.add(d, arcname=dir_name)
    append_audit({"action": "backup_created", "file": backup_file.name})
    return {"status": "ok", "file": backup_file.name, "size": backup_file.stat().st_size}

@app.post("/api/backup/restore")
def restore_backup(data: BackupRestoreRequest):
    validate_identifier(data.file, r"^agentic-os-\d{8}_\d{6}\.tar\.gz$", "backup file")
    backup_file = BASE_DIR / "backups" / data.file
    if not backup_file.exists():
        raise HTTPException(404, "Backup file not found")
    with tarfile.open(backup_file, "r:gz") as tar:
        safe_extractall(tar, BASE_DIR)
    append_audit({"action": "backup_restored", "file": data.file})
    return {"status": "restored"}

# ─── Routes: Prompts ──────────────────────────────────────────────

@app.get("/api/prompts")
def list_prompts():
    prompts_dir = BASE_DIR / "prompts"
    prompts = {}
    for f in sorted(prompts_dir.glob("*.md")):
        prompts[f.stem] = read_file(f)
    return prompts

# ─── Routes: Settings ─────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    sf = BASE_DIR / "data" / "settings.json"
    if not sf.exists():
        return {}
    data = json.loads(sf.read_text())
    # Mask sensitive values
    if "api_keys" in data:
        data["api_keys"] = {k: v[:4] + "****" if len(v) > 8 else "****" for k, v in data["api_keys"].items()}
    return data

@app.put("/api/settings")
def update_settings(data: SettingsUpdate):
    sf = BASE_DIR / "data" / "settings.json"
    # Merge with existing
    existing = json.loads(sf.read_text()) if sf.exists() else {}
    existing.update(data.settings)
    sf.write_text(json.dumps(existing, indent=2))
    append_audit({"action": "settings_updated"})
    return {"status": "ok"}

# ─── Routes: Webhooks & Scheduler Events (v0.3.0) ─────────────────

@app.post("/api/webhook")
async def webhook_receiver(request: Request):
    """Generic webhook receiver — triggers skill execution by event type.

    Requires an HMAC-SHA256 signature over f"{timestamp}.{body}" sent as
    X-Signature + X-Agentic-Timestamp. Unsigned requests are only accepted
    while ALLOW_UNSIGNED_WEBHOOKS=1 (7-day migration window).
    """
    raw = await request.body()
    if not verify_webhook_signature(
        request.headers.get("X-Signature"),
        request.headers.get("X-Agentic-Timestamp"),
        raw,
    ):
        raise HTTPException(401, "Invalid or missing webhook signature")

    data = json.loads(raw or b"{}")
    event_type = data.get("event", data.get("type", "unknown"))
    skill_name = data.get("skill", "")
    payload = data.get("payload", {})
    if skill_name:
        from scheduler.scheduler import run_skill
        result = run_skill(skill_name, trigger=f"webhook:{event_type}", input_text=json.dumps(payload))
        append_audit({"action": "webhook_received", "event": event_type, "skill": skill_name})
        return {"status": "processed", "event": event_type, "skill": skill_name, "result": result}
    append_audit({"action": "webhook_received", "event": event_type})
    return {"status": "received", "event": event_type}

@app.get("/api/scheduler/events")
def get_scheduler_events(limit: int = Query(50, le=200)):
    from scheduler.scheduler import get_history
    return {"events": get_history(limit=limit)}

@app.post("/api/scheduler/trigger/{job_id}")
def trigger_job(job_id: str):
    from scheduler.scheduler import get_job_by_id, run_skill
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    result = run_skill(job["skill"], trigger="manual")
    append_audit({"action": "job_triggered", "job_id": job_id, "skill": job["skill"]})
    return result

@app.post("/api/webhook/generic")
def generic_webhook(data: dict):
    """Catch-all webhook receiver for external tool integrations."""
    source = data.get("source", "unknown")
    event = data.get("event", data.get("action", "trigger"))
    skill = data.get("skill", "")
    if skill:
        from scheduler.scheduler import run_skill
        run_skill(skill, trigger=f"webhook:{source}:{event}")
        append_audit({"action": "generic_webhook", "source": source, "event": event, "skill": skill})
    return {"status": "ok", "source": source, "event": event}

# ─── Routes: Memory Search & Auto-Skill Generator (v0.3.0) ─────────

@app.get("/api/memory/search")
def memory_search(q: str = Query(""), limit: int = Query(20, le=100)):
    from brain.memory_search import search, extract_entities
    results = search(q, limit) if q else []
    entities = extract_entities(q) if q else []
    return {"results": results, "entities": entities, "query": q}

@app.post("/api/memory/reindex")
def memory_reindex():
    from brain.memory_search import reindex_all
    reindex_all()
    append_audit({"action": "memory_reindexed"})
    return {"status": "reindexed"}

@app.get("/api/memory/entities")
def list_entities(entity_type: str = "", limit: int = Query(50, le=200)):
    from brain.memory_search import get_entities
    return {"entities": get_entities(entity_type=entity_type, limit=limit)}

@app.get("/api/memory/graph")
def memory_graph():
    """Knowledge graph of memory files, skills, and extracted entities (v0.4.0)."""
    try:
        from brain.memory_search import build_graph
        graph = build_graph()
        append_audit({"action": "memory_graph_viewed", "nodes": graph["stats"]["nodes"]})
        return graph
    except Exception as e:
        return {"nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0}, "error": str(e)}

@app.post("/api/skills/generate")
def generate_skill(data: dict):
    """Auto-generate a SKILL.md from a natural language description."""
    name = data.get("name", "").strip().lower().replace(" ", "-")
    description = data.get("description", "").strip()
    if not name or not description:
        raise HTTPException(400, "Both 'name' and 'description' are required")
    if not re.match(r'^[a-z0-9-]+$', name):
        raise HTTPException(400, "Skill name must be alphanumeric with hyphens")
    skill_dir = BASE_DIR / "skills" / name
    if skill_dir.exists():
        raise HTTPException(409, "Skill already exists")
    skill_dir.mkdir(parents=True)
    (skill_dir / "context").mkdir(exist_ok=True)
    skill_md = f"""# {description}

{description}

## Usage
Generate this skill by running it with appropriate input.

## Input
- Natural language description of what to do

## Output
- Executed task result

## Primary: opencode
"""
    (skill_dir / "SKILL.md").write_text(skill_md)
    (skill_dir / "learnings.md").write_text(f"# {name}\n\nAuto-generated skill.\n")
    eval_data = {"criteria": ["completeness", "accuracy", "efficiency"], "weights": [0.4, 0.3, 0.3]}
    (skill_dir / "eval.json").write_text(json.dumps(eval_data, indent=2))
    (skill_dir / "score-history.json").write_text("[]")
    append_audit({"action": "skill_generated", "name": name, "description": description})
    return {"status": "created", "name": name, "skill": skill_md}

# ─── Routes: Error Tracking (v0.3.0) ───────────────────────────────

ERROR_LOG_FILE = BASE_DIR / "data" / "error-log.json"

def log_error(source: str, message: str, category: str = "general", details: dict = None):
    errors = []
    if ERROR_LOG_FILE.exists():
        errors = json.loads(ERROR_LOG_FILE.read_text())
    errors.append({
        "id": str(uuid.uuid4())[:8],
        "source": source,
        "message": message,
        "category": category,
        "details": details or {},
        "timestamp": get_timestamp(),
    })
    if len(errors) > 500:
        errors = errors[-500:]
    ERROR_LOG_FILE.write_text(json.dumps(errors, indent=2))

@app.get("/api/errors")
def get_errors(limit: int = Query(50, le=200), category: str = ""):
    if not ERROR_LOG_FILE.exists():
        return {"errors": []}
    errors = json.loads(ERROR_LOG_FILE.read_text())
    if category:
        errors = [e for e in errors if e.get("category") == category]
    return {"errors": errors[-limit:]}

@app.delete("/api/errors")
def clear_errors():
    if ERROR_LOG_FILE.exists():
        ERROR_LOG_FILE.write_text("[]")
    return {"status": "cleared"}

@app.post("/api/errors/report")
def report_error(data: dict):
    log_error(
        source=data.get("source", "unknown"),
        message=data.get("message", ""),
        category=data.get("category", "general"),
        details=data.get("details"),
    )
    return {"status": "reported"}

# ─── Circuit Breaker (v0.3.0) ──────────────────────────────────────

CIRCUIT_BREAKER_FILE = BASE_DIR / "data" / "circuit-breaker.json"

def _get_circuit_state() -> dict:
    if CIRCUIT_BREAKER_FILE.exists():
        return json.loads(CIRCUIT_BREAKER_FILE.read_text())
    return {"agents": {}, "threshold": 3, "recovery_timeout": 300}

def _save_circuit_state(state: dict):
    CIRCUIT_BREAKER_FILE.write_text(json.dumps(state, indent=2))

@app.get("/api/circuit-breaker")
def get_circuit_breaker():
    state = _get_circuit_state()
    now = time.time()
    for agent, cb in state.get("agents", {}).items():
        if cb.get("state") == "open" and now - cb.get("opened_at", 0) > state.get("recovery_timeout", 300):
            cb["state"] = "half-open"
    return state

@app.post("/api/circuit-breaker/trip")
def trip_circuit_breaker(data: dict):
    agent = data.get("agent", "")
    if agent not in ["opencode", "hermes", "agy"]:
        raise HTTPException(400, "Invalid agent")
    state = _get_circuit_state()
    if agent not in state["agents"]:
        state["agents"][agent] = {"state": "closed", "failures": 0, "opened_at": None}
    cb = state["agents"][agent]
    cb["failures"] = cb.get("failures", 0) + 1
    if cb["failures"] >= state["threshold"]:
        cb["state"] = "open"
        cb["opened_at"] = time.time()
    _save_circuit_state(state)
    append_audit({"action": "circuit_tripped", "agent": agent, "failures": cb["failures"]})
    return {"agent": agent, "state": cb["state"], "failures": cb["failures"]}

@app.post("/api/circuit-breaker/reset")
def reset_circuit_breaker(data: dict):
    agent = data.get("agent", "")
    if agent not in ["opencode", "hermes", "agy"]:
        raise HTTPException(400, "Invalid agent")
    state = _get_circuit_state()
    state["agents"][agent] = {"state": "closed", "failures": 0, "opened_at": None}
    _save_circuit_state(state)
    return {"agent": agent, "state": "closed"}

# ─── Routes: Standards ────────────────────────────────────────────

@app.get("/api/standards")
def list_standards():
    std_dir = BASE_DIR / "standards"
    if not std_dir.exists():
        return {"standards": []}
    standards = []
    index_file = std_dir / "index.yml"
    index_content = read_file(index_file)
    for f in std_dir.glob("*.md"):
        standards.append({
            "name": f.stem,
            "content": read_file(f),
        })
    return {"standards": standards, "index": index_content}

@app.post("/api/standards/discover")
def discover_standards():
    # Stub: scans codebase for patterns
    append_audit({"action": "standards_discovery_run"})
    return {"status": "discovery_started", "message": "Scanning codebase for patterns..."}

# ─── Routes: Chat ─────────────────────────────────────────────────

CHAT_HISTORY_FILE = BASE_DIR / "data" / "chat-history.json"

def load_chat_history():
    if not CHAT_HISTORY_FILE.exists():
        return {"messages": []}
    try:
        data = json.loads(CHAT_HISTORY_FILE.read_text())
        if isinstance(data, dict) and "messages" in data:
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {"messages": []}

def save_chat_message(msg: dict):
    history = load_chat_history()
    history.setdefault("messages", []).append(msg)
    if len(history["messages"]) > 200:
        history["messages"] = history["messages"][-200:]
    CHAT_HISTORY_FILE.write_text(json.dumps(history, indent=2))

def run_cli(args: list, timeout: int = 30, stdin_payload: str = None) -> tuple:
    """Spawn a CLI without invoking a shell.

    shell=False is the default and must stay that way: it is what prevents
    injection through the arguments themselves.

    When the payload is user-controlled, pass it as stdin_payload rather than
    as an argument wherever the CLI supports that. `hermes chat --query-file -`
    is documented as "safe for arbitrary text: nothing is shell-interpreted,
    so quotes, $(...), and backticks are preserved verbatim". `opencode run`
    has no documented stdin, so for that CLI the caller must validate argv
    instead (see assert_safe_payload).
    """
    r = subprocess.run(
        args,
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    return r.returncode, r.stdout, r.stderr


def assert_safe_payload(payload: str, label: str = "payload") -> str:
    """Reject a payload that would be parsed as a CLI flag if passed as an argv.

    Needed for CLIs with no stdin path (opencode run, agy): an argument
    beginning with '-' is interpreted as an option, not as message text.
    Anything starting with '-' or containing NUL is refused outright.
    """
    if payload is None:
        raise HTTPException(400, "Missing message")
    if payload.startswith("-"):
        raise HTTPException(
            400,
            f"Invalid {label}: must not begin with '-' (it would be parsed as a CLI flag)",
        )
    if "\x00" in payload:
        raise HTTPException(400, f"Invalid {label}: contains NUL byte")
    return payload

def clean_hermes_output(raw: str) -> str:
    """Strip CLI metadata from Hermes output, returning only the AI response."""
    if not raw:
        return ""
    lines = raw.split('\n')
    in_box = False
    content_lines = []
    for line in lines:
        if '╭─' in line:
            in_box = True
            continue
        if '╰─' in line:
            in_box = False
            continue
        if in_box:
            # Remove ANSI escape codes and leading whitespace
            cleaned = line.strip()
            if cleaned:
                content_lines.append(cleaned)
    if content_lines:
        return '\n'.join(content_lines)
    # Fallback: if no box found, return last non-metadata line
    non_meta = [l.strip() for l in lines if l.strip() and not l.startswith(('Query:', 'Initializing', '──', 'Resume', 'Session:', 'Duration:', 'Messages:'))]
    return '\n'.join(non_meta[-5:]) or raw

def execute_agent(agent: str, message: str) -> str:
    try:
        if agent == "opencode":
            try:
                # opencode run has no documented stdin path, so the argv is
                # validated instead of routed around.
                assert_safe_payload(message, "message")
                code, out, err = run_cli(["opencode", "run", "--format", "json", message], timeout=30)
            except subprocess.TimeoutExpired:
                return f"⏱ Agent 'opencode' timed out.\n\nOpenCode's model is taking too long. Try running `opencode run \"{message[:60]}\"` directly in your terminal.\n\n**Message:** {message[:100]}"
            if code == 0:
                response_text = ""
                for line in (out or "").split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        if event.get("type") == "text":
                            text = event.get("part", {}).get("text", "")
                            if text:
                                response_text += text + "\n"
                    except (json.JSONDecodeError, KeyError):
                        continue
                if response_text:
                    return response_text.strip()
                return f"**opencode**\n\nProcessed your message.\n\n**Message:** {message[:100]}"
            err_msg = (err or "").strip()
            return err_msg or f"opencode returned exit code {code}"

        elif agent == "hermes":
            try:
                # `hermes chat --query-file -` reads the query from stdin and is
                # documented as shell-safe ("nothing is shell-interpreted, so
                # quotes, $(...), and backticks are preserved verbatim"), so the
                # user payload never becomes an argv element.
                code, out, err = run_cli(
                    ["hermes", "chat", "--query-file", "-", "--oneshot"],
                    timeout=180,
                    stdin_payload=message,
                )
            except subprocess.TimeoutExpired:
                return f"⏱ Hermes timed out.\n\nThe model took too long to respond. Try a shorter query or check your OpenRouter rate limits.\n\n**Message:** {message[:100]}"
            if code == 0:
                cleaned = clean_hermes_output(out or "")
                if cleaned:
                    return cleaned
                # Empty response from model - return useful fallback
                return f"**Hermes**\n\nReceived your message but the model returned an empty response. Try rephrasing your query.\n\n**Message:** {message}"
            err_msg = (err or "").strip()
            if "invalid choice" in err_msg or "usage:" in err_msg:
                return f"**Hermes needs setup**\n\nRun `hermes setup` or check your config.\n\n**Details:** {err_msg[:200]}"
            return err_msg or f"hermes returned exit code {code}"

        elif agent == "agy":
            try:
                code, out, err = run_cli(["agy", "--print", message], timeout=60)
            except subprocess.TimeoutExpired:
                return f"**agy timed out.**\n\nTry running `agy --print \"{message[:60]}\"` directly."
            combined = ((err or "") + " " + (out or "")).strip()
            if code == 0:
                return (out or "").strip() or f"**agy**\n\nProcessed your query."
            if "auth" in combined.lower() or "login" in combined.lower() or "api key" in combined.lower():
                return f"**agy needs auth**\n\nRun `agy login` to authenticate.\n\n**Details:** {combined[:200]}"
            return combined or f"agy returned exit code {code}"

        else:
            return f"Unknown agent: {agent}"
    except subprocess.TimeoutExpired:
        return f"⏱ Agent '{agent}' timed out.\n\nRun `{agent} --help` in your terminal for CLI usage.\n\n**Message:** {message[:100]}"
    except FileNotFoundError:
        return f"⚠ Agent '{agent}' CLI not installed. Install it and try again."
    except Exception as e:
        return f"⚠ Error communicating with {agent}: {str(e)}"

@app.post("/api/chat")
async def chat(req: ChatRequest):
    agent = req.agent.lower().strip()
    if agent not in ["opencode", "hermes", "agy"]:
        raise HTTPException(400, "Agent must be one of: opencode, hermes, agy")
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(400, "Message cannot be empty")
    if len(message) > 10000:
        raise HTTPException(400, "Message too long (max 10000 characters)")

    # Bound concurrent agent executions. Without this, ~40 concurrent chats
    # saturate the AnyIO threadpool (77/78 endpoints are sync `def`) and the
    # whole API stops responding for up to 180s.
    await acquire_agent_slot()

    try:
        user_msg = {
            "id": str(uuid.uuid4())[:8],
            "role": "user",
            "agent": agent,
            "content": message,
            "timestamp": get_timestamp(),
        }
        save_chat_message(user_msg)

        # execute_agent blocks (subprocess, up to 180s), so it must not run on
        # the event loop itself — offload it to the threadpool.
        response_text = await run_in_threadpool(execute_agent, agent, message)

        agent_msg = {
            "id": str(uuid.uuid4())[:8],
            "role": "assistant",
            "agent": agent,
            "content": response_text,
            "timestamp": get_timestamp(),
        }
        save_chat_message(agent_msg)

        append_audit({"action": "chat_message", "agent": agent, "msg_preview": message[:50]})

        return {"status": "ok", "response": agent_msg}
    finally:
        release_agent_slot()

@app.get("/api/chat/history")
def get_chat_history(q: str = Query(""), agent: str = Query(""), limit: int = Query(200, le=1000)):
    """Chat history with optional search/filter (v0.4.0)."""
    history = load_chat_history()
    messages = history.get("messages", [])
    if q:
        ql = q.lower()
        messages = [m for m in messages if ql in m.get("content", "").lower()]
    if agent:
        messages = [m for m in messages if m.get("agent") == agent]
    if limit:
        messages = messages[-limit:]
    return {"messages": messages, "total": len(messages), "query": q, "agent": agent}

# ─── Routes: Chat File Attachments (v0.4.0) ─────────────────────────

UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
UPLOAD_TTL_HOURS = 24
ALLOWED_UPLOAD_EXTENSIONS = {
    ".txt", ".md", ".log", ".json", ".yml", ".yaml", ".csv", ".py",
    ".js", ".ts", ".sh", ".toml", ".ini", ".env", ".cfg", ".xml",
    ".html", ".css", ".go", ".rs", ".sql", ".tsx", ".jsx",
}

def _cleanup_uploads(force: bool = False):
    """Delete upload files older than the TTL (24h)."""
    if not UPLOAD_DIR.exists():
        return
    now = time.time()
    for f in UPLOAD_DIR.glob("*"):
        try:
            if force or now - f.stat().st_mtime > UPLOAD_TTL_HOURS * 3600:
                f.unlink(missing_ok=True)
        except OSError:
            pass

@app.post("/api/chat/upload")
async def chat_upload(
    agent: str = Form(...),
    message: str = Form(""),
    file: UploadFile = File(...),
):
    """Chat with an optional file attachment (multipart/form-data, v0.4.0)."""
    agent = agent.lower().strip()
    if agent not in ["opencode", "hermes", "agy"]:
        raise HTTPException(400, "Agent must be one of: opencode, hermes, agy")

    raw = await file.read(UPLOAD_MAX_BYTES + 1)
    if len(raw) > UPLOAD_MAX_BYTES:
        raise HTTPException(413, "File too large (max 2 MB)")
    filename = (file.filename or "attachment.txt").strip().replace("\\", "/").split("/")[-1]
    if not re.match(r"^[A-Za-z0-9._-]+$", filename):
        raise HTTPException(400, "Invalid file name")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(400, f"File type .{ext} not allowed")

    _cleanup_uploads()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
    upload_path = UPLOAD_DIR / safe_name
    upload_path.write_bytes(raw)
    append_audit({"action": "chat_upload", "file": filename, "size": len(raw)})

    # Prepend file content to the message so the agent can read it
    try:
        text = raw.decode("utf-8", errors="replace")[:50000]
    except Exception:
        text = "[binary file — content not readable as text]"
    attachment_block = f"--- File: {filename} ---\n{text}\n--- End {filename} ---"
    message = (message or "").strip()
    full_message = f"{attachment_block}\n\n{message}" if message else attachment_block

    # Reuse the standard chat flow
    user_msg = {
        "id": str(uuid.uuid4())[:8],
        "role": "user",
        "agent": agent,
        "content": full_message,
        "timestamp": get_timestamp(),
    }
    save_chat_message(user_msg)
    response_text = execute_agent(agent, full_message)
    agent_msg = {
        "id": str(uuid.uuid4())[:8],
        "role": "assistant",
        "agent": agent,
        "content": response_text,
        "timestamp": get_timestamp(),
    }
    save_chat_message(agent_msg)
    append_audit({"action": "chat_message", "agent": agent, "msg_preview": (message or filename)[:50]})
    return {"status": "ok", "response": agent_msg, "file": filename, "saved_as": safe_name}

# ═══════════════════════════════════════════════════════════════════
# v0.2.0 — New Feature Endpoints
# ═══════════════════════════════════════════════════════════════════

# ─── Models ─────────────────────────────────────────────────────

class KanbanTaskCreate(BaseModel):
    title: str
    body: str = ""
    status: str = "triage"
    priority: str = "medium"
    assignee: str = ""

class KanbanTaskUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None

class KanbanComplete(BaseModel):
    summary: str = ""

class KanbanBlock(BaseModel):
    reason: str = ""

class KanbanCommentCreate(BaseModel):
    message: str

class KanbanLinkCreate(BaseModel):
    parent_id: str
    child_id: str

class GoalCreate(BaseModel):
    title: str
    description: str = ""
    category: str = "general"
    target_date: str = ""

class GoalUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    target_date: Optional[str] = None
    progress: Optional[int] = None
    status: Optional[str] = None

class JournalSave(BaseModel):
    content: str

class RouterSuggest(BaseModel):
    task: str

class RouterRoute(BaseModel):
    task: str
    agent: str

# ─── Data Helpers ───────────────────────────────────────────────

KANBAN_DIR = BASE_DIR / "data" / "kanban"
GOALS_FILE = BASE_DIR / "data" / "goals.json"
JOURNAL_DIR = BASE_DIR / "brain" / "journal"

def ensure_dir(d: Path):
    d.mkdir(parents=True, exist_ok=True)

def load_kanban_tasks():
    ensure_dir(KANBAN_DIR)
    tasks = []
    for f in sorted(KANBAN_DIR.glob("*.json")):
        tasks.append(json.loads(f.read_text()))
    return tasks

def save_kanban_task(task: dict):
    ensure_dir(KANBAN_DIR)
    validate_identifier(str(task["id"]), r"^[a-zA-Z0-9_-]+$", "task id")
    (KANBAN_DIR / f"{task['id']}.json").write_text(json.dumps(task, indent=2))

def load_goals():
    if GOALS_FILE.exists():
        return json.loads(GOALS_FILE.read_text())
    return []

def save_goals(goals: list):
    GOALS_FILE.write_text(json.dumps(goals, indent=2))

# ─── Routes: Kanban Board (13 endpoints) ────────────────────────

@app.get("/api/kanban/board")
def kanban_board(status: Optional[str] = None):
    try:
        tasks = load_kanban_tasks()
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        columns = {"triage": [], "todo": [], "ready": [], "in_progress": [], "blocked": [], "done": []}
        for t in tasks:
            s = t.get("status", "triage")
            if s in columns:
                columns[s].append(t)
        return {"columns": columns, "total": len(tasks)}
    except Exception as e:
        return {"error": str(e), "columns": {}, "total": 0}

@app.get("/api/kanban/tasks/{task_id}")
def kanban_get_task(task_id: str):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    return json.loads(path.read_text())

@app.post("/api/kanban/tasks")
def kanban_create_task(data: KanbanTaskCreate):
    try:
        task = {
            "id": str(uuid.uuid4())[:8],
            "title": data.title,
            "body": data.body,
            "status": data.status,
            "priority": data.priority,
            "assignee": data.assignee,
            "comments": [],
            "links": [],
            "created": get_timestamp(),
            "updated": get_timestamp(),
        }
        save_kanban_task(task)
        append_audit({"action": "kanban_task_created", "title": data.title})
        return task
    except Exception as e:
        raise HTTPException(500, str(e))

@app.patch("/api/kanban/tasks/{task_id}")
def kanban_update_task(task_id: str, data: KanbanTaskUpdate):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    task = json.loads(path.read_text())
    for field in ["title", "body", "status", "priority", "assignee"]:
        val = getattr(data, field, None)
        if val is not None:
            task[field] = val
    task["updated"] = get_timestamp()
    save_kanban_task(task)
    append_audit({"action": "kanban_task_updated", "task_id": task_id})
    return task

@app.post("/api/kanban/tasks/{task_id}/complete")
def kanban_complete_task(task_id: str, data: KanbanComplete):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    task = json.loads(path.read_text())
    task["status"] = "done"
    task["summary"] = data.summary
    task["completed_at"] = get_timestamp()
    task["updated"] = get_timestamp()
    save_kanban_task(task)
    append_audit({"action": "kanban_task_completed", "task_id": task_id})
    return task

@app.post("/api/kanban/tasks/{task_id}/block")
def kanban_block_task(task_id: str, data: KanbanBlock):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    task = json.loads(path.read_text())
    task["status"] = "blocked"
    task["block_reason"] = data.reason
    task["updated"] = get_timestamp()
    save_kanban_task(task)
    append_audit({"action": "kanban_task_blocked", "task_id": task_id})
    return task

@app.post("/api/kanban/tasks/{task_id}/unblock")
def kanban_unblock_task(task_id: str):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    task = json.loads(path.read_text())
    task["status"] = "ready"
    task["block_reason"] = ""
    task["updated"] = get_timestamp()
    save_kanban_task(task)
    append_audit({"action": "kanban_task_unblocked", "task_id": task_id})
    return task

@app.post("/api/kanban/tasks/{task_id}/comments")
def kanban_add_comment(task_id: str, data: KanbanCommentCreate):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    task = json.loads(path.read_text())
    comment = {
        "id": str(uuid.uuid4())[:8],
        "message": data.message,
        "timestamp": get_timestamp(),
    }
    task.setdefault("comments", []).append(comment)
    task["updated"] = get_timestamp()
    save_kanban_task(task)
    return task

@app.post("/api/kanban/links")
def kanban_add_link(data: KanbanLinkCreate):
    for tid in [data.parent_id, data.child_id]:
        validate_identifier(tid, r"^[a-zA-Z0-9_-]+$", "task id")
        path = KANBAN_DIR / f"{tid}.json"
        if not path.exists():
            raise HTTPException(404, f"Task {tid} not found")
        t = json.loads(path.read_text())
        t.setdefault("links", [])
        link = {"parent": data.parent_id, "child": data.child_id}
        if link not in t["links"]:
            t["links"].append(link)
        t["updated"] = get_timestamp()
        save_kanban_task(t)
    append_audit({"action": "kanban_link_added", "parent": data.parent_id, "child": data.child_id})
    return {"status": "linked"}

@app.delete("/api/kanban/links")
def kanban_remove_link(parent_id: str = Query(...), child_id: str = Query(...)):
    for tid in [parent_id, child_id]:
        validate_identifier(tid, r"^[a-zA-Z0-9_-]+$", "task id")
        path = KANBAN_DIR / f"{tid}.json"
        if path.exists():
            t = json.loads(path.read_text())
            t.setdefault("links", [])
            t["links"] = [l for l in t["links"] if not (l.get("parent") == parent_id and l.get("child") == child_id)]
            t["updated"] = get_timestamp()
            save_kanban_task(t)
    return {"status": "unlinked"}

@app.post("/api/kanban/dispatch")
def kanban_dispatch():
    append_audit({"action": "kanban_dispatch_triggered"})
    return {"status": "dispatch_triggered", "message": "Dispatcher notified"}

@app.post("/api/kanban/tasks/{task_id}/specify")
def kanban_specify_task(task_id: str):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    task = json.loads(path.read_text())
    if task.get("status") == "triage":
        task["status"] = "todo"
        task["updated"] = get_timestamp()
        save_kanban_task(task)
    return task

@app.post("/api/kanban/tasks/{task_id}/decompose")
def kanban_decompose_task(task_id: str):
    validate_identifier(task_id, r"^[a-zA-Z0-9_-]+$", "task id")
    path = KANBAN_DIR / f"{task_id}.json"
    if not path.exists():
        raise HTTPException(404, "Task not found")
    task = json.loads(path.read_text())
    children = []
    for i, subtask in enumerate(task.get("body", "").split("\n")):
        subtask = subtask.strip().lstrip("-* ")
        if subtask:
            child = {
                "id": str(uuid.uuid4())[:8],
                "title": subtask[:80],
                "body": subtask,
                "status": "todo",
                "priority": task.get("priority", "medium"),
                "assignee": "",
                "comments": [],
                "links": [{"parent": task_id, "child": ""}],
                "created": get_timestamp(),
                "updated": get_timestamp(),
            }
            child["links"][0]["child"] = child["id"]
            save_kanban_task(child)
            children.append(child)
    return {"parent": task_id, "children": children}

# ─── Routes: Goals (4 endpoints) ─────────────────────────────────

@app.get("/api/goals")
def list_goals():
    try:
        return {"goals": load_goals()}
    except Exception as e:
        return {"goals": [], "error": str(e)}

@app.post("/api/goals")
def create_goal(data: GoalCreate):
    try:
        goals = load_goals()
        goal = {
            "id": str(uuid.uuid4())[:8],
            "title": data.title,
            "description": data.description,
            "category": data.category,
            "target_date": data.target_date,
            "status": "active",
            "progress": 0,
            "created": get_timestamp(),
            "updated": get_timestamp(),
        }
        goals.append(goal)
        save_goals(goals)
        # Auto-sync to brain/active-projects.md
        active_path = BASE_DIR / "brain" / "active-projects.md"
        if active_path.exists():
            existing = active_path.read_text()
            existing += f"\n- [{goal['title']}](goal:{goal['id']}) — {goal['description'][:80]}\n"
            active_path.write_text(existing)
        append_audit({"action": "goal_created", "title": data.title})
        return goal
    except Exception as e:
        raise HTTPException(500, str(e))

@app.put("/api/goals/{goal_id}")
def update_goal(goal_id: str, data: GoalUpdate):
    try:
        goals = load_goals()
        for g in goals:
            if g["id"] == goal_id:
                for field in ["title", "description", "category", "target_date", "progress", "status"]:
                    val = getattr(data, field, None)
                    if val is not None:
                        g[field] = val
                g["updated"] = get_timestamp()
                save_goals(goals)
                return g
        raise HTTPException(404, "Goal not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: str):
    try:
        goals = load_goals()
        goals = [g for g in goals if g["id"] != goal_id]
        save_goals(goals)
        append_audit({"action": "goal_deleted", "goal_id": goal_id})
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(500, str(e))

# ─── Routes: Journal (4 endpoints) ───────────────────────────────

@app.get("/api/journal/entries")
def list_journal_entries():
    try:
        ensure_dir(JOURNAL_DIR)
        entries = []
        for f in sorted(JOURNAL_DIR.glob("*.md"), reverse=True):
            entries.append({
                "date": f.stem,
                "preview": f.read_text()[:200],
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
        return {"entries": entries}
    except Exception as e:
        return {"entries": [], "error": str(e)}

@app.get("/api/journal/entries/{entry_date}")
def get_journal_entry(entry_date: str):
    validate_identifier(entry_date, r"^\d{4}-\d{2}-\d{2}$", "date")
    try:
        path = JOURNAL_DIR / f"{entry_date}.md"
        ensure_dir(JOURNAL_DIR)
        content = path.read_text() if path.exists() else ""
        return {"date": entry_date, "content": content}
    except Exception as e:
        return {"date": entry_date, "content": "", "error": str(e)}

@app.put("/api/journal/entries/{entry_date}")
def save_journal_entry(entry_date: str, data: JournalSave):
    validate_identifier(entry_date, r"^\d{4}-\d{2}-\d{2}$", "date")
    try:
        ensure_dir(JOURNAL_DIR)
        path = JOURNAL_DIR / f"{entry_date}.md"
        path.write_text(data.content)
        append_audit({"action": "journal_saved", "date": entry_date})
        return {"status": "saved", "date": entry_date}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/journal/search")
def search_journal(q: str = Query("")):
    try:
        ensure_dir(JOURNAL_DIR)
        if not q:
            return {"results": []}
        results = []
        for f in JOURNAL_DIR.glob("*.md"):
            content = f.read_text()
            if q.lower() in content.lower():
                results.append({"date": f.stem, "preview": content[:200]})
        return {"results": results, "query": q}
    except Exception as e:
        return {"results": [], "error": str(e)}

# ─── Routes: Agent Health (3 endpoints) ──────────────────────────

@app.get("/api/agents/health")
def get_agent_health():
    try:
        agents = []
        for name in ["opencode", "hermes", "agy"]:
            info = check_agent(name)
            info["uptime"] = 0
            info["success_rate"] = 100
            info["last_seen"] = get_timestamp()
            agents.append(info)
        return {"agents": agents, "updated": get_timestamp()}
    except Exception as e:
        return {"agents": [], "error": str(e), "updated": get_timestamp()}

@app.get("/api/agents/{name}/stats")
def get_agent_stats(name: str):
    try:
        if name not in ["opencode", "hermes", "agy"]:
            raise HTTPException(400, "Invalid agent")
        info = check_agent(name)
        return {
            "name": name,
            "status": info["status"],
            "total_runs": 0,
            "successful_runs": 0,
            "failed_runs": 0,
            "avg_response_time": 0,
            "last_seen": get_timestamp(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/agents/health/refresh")
def refresh_agent_health():
    try:
        agents = []
        for name in ["opencode", "hermes", "agy"]:
            info = check_agent(name)
            agents.append(info)
        append_audit({"action": "agent_health_refreshed"})
        return {"agents": agents, "updated": get_timestamp()}
    except Exception as e:
        return {"agents": [], "error": str(e)}

# ─── Routes: Smart Router (2 endpoints) ─────────────────────────

ROUTER_RULES = {
    "opencode": ["code", "devops", "deploy", "git", "file", "terraform", "docker", "test", "build", "infra", "script"],
    "hermes": ["memory", "schedule", "channel", "skill", "cron", "reminder", "brain", "plugin", "backup"],
    "agy": ["research", "analyze", "search", "compare", "explain", "study", "learn", "document", "report", "review"],
}

@app.post("/api/router/suggest")
def router_suggest(data: RouterSuggest):
    try:
        task_lower = data.task.lower()
        scores = {}
        for agent, keywords in ROUTER_RULES.items():
            scores[agent] = sum(1 for k in keywords if k in task_lower)
        best = max(scores, key=scores.get)
        confidence = "high" if scores[best] >= 2 else "medium" if scores[best] == 1 else "low"
        return {
            "suggested_agent": best,
            "confidence": confidence,
            "scores": scores,
            "task": data.task,
        }
    except Exception as e:
        return {"suggested_agent": "opencode", "confidence": "low", "error": str(e)}

@app.post("/api/router/route")
def router_route(data: RouterRoute):
    try:
        agent = data.agent.lower()
        if agent not in ["opencode", "hermes", "agy"]:
            return {"status": "error", "message": f"Invalid agent: {agent}"}
        append_audit({"action": "task_routed", "agent": agent, "task_preview": data.task[:50]})
        return {
            "status": "routed",
            "agent": agent,
            "task": data.task,
            "message": f"Task routed to {agent}",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ─── Routes: Learning Analytics (2 endpoints) ───────────────────

@app.get("/api/analytics/skills")
def get_skill_analytics():
    try:
        skills_dir = BASE_DIR / "skills"
        analytics = []
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                eval_path = d / "eval.json"
                score_path = d / "score-history.json"
                scores = json.loads(score_path.read_text()) if score_path.exists() else []
                eval_data = json.loads(eval_path.read_text()) if eval_path.exists() else {}
                avg_score = sum(s.get("score", 0) for s in scores) / len(scores) if scores else 0
                analytics.append({
                    "name": d.name,
                    "total_runs": len(scores),
                    "avg_score": round(avg_score, 1),
                    "last_score": scores[-1].get("score", 0) if scores else 0,
                    "trend": "up" if len(scores) >= 2 and scores[-1].get("score", 0) > scores[-2].get("score", 0) else "down" if len(scores) >= 2 else "stable",
                })
        return {"skills": sorted(analytics, key=lambda x: x["total_runs"], reverse=True)}
    except Exception as e:
        return {"skills": [], "error": str(e)}

@app.get("/api/analytics/trends")
def get_trend_analytics():
    try:
        skills_dir = BASE_DIR / "skills"
        trends = []
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                score_path = d / "score-history.json"
                scores = json.loads(score_path.read_text()) if score_path.exists() else []
                if scores:
                    trends.append({
                        "name": d.name,
                        "scores": [s.get("score", 0) for s in scores[-10:]],
                        "labels": [s.get("date", "") for s in scores[-10:]],
                    })
        return {"trends": trends}
    except Exception as e:
        return {"trends": [], "error": str(e)}

# ─── Routes: Session Replay (2 endpoints) ───────────────────────

@app.get("/api/sessions/list")
def list_sessions():
    try:
        sessions = []
        sessions_dir = Path.home() / ".local" / "share" / "opencode"
        log_dir = sessions_dir / "log"
        if log_dir.exists():
            for f in sorted(log_dir.glob("*.log"), reverse=True)[:20]:
                sessions.append({
                    "id": f.stem,
                    "name": f.stem,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                    "source": "opencode",
                })
        hermes_sessions = Path.home() / ".hermes" / "sessions.json"
        if hermes_sessions.exists():
            sessions.append({
                "id": "hermes-sessions",
                "name": "Hermes Session Archive",
                "size": hermes_sessions.stat().st_size,
                "modified": datetime.fromtimestamp(hermes_sessions.stat().st_mtime).isoformat(),
                "source": "hermes",
            })
        return {"sessions": sessions}
    except Exception as e:
        return {"sessions": [], "error": str(e)}

MAX_SESSION_CONTENT = 2000

@app.get("/api/sessions/{session_id}/replay")
def get_session_replay(session_id: str):
    if ".." in session_id or "/" in session_id:
        raise HTTPException(400, "Invalid session ID")
    try:
        sessions_dir = Path.home() / ".local" / "share" / "opencode"
        log_file = sessions_dir / "log" / f"{session_id}.log"
        if log_file.exists():
            content = log_file.read_text()
            lines = content.split("\n")
            messages = []
            for line in lines:
                if "user:" in line.lower() or "assistant:" in line.lower():
                    messages.append(line)
            return {
                "session_id": session_id,
                "lines": len(lines),
                "messages": messages[:50],
                "content": content[:MAX_SESSION_CONTENT],
            }
        return {"session_id": session_id, "messages": [], "content": "Session log not found"}
    except Exception as e:
        return {"session_id": session_id, "messages": [], "error": str(e)}

# ─── Routes: Code Diff Viewer (v0.4.0) ─────────────────────────────

DIFF_ALLOWED_PREFIXES = (
    "brain/", "skills/", "server.py", "scheduler/", "dashboard/",
    "prompts/", "standards/", "agents/", "data/", "registry/", "tests/",
)

@app.get("/api/diff")
def get_diff(file: str = Query(""), ref: str = Query("HEAD")):
    """Unified git diff for a file in the repo (v0.4.0)."""
    try:
        if not file:
            raise HTTPException(400, "Query parameter 'file' is required")
        # Reject refs that git would parse as a flag (--output=, --upload-pack=).
        ref = validate_git_ref(ref)
        # Prevent traversal — allow only repo-relative paths
        resolved = (BASE_DIR / file).resolve()
        if not str(resolved).startswith(str(BASE_DIR.resolve()) + os.sep) and resolved != BASE_DIR:
            raise HTTPException(400, "Invalid file path")
        if not resolved.exists():
            raise HTTPException(404, "File not found")
        rel = str(resolved.relative_to(BASE_DIR))
        # The '--' separator is correct and required for git: everything after
        # it is treated as a pathspec, never as an option.
        code, out, err = run_cli(["git", "-C", str(BASE_DIR), "diff", ref, "--", rel], timeout=10)
        if code == 0 and not out.strip():
            # no diff against ref — try working tree vs index
            return {"file": rel, "diff": "", "changed": False, "ref": ref}
        return {"file": rel, "diff": out or err, "changed": bool(out.strip()), "ref": ref}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ─── Routes: Dashboard Static Files ──────────────────────────────

dashboard_dir = BASE_DIR / "dashboard"
if dashboard_dir.exists():
    app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir)), name="dashboard")

@app.get("/", response_class=HTMLResponse)
def index():
    html_file = BASE_DIR / "dashboard" / "index.html"
    if html_file.exists():
        content = html_file.read_text()
        # Version-agnostic rewrite: handle any ?v= suffix (or none) so both
        # freshly-served and previously-cached index.html resolve correctly.
        content = re.sub(r'href="(styles\.css)(\?v=[0-9.]+)?"',
                         r'href="/dashboard/styles.css\2"', content)
        for name in ("utils.js", "api.js", "app.js"):
            content = re.sub(rf'src="{name}(?:\?v=[0-9.]+)?"',
                             rf'src="/dashboard/{name}"', content)
        content = content.replace('pages/', '/dashboard/pages/')
        return HTMLResponse(content=content)
    return HTMLResponse("<h1>Agentic OS</h1><p>Dashboard not built yet. Run <code>./install.sh</code> first.</p>")

# Root-level fallbacks so stale cached index.html (which references
# root-relative core assets) still resolves even when /dashboard rewrite
# hasn't been seen by the browser (v0.4.1).
for _asset in ("styles.css", "utils.js", "api.js", "app.js"):

    def _serve_asset(asset=_asset):
        f = BASE_DIR / "dashboard" / asset
        if not f.exists():
            raise HTTPException(404, "Not found")
        media = "text/css" if asset.endswith(".css") else "application/javascript"
        return Response(content=f.read_bytes(), media_type=media)

    app.add_api_route(f"/{_asset}", _serve_asset)

# ─── Favicon ──────────────────────────────────────────────────────

FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#6c5ce7"/><stop offset="100%" stop-color="#fd79a8"/></linearGradient></defs><rect width="32" height="32" rx="8" fill="url(#g)"/><polygon points="16,6 24,11 24,21 16,26 8,21 8,11" fill="none" stroke="white" stroke-width="2" stroke-linejoin="round"/><circle cx="16" cy="16" r="3" fill="white"/></svg>'

@app.get("/favicon.ico")
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")

@app.get("/favicon.svg")
def favicon_svg():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")

# ─── PWA Support (v0.3.0) ──────────────────────────────────────────

MANIFEST_JSON = {
    "name": "Agentic OS",
    "short_name": "AgenticOS",
    "description": "Multi-agent orchestration platform",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0f0f23",
    "theme_color": "#6c5ce7",
    "icons": [
        {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
    ],
}

@app.get("/manifest.json")
def manifest():
    return JSONResponse(content=MANIFEST_JSON)

SERVICE_WORKER_JS = """
self.addEventListener('install', (e) => {
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(clients.claim());
});
self.addEventListener('fetch', (e) => {
  e.respondWith(fetch(e.request).catch(() => new Response('Offline', {status: 503})));
});
"""

@app.get("/sw.js")
def service_worker():
    return Response(content=SERVICE_WORKER_JS, media_type="application/javascript")

# ─── Main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument(
        "--insecure-allow-remote",
        action="store_true",
        help=(
            "Bind a non-loopback address even though AGENCY_API_TOKEN is not "
            "set. This exposes all /api endpoints, including backup restore, "
            "to the network. Use only when a token is already exported."
        ),
    )
    args = parser.parse_args()

    if RECOVERY_MODE:
        print("[SECURITY WARNING] AGENCY_RECOVERY_MODE=1 - authentication is DISABLED.")

    if not API_TOKEN_FROM_ENV and not _dotenv.get("AGENCY_API_TOKEN"):
        print(
            "[SECURITY] No AGENCY_API_TOKEN configured. Generated an ephemeral "
            "token for this process only - it changes on every restart:\n"
            f"    {API_TOKEN}\n"
            "Set AGENCY_API_TOKEN in the environment (or in .env) to persist it."
        )

    if (
        args.host not in ("127.0.0.1", "localhost", "::1")
        and not API_TOKEN_FROM_ENV
        and not args.insecure_allow_remote
    ):
        raise SystemExit(
                f"Refusing to bind {args.host} without AGENCY_API_TOKEN.\n"
                "An unauthenticated remote bind exposes all 78 /api endpoints, "
                "including POST /api/backup/restore.\n"
                "Set AGENCY_API_TOKEN, or pass --insecure-allow-remote to "
                "override this check."
            )

    uvicorn.run(app, host=args.host, port=args.port)
