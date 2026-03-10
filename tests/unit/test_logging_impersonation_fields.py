"""
Tests for audit logging fields under impersonation.

Covers plan test cases:
10. Audit summary event includes request_id, actor, effective, tool, outcome, and sanitized reason context.
11. Actor fallback logs unknown_actor with warning marker when unresolved.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from src.core.config import Config
from src.core.connection import P4ConnectionManager


# ---------------------------------------------------------------------------
# process_tool_logs audit fields
# ---------------------------------------------------------------------------


class TestProcessToolLogsAuditFields:
    """Verify that process_tool_logs produces complete audit summary."""

    def _make_server_stub(self, p4user="superuser", session_id="test-sess"):
        """Build a minimal P4MCPServer-like object with process_tool_logs."""
        from src.server import P4MCPServer

        server = P4MCPServer.__new__(P4MCPServer)
        server.session_id = session_id

        config = Config(p4user=p4user)
        server.p4config = config

        # Fake the connection manager
        manager = MagicMock(spec=P4ConnectionManager)
        manager.actor_user = p4user or "unknown_actor"
        server.p4_manager = manager

        return server

    def test_includes_required_audit_fields(self):
        """All required fields present in the logged payload."""
        server = self._make_server_stub()
        ctx = MagicMock()
        ctx.session.client_params.clientInfo.name = "TestClient"

        result = {"status": "success", "action": "server_info"}

        with patch("src.server.logger") as mock_logger:
            server.process_tool_logs("query_server", result, ctx, as_user="alice")

        # Extract the logged JSON
        call_args = mock_logger.info.call_args
        logged_json = json.loads(call_args[0][1])

        assert "request_id" in logged_json
        assert logged_json["actor_user"] == "superuser"
        assert logged_json["effective_user"] == "alice"
        assert logged_json["tool_name"] == "query_server"
        assert logged_json["outcome"] == "success"
        assert logged_json["session_id"] == "test-sess"

    def test_non_impersonated_call_defaults_and_omits_reason_code(self):
        """When as_user is None, effective_user == actor_user and no reason_code."""
        server = self._make_server_stub()
        ctx = MagicMock()
        ctx.session.client_params.clientInfo.name = "TestClient"

        result = {"status": "success", "action": "server_info"}

        with patch("src.server.logger") as mock_logger:
            server.process_tool_logs("query_server", result, ctx, as_user=None)

        logged_json = json.loads(mock_logger.info.call_args[0][1])
        assert logged_json["effective_user"] == "superuser"
        assert logged_json["actor_user"] == "superuser"
        assert "reason_code" not in logged_json

    def test_reason_code_included_when_present(self):
        """Reason code appears in log when supplied."""
        server = self._make_server_stub()
        ctx = MagicMock()
        ctx.session.client_params.clientInfo.name = "TestClient"

        result = {"status": "denied", "action": "query_files"}

        with patch("src.server.logger") as mock_logger:
            server.process_tool_logs(
                "query_files",
                result,
                ctx,
                as_user="alice",
                reason_code="AS_USER_REQUIRED",
            )

        logged_json = json.loads(mock_logger.info.call_args[0][1])
        assert logged_json["reason_code"] == "AS_USER_REQUIRED"


class TestActorFallback:
    """Actor resolution fallback behaviour."""

    def test_unknown_actor_when_p4user_missing(self):
        """When p4user is None, actor resolves to 'unknown_actor'."""
        from src.server import P4MCPServer

        server = P4MCPServer.__new__(P4MCPServer)
        server.session_id = None

        config = Config(p4user=None)
        server.p4config = config

        manager = MagicMock(spec=P4ConnectionManager)
        manager.actor_user = "unknown_actor"
        server.p4_manager = manager

        ctx = MagicMock()
        ctx.session.client_params.clientInfo.name = "TestClient"
        result = {"status": "success", "action": "test"}

        with patch("src.server.logger") as mock_logger:
            server.process_tool_logs("query_server", result, ctx)

        logged_json = json.loads(mock_logger.info.call_args[0][1])
        assert logged_json["actor_user"] == "unknown_actor"

        # Should have logged a warning about unknown_actor
        warning_calls = [c for c in mock_logger.warning.call_args_list]
        assert any("unknown_actor" in str(c) for c in warning_calls)

    def test_no_secrets_in_log_payload(self):
        """Verify that logged payloads do not contain P4 credentials or tickets."""
        from src.server import P4MCPServer

        server = P4MCPServer.__new__(P4MCPServer)
        server.session_id = "sess-123"

        config = Config(p4user="superuser")
        server.p4config = config

        manager = MagicMock(spec=P4ConnectionManager)
        manager.actor_user = "superuser"
        server.p4_manager = manager

        ctx = MagicMock()
        ctx.session.client_params.clientInfo.name = "TestClient"
        result = {"status": "success", "action": "test"}

        with patch("src.server.logger") as mock_logger:
            server.process_tool_logs("query_server", result, ctx, as_user="alice")

        logged_str = mock_logger.info.call_args[0][1]
        # Sanity: no password/ticket/secret keywords in the log line
        for forbidden in ["password", "ticket", "P4PASSWD", "secret"]:
            assert forbidden.lower() not in logged_str.lower()
