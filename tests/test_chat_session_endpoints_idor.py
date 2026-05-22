# -*- coding: utf-8 -*-
"""
Regression tests for /api/v1/agent/chat/sessions* IDOR hardening.

The chat session endpoints used to accept any ``user_id`` query value and
operate on any ``session_id`` regardless of whether the caller owned that
session. When ``ADMIN_AUTH_ENABLED=false`` (the default single-user setup)
this exposed a CWE-639 IDOR: any unauthenticated network reachable client
could read or delete bot-prefixed conversation history (e.g. Feishu /
Telegram users).

These tests pin down the new policy:

* ``user_id`` must match a conservative shape ``[A-Za-z][A-Za-z0-9_-]{0,63}``.
* When admin auth is disabled, listing/reading/deleting bot-prefixed
  sessions (any session_id containing ``":"``) returns 404 and never
  reveals their content. Web-local sessions (bare UUIDs) remain accessible
  to local callers.
* When admin auth is enabled and the caller presents a valid admin
  cookie, full access is restored (admin owns the box).
"""

from __future__ import annotations

import os
import tempfile
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from src import auth as auth_module
from src.storage import DatabaseManager


@pytest.fixture
def db() -> Iterator[DatabaseManager]:
    """Provide a fresh file-backed DB and seed two bot sessions + one web session.

    A file-backed SQLite DB is required (instead of ``:memory:``) because
    ``TestClient`` dispatches requests on a worker thread distinct from the
    fixture thread, and the default ``sqlite3`` driver enforces thread
    isolation per connection.
    """
    DatabaseManager.reset_instance()
    fd, path = tempfile.mkstemp(prefix="dsa_idor_", suffix=".db")
    os.close(fd)
    try:
        manager = DatabaseManager(db_url=f"sqlite:///{path}")
        manager.save_conversation_message("feishu_u1:abc", "user", "feishu user message")
        manager.save_conversation_message("telegram_42:xyz", "user", "telegram user message")
        manager.save_conversation_message("11111111-2222-3333-4444-555555555555", "user", "web admin message")
        yield manager
    finally:
        DatabaseManager.reset_instance()
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def client(db: DatabaseManager) -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def admin_client(db: DatabaseManager) -> Iterator[TestClient]:
    """TestClient that presents a valid admin session cookie.

    ``api.v1.endpoints.agent`` resolves ``is_auth_enabled`` / ``verify_session``
    through the ``src.auth`` module at call time, so a single patch on
    ``src.auth`` is enough.
    """
    with patch.object(auth_module, "is_auth_enabled", return_value=True), \
         patch.object(auth_module, "_get_session_secret", return_value=b"x" * 32):
        token = auth_module.create_session()
        assert token, "fixture should produce a valid signed session"
        c = TestClient(create_app())
        c.cookies.set(auth_module.COOKIE_NAME, token)
        yield c


# ---------------------------------------------------------------------------
# Anonymous (admin auth disabled) -- the default deployment mode
# ---------------------------------------------------------------------------

class TestAnonymousCaller:
    def test_listing_without_user_id_hides_bot_sessions(self, client: TestClient) -> None:
        resp = client.get("/api/v1/agent/chat/sessions")
        assert resp.status_code == 200
        ids = [s["session_id"] for s in resp.json()["sessions"]]
        # Only the web-local (no ``:``) session is returned.
        assert ids == ["11111111-2222-3333-4444-555555555555"]

    def test_listing_with_user_id_is_rejected(self, client: TestClient) -> None:
        resp = client.get("/api/v1/agent/chat/sessions", params={"user_id": "feishu_u1"})
        assert resp.status_code == 404

    def test_listing_with_other_users_id_does_not_leak(self, client: TestClient) -> None:
        # Even guessing the right prefix yields 404, not the rows.
        resp = client.get("/api/v1/agent/chat/sessions", params={"user_id": "telegram_42"})
        assert resp.status_code == 404

    def test_read_bot_session_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/agent/chat/sessions/feishu_u1:abc")
        assert resp.status_code == 404

    def test_delete_bot_session_is_404(self, client: TestClient, db: DatabaseManager) -> None:
        resp = client.delete("/api/v1/agent/chat/sessions/feishu_u1:abc")
        assert resp.status_code == 404
        # And the row is still there afterwards.
        assert db.conversation_session_exists("feishu_u1:abc")

    def test_read_web_session_still_works(self, client: TestClient) -> None:
        resp = client.get("/api/v1/agent/chat/sessions/11111111-2222-3333-4444-555555555555")
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == 1


# ---------------------------------------------------------------------------
# user_id parameter validation
# ---------------------------------------------------------------------------

class TestUserIdValidation:
    @pytest.mark.parametrize("bad", [
        "feishu:u1",       # ``:`` collides with internal session separator
        "../etc/passwd",   # path traversal probe
        "feishu/u1",       # slash
        "feishu u1",       # space
        "1abc",            # must start with a letter
        "",                # empty -> still treated as "no scope" by validator (see other test)
        "a" * 65,          # too long
        "@user",           # punctuation
    ])
    def test_malformed_user_id_returns_422(self, client: TestClient, bad: str) -> None:
        resp = client.get("/api/v1/agent/chat/sessions", params={"user_id": bad})
        if bad == "":
            # Empty value behaves as if user_id is unset (no scope).
            assert resp.status_code == 200
        else:
            assert resp.status_code == 422, f"expected 422 for {bad!r}, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Admin-authenticated caller -- full access restored
# ---------------------------------------------------------------------------

class TestAdminCaller:
    def test_admin_can_list_with_user_id(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/v1/agent/chat/sessions", params={"user_id": "feishu_u1"})
        assert resp.status_code == 200
        ids = [s["session_id"] for s in resp.json()["sessions"]]
        assert ids == ["feishu_u1:abc"]

    def test_admin_can_list_all_sessions_unfiltered(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/v1/agent/chat/sessions")
        assert resp.status_code == 200
        ids = {s["session_id"] for s in resp.json()["sessions"]}
        assert ids == {
            "feishu_u1:abc",
            "telegram_42:xyz",
            "11111111-2222-3333-4444-555555555555",
        }

    def test_admin_can_read_bot_session(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/v1/agent/chat/sessions/telegram_42:xyz")
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == 1

    def test_admin_can_delete_bot_session(self, admin_client: TestClient, db: DatabaseManager) -> None:
        resp = admin_client.delete("/api/v1/agent/chat/sessions/telegram_42:xyz")
        assert resp.status_code == 200
        assert resp.json()["deleted"] >= 1
        assert not db.conversation_session_exists("telegram_42:xyz")
