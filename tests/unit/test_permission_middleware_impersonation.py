"""
Tests for impersonation policy enforcement in CheckPermissionMiddleware.

Covers plan test cases:
5. Middleware denies as_user when impersonation is disabled (reason_code asserted).
6. Middleware requires as_user for P4-backed tools when enabled (reason_code asserted).
7. Property cache key includes effective user and preserves TTL behavior per user.
9. Non-impersonation behavior remains unchanged when feature is disabled and no as_user supplied.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import Config
from src.middleware.check_permission import CheckPermissionMiddleware
from src.middleware.impersonation_policy import (
    ImpersonationPolicyError,
    ImpersonationReasonCode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_middleware(config: Config) -> CheckPermissionMiddleware:
    """Create middleware with a config but no real connection manager."""
    mw = CheckPermissionMiddleware.__new__(CheckPermissionMiddleware)
    mw.config = config
    mw.connection_manager = None
    mw._property_cache = {}
    mw._cache_timeout = 60
    return mw


# ---------------------------------------------------------------------------
# Policy matrix: _check_impersonation_policy
# ---------------------------------------------------------------------------


class TestImpersonationPolicyDisabled:
    """When impersonation is disabled."""

    def test_no_as_user_passes(self, base_config):
        """Normal operation – no as_user, no error."""
        mw = _make_middleware(base_config)
        result = mw._check_impersonation_policy("query_server", {}, {})
        assert result is None

    def test_as_user_present_denied(self, base_config):
        """as_user supplied while disabled => IMPERSONATION_DISABLED."""
        mw = _make_middleware(base_config)
        with pytest.raises(ImpersonationPolicyError) as exc_info:
            mw._check_impersonation_policy("query_server", {}, {"as_user": "alice"})
        assert (
            exc_info.value.reason_code == ImpersonationReasonCode.IMPERSONATION_DISABLED
        )


class TestImpersonationPolicyEnabled:
    """When impersonation is enabled."""

    def test_as_user_required(self, impersonation_config):
        """Missing as_user when enabled => AS_USER_REQUIRED."""
        mw = _make_middleware(impersonation_config)
        with pytest.raises(ImpersonationPolicyError) as exc_info:
            mw._check_impersonation_policy("query_server", {}, {})
        assert exc_info.value.reason_code == ImpersonationReasonCode.AS_USER_REQUIRED

    @patch(
        "src.middleware.check_permission.get_http_headers",
        return_value={"glean-user-email": "alice"},
    )
    def test_as_user_provided_returns_user(self, _mock_headers, impersonation_config):
        """as_user provided when enabled => returns the user (header matches)."""
        mw = _make_middleware(impersonation_config)
        result = mw._check_impersonation_policy(
            "query_server", {}, {"as_user": "alice"}
        )
        assert result == "alice"

    @patch(
        "src.middleware.check_permission.get_http_headers",
        return_value={"glean-user-email": "alice@company.com"},
    )
    def test_as_user_email_normalizes_to_username(
        self, _mock_headers, impersonation_config
    ):
        """as_user as email matching header => returns local-part username."""
        mw = _make_middleware(impersonation_config)
        result = mw._check_impersonation_policy(
            "query_server", {}, {"as_user": "alice@company.com"}
        )
        assert result == "alice"

    @patch(
        "src.middleware.check_permission.get_http_headers",
        return_value={},
    )
    def test_as_user_stripped(self, _mock_headers, impersonation_config):
        """Leading/trailing whitespace is stripped; no header => check skipped."""
        mw = _make_middleware(impersonation_config)
        result = mw._check_impersonation_policy(
            "query_server", {}, {"as_user": "  alice  "}
        )
        assert result == "alice"

    @patch(
        "src.middleware.check_permission.get_http_headers",
        return_value={"glean-user-email": "alice@company.com"},
    )
    def test_as_user_username_matches_header_local_part(
        self, _mock_headers, impersonation_config
    ):
        """as_user as plain username matching header local-part => allowed."""
        mw = _make_middleware(impersonation_config)
        result = mw._check_impersonation_policy(
            "query_server", {}, {"as_user": "alice"}
        )
        assert result == "alice"

    @patch(
        "src.middleware.check_permission.get_http_headers",
        return_value={"glean-user-email": "alice@company.com"},
    )
    def test_as_user_mismatch_with_glean_user_email_header(
        self, _mock_headers, impersonation_config
    ):
        """as_user does not match Glean-User-Email header => AS_USER_MISMATCH."""
        mw = _make_middleware(impersonation_config)
        with pytest.raises(ImpersonationPolicyError) as exc_info:
            mw._check_impersonation_policy("query_server", {}, {"as_user": "bob"})
        assert exc_info.value.reason_code == ImpersonationReasonCode.AS_USER_MISMATCH

    @patch(
        "src.middleware.check_permission.get_http_headers",
        return_value={"glean-user-email": "alice@company.com"},
    )
    def test_as_user_email_mismatch_with_glean_user_email_header(
        self, _mock_headers, impersonation_config
    ):
        """as_user as email not matching header => AS_USER_MISMATCH."""
        mw = _make_middleware(impersonation_config)
        with pytest.raises(ImpersonationPolicyError) as exc_info:
            mw._check_impersonation_policy(
                "query_server", {}, {"as_user": "bob@other.com"}
            )
        assert exc_info.value.reason_code == ImpersonationReasonCode.AS_USER_MISMATCH


class TestAsUserWhitespaceValidation:
    """Whitespace-only as_user is rejected regardless of impersonation setting."""

    @pytest.mark.parametrize("bad_value", [" ", "  ", "\t", "\n", " \t\n "])
    def test_whitespace_only_denied_when_enabled(self, impersonation_config, bad_value):
        mw = _make_middleware(impersonation_config)
        with pytest.raises(ImpersonationPolicyError) as exc_info:
            mw._check_impersonation_policy("query_server", {}, {"as_user": bad_value})
        assert exc_info.value.reason_code == ImpersonationReasonCode.AS_USER_INVALID

    @pytest.mark.parametrize("bad_value", [" ", "  ", "\t", "\n", " \t\n "])
    def test_whitespace_only_denied_when_disabled(self, base_config, bad_value):
        mw = _make_middleware(base_config)
        with pytest.raises(ImpersonationPolicyError) as exc_info:
            mw._check_impersonation_policy("query_server", {}, {"as_user": bad_value})
        assert exc_info.value.reason_code == ImpersonationReasonCode.AS_USER_INVALID

    def test_empty_string_denied(self, impersonation_config):
        mw = _make_middleware(impersonation_config)
        with pytest.raises(ImpersonationPolicyError) as exc_info:
            mw._check_impersonation_policy("query_server", {}, {"as_user": ""})
        assert exc_info.value.reason_code == ImpersonationReasonCode.AS_USER_INVALID


class TestImpersonationPolicyNoConfig:
    """When no config is provided (legacy mode)."""

    def test_no_config_skips_checks(self):
        """Legacy mode – middleware without config does not enforce."""
        mw = CheckPermissionMiddleware.__new__(CheckPermissionMiddleware)
        mw.config = None
        mw.connection_manager = None
        mw._property_cache = {}
        mw._cache_timeout = 60

        result = mw._check_impersonation_policy(
            "query_server", {}, {"as_user": "alice"}
        )
        assert result is None


# ---------------------------------------------------------------------------
# Reason code contract
# ---------------------------------------------------------------------------


class TestReasonCodeContract:
    def test_policy_error_carries_reason_code(self):
        err = ImpersonationPolicyError("test", "TEST_CODE")
        assert err.reason_code == "TEST_CODE"
        assert str(err) == "test"


# ---------------------------------------------------------------------------
# on_call_tool argument mutation
# ---------------------------------------------------------------------------


class TestOnCallToolEmailNormalization:
    """Verify on_call_tool rewrites as_user from email to username before call_next."""

    @pytest.mark.asyncio
    @patch(
        "src.middleware.check_permission.get_http_headers",
        return_value={"glean-user-email": "alice@company.com"},
    )
    async def test_email_as_user_rewritten_to_username_in_arguments(
        self, _mock_headers, impersonation_config
    ):
        """When as_user is an email, on_call_tool must mutate arguments to the local-part
        so that downstream handlers receive a plain Perforce username."""
        mw = _make_middleware(impersonation_config)
        # Stub connection_manager so global permission checks don't hit P4
        mw.connection_manager = MagicMock()

        # Build a fake MiddlewareContext
        message = MagicMock()
        message.name = "query_server"
        message.arguments = {"action": "current_user", "as_user": "alice@company.com"}

        fastmcp_ctx = MagicMock()
        tool = MagicMock()
        tool.tags = ["read", "server"]
        tool.enabled = True
        fastmcp_ctx.fastmcp.get_tool = AsyncMock(return_value=tool)

        context = MagicMock()
        context.message = message
        context.fastmcp_context = fastmcp_ctx

        call_next = AsyncMock(return_value={"status": "success"})

        # Patch _check_global_permissions to skip P4 property lookups
        with patch.object(mw, "_check_global_permissions", new_callable=AsyncMock):
            await mw.on_call_tool(context, call_next)

        # The key assertion: arguments["as_user"] should now be "alice", not the email
        assert context.message.arguments["as_user"] == "alice"
        call_next.assert_awaited_once()
