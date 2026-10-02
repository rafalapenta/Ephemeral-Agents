"""Security regression tests for server.py (Fases 0 and 1).

Run from the repo root:
    unset PYTHONPATH; .venv-test/Scripts/python.exe -m pytest tests/test_server_security.py -v

Note: pyproject.toml sets testpaths=['tests'], but server.py lives in the repo
root and was not covered by any test before this file existed.
"""
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def auth():
    """Authorization header carrying the server's active token."""
    return {"X-Agentic-Token": server.API_TOKEN}


# ─── (c) Path traversal: prefix-collision regression ──────────────────────────
# Regression test for a CONFIRMED vulnerability: safe_resolve used
# str.startswith(), a text prefix match. With base=C:/data and
# user_path='../data-evil/x', the resolved path C:/data-evil/x satisfies
# startswith('C:/data'), so a sibling directory escaped the sandbox.

def test_safe_resolve_rejects_prefix_collision(tmp_path):
    """A sibling dir sharing a name prefix must NOT pass validation."""
    base = tmp_path / "data"
    base.mkdir()
    (tmp_path / "data-evil").mkdir()

    with pytest.raises(HTTPException) as exc:
        server.safe_resolve(base, "../data-evil/secret.txt")
    assert exc.value.status_code == 400


def test_safe_resolve_allows_nested_child(tmp_path):
    """Legitimate nested paths must keep working."""
    base = tmp_path / "data"
    (base / "sub").mkdir(parents=True)

    assert server.safe_resolve(base, "sub/ok.txt") == (base / "sub/ok.txt").resolve()


@pytest.mark.parametrize("attack", [
    "../data-evil/x",
    "../../etc/passwd",
    "..",
    "sub/../../data-evil/x",
])
def test_safe_resolve_rejects_traversal_variants(tmp_path, attack):
    base = tmp_path / "data"
    base.mkdir()
    (tmp_path / "data-evil").mkdir()

    with pytest.raises(HTTPException):
        server.safe_resolve(base, attack)


def test_safe_extractall_blocks_traversal(tmp_path):
    """filter='data' must refuse a member whose name escapes the destination."""
    import tarfile
    import io

    dest = tmp_path / "dest"
    dest.mkdir()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="../escaped.txt")
        payload = b"pwned"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    buf.seek(0)

    with tarfile.open(fileobj=buf, mode="r") as tar:
        with pytest.raises(tarfile.TarError):
            server.safe_extractall(tar, dest)

    assert not (tmp_path / "escaped.txt").exists()


# ─── (d) Git argument injection ──────────────────────────────────────────────
# `git diff --output=<path>` writes an arbitrary file and `--upload-pack=<cmd>`
# executes a command. The `ref` query parameter was passed through unvalidated.

@pytest.mark.parametrize("evil", [
    "--output=C:/Windows/System32/evil",
    "--upload-pack=calc.exe",
    "--ext=diff.gitattributes",
    "-o/tmp/pwned",
    "--no-index",
    "--",
    "--cached",
])
def test_validate_git_ref_rejects_flag_injection(evil):
    with pytest.raises(HTTPException) as exc:
        server.validate_git_ref(evil)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("legit", [
    "HEAD", "HEAD~1", "HEAD^", "main", "v1.2.3",
    "abc1234", "feat/my-branch", "@",
])
def test_validate_git_ref_accepts_legitimate(legit):
    assert server.validate_git_ref(legit) == legit


def test_diff_endpoint_rejects_injected_ref(client, auth):
    r = client.get(
        "/api/diff",
        params={"file": "server.py", "ref": "--output=/tmp/pwned"},
        headers=auth,
    )
    assert r.status_code == 400


# ─── (a) Authentication: 401 without a token ─────────────────────────────────

def test_api_requires_token(client):
    r = client.get("/api/skills")
    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower()


def test_api_rejects_wrong_token(client):
    r = client.get("/api/skills", headers={"X-Agentic-Token": "not-the-token"})
    assert r.status_code == 401


def test_status_stays_public(client):
    """The SPA reads /api/status before it has a token; it must stay open."""
    r = client.get("/api/status")
    assert r.status_code == 200
    assert "status" in r.json()


@pytest.mark.parametrize("method,path", [
    ("get", "/api/skills"),
    ("get", "/api/audit"),
    ("get", "/api/settings"),
    ("get", "/api/kanban/board"),
    ("post", "/api/settings"),
    ("post", "/api/plugins/install"),
    ("post", "/api/skills/generate"),
])
def test_all_protected_endpoints_reject_anonymous(client, method, path):
    # httpx.Client.get() takes no `json=` kwarg — only send a body for verbs
    # that accept one.
    if method == "get":
        r = client.get(path)
    else:
        r = client.post(path, json={})
    assert r.status_code == 401, f"{method.upper()} {path} should require a token"


def test_valid_token_is_accepted(client, auth):
    r = client.get("/api/skills", headers=auth)
    assert r.status_code == 200


def test_destructive_endpoint_requires_token(client):
    """backup/restore writes to disk — it must never be anonymous."""
    r = client.post("/api/backup/restore", json={"file": "agentic-os-20240101_000000.tar.gz"})
    assert r.status_code == 401


# ─── (b) Rate limiting: 429 + Retry-After ────────────────────────────────────

@pytest.fixture
def tight_chat_bucket():
    """Shrink the chat bucket so the test is deterministic and fast."""
    original_capacity = server.RATE_LIMITS["chat"].capacity
    original_refill = server.RATE_LIMITS["chat"].refill_per_sec
    server.RATE_LIMITS["chat"].capacity = 3
    server.RATE_LIMITS["chat"].refill_per_sec = 0.0001
    server.RATE_LIMITS["chat"].reset()
    yield
    server.RATE_LIMITS["chat"].capacity = original_capacity
    server.RATE_LIMITS["chat"].refill_per_sec = original_refill
    server.RATE_LIMITS["chat"].reset()


def test_chat_tier_returns_429_with_retry_after(client, auth, tight_chat_bucket):
    """Overflow must yield 429 plus a Retry-After header, never a hang."""
    codes = []
    for _ in range(6):
        r = client.post("/api/chat", json={"agent": "opencode", "message": "x"}, headers=auth)
        codes.append(r.status_code)
        if r.status_code == 429:
            assert "Retry-After" in r.headers
            assert int(r.headers["Retry-After"]) >= 1
            assert r.headers["X-RateLimit-Remaining"] == "0"

    assert 429 in codes, f"expected a 429 among {codes}"


def test_rate_limit_headers_present_on_success(client, auth):
    """Successful responses must carry quota headers so the UI can show a meter."""
    r = client.get("/api/skills", headers=auth)
    assert r.status_code == 200
    assert "X-RateLimit-Limit" in r.headers
    assert "X-RateLimit-Remaining" in r.headers


def test_token_bucket_allows_burst_up_to_capacity():
    """Direct unit test of the limiter: capacity hits pass, the next is refused."""
    import asyncio

    limiter = server.TokenBucketLimiter(capacity=3, refill_per_sec=0.0001)

    async def run():
        results = []
        for _ in range(5):
            allowed, retry_after, remaining = await limiter.check("k")
            results.append((allowed, retry_after, remaining))
        return results

    results = asyncio.run(run())
    assert [r[0] for r in results] == [True, True, True, False, False]
    assert results[3][1] >= 1


# ─── Webhook signature verification ──────────────────────────────────────────

def test_unsigned_webhook_rejected_when_window_closed(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_UNSIGNED_WEBHOOKS", False)
    assert server.verify_webhook_signature(None, None, b"{}") is False


def test_unsigned_webhook_allowed_inside_transition_window(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_UNSIGNED_WEBHOOKS", True)
    assert server.verify_webhook_signature(None, None, b"{}") is True


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()


def test_valid_webhook_signature_accepted(monkeypatch):
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(server, "ALLOW_UNSIGNED_WEBHOOKS", False)
    body = json.dumps({"skill": "daily-standup"}).encode()
    ts = str(int(time.time()))
    assert server.verify_webhook_signature(_sign("s3cret", ts, body), ts, body) is True


def test_wrong_webhook_signature_rejected(monkeypatch):
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(server, "ALLOW_UNSIGNED_WEBHOOKS", False)
    body = json.dumps({"skill": "x"}).encode()
    ts = str(int(time.time()))
    assert server.verify_webhook_signature(_sign("wrong-secret", ts, body), ts, body) is False


def test_stale_timestamp_rejected_as_replay(monkeypatch):
    """A captured signature must not be replayable after the tolerance window."""
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(server, "ALLOW_UNSIGNED_WEBHOOKS", False)
    body = json.dumps({"skill": "x"}).encode()
    stale_ts = str(int(time.time()) - (server.WEBHOOK_TOLERANCE_SECONDS + 60))
    assert server.verify_webhook_signature(
        _sign("s3cret", stale_ts, body), stale_ts, body
    ) is False


# ─── CLI argv injection ──────────────────────────────────────────────────────

@pytest.mark.parametrize("evil", [
    "--output=/tmp/pwned",
    "-rf",
    "--help",
])
def test_assert_safe_payload_rejects_flag_prefix(evil):
    with pytest.raises(HTTPException) as exc:
        server.assert_safe_payload(evil, "message")
    assert exc.value.status_code == 400


def test_assert_safe_payload_accepts_normal_text():
    text = "Check $(whoami) and `rm -rf` — really"
    assert server.assert_safe_payload(text, "message") == text


def test_run_cli_uses_no_shell(monkeypatch):
    """Guard against shell=True being reintroduced into run_cli."""
    captured = {}

    class FakeResult:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server.run_cli(["echo", "hi"])

    assert captured.get("shell") is False


# ─── Response header hygiene ─────────────────────────────────────────────────

def test_hsts_not_emitted_over_plain_http(client, auth):
    """HSTS is a no-op over HTTP and hid the absence of TLS."""
    r = client.get("/api/skills", headers=auth)
    assert "Strict-Transport-Security" not in r.headers


def test_obsolete_x_xss_protection_removed(client, auth):
    """X-XSS-Protection was removed from Chrome in 2023."""
    r = client.get("/api/skills", headers=auth)
    assert "X-XSS-Protection" not in r.headers