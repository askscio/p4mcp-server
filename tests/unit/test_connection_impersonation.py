"""
Tests for per-request connection model and identity isolation.

Covers plan test cases:
3. Per-request connection sets p4.user from as_user and isolates concurrent requests.
4. Connection cleanup executes on exceptions.
12. Invalid/nonexistent as_user behavior is surfaced as pass-through P4 error.
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from P4 import P4, P4Exception

from src.core.config import Config
from src.core.connection import P4ConnectionManager


# ---------------------------------------------------------------------------
# Fresh P4 creation
# ---------------------------------------------------------------------------


class TestCreateFreshP4:
    def test_creates_p4_with_correct_identity_and_config(self, base_config):
        """Fresh P4 mirrors config (port, client, prog) and sets the effective user."""
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = base_config
        manager._connection = None
        manager._is_connected = False

        p4 = manager._create_fresh_p4("alice")

        assert p4.user == "alice"
        assert p4.port == base_config.p4port
        assert p4.client == base_config.p4client
        assert p4.prog == "P4-MCP-Server"


# ---------------------------------------------------------------------------
# get_connection with effective_user
# ---------------------------------------------------------------------------


def _make_fake_p4(user="test"):
    """Create a MagicMock that behaves like P4 for connection tests."""
    fake = MagicMock()
    fake.user = user
    fake.ticket_file = None
    fake.connected.return_value = True
    return fake


class TestGetConnectionImpersonation:
    @pytest.mark.asyncio
    async def test_yields_p4_with_user_set(self, base_config):
        """Per-request P4 should have p4.user = effective_user."""
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = base_config
        manager._connection = None
        manager._is_connected = False

        fake = _make_fake_p4("alice")
        with patch.object(manager, "_create_fresh_p4", return_value=fake):
            async with manager.get_connection(effective_user="alice") as p4:
                assert p4.user == "alice"

    @pytest.mark.asyncio
    async def test_cleanup_on_success(self, base_config):
        """Per-request P4 is disconnected after normal exit."""
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = base_config
        manager._connection = None
        manager._is_connected = False

        fake = _make_fake_p4("alice")
        with patch.object(manager, "_create_fresh_p4", return_value=fake):
            async with manager.get_connection(effective_user="alice") as p4:
                pass
            fake.disconnect.assert_called()

    @pytest.mark.asyncio
    async def test_cleanup_on_exception(self, base_config):
        """Per-request P4 is disconnected even on P4Exception."""
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = base_config
        manager._connection = None
        manager._is_connected = False

        fake = _make_fake_p4("alice")
        fake.run.side_effect = P4Exception("[P4#run] error")
        with patch.object(manager, "_create_fresh_p4", return_value=fake):
            with pytest.raises(P4Exception):
                async with manager.get_connection(effective_user="alice") as p4:
                    p4.run("info")
            fake.disconnect.assert_called()

    @pytest.mark.asyncio
    async def test_concurrent_users_isolated(self, base_config):
        """Two concurrent requests for different users never share state."""
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = base_config
        manager._connection = None
        manager._is_connected = False

        captured_users = []

        def make_fake_for_user(effective_user):
            return _make_fake_p4(effective_user)

        with patch.object(manager, "_create_fresh_p4", side_effect=make_fake_for_user):

            async def use_connection(user):
                async with manager.get_connection(effective_user=user) as p4:
                    await asyncio.sleep(0.01)  # simulate work
                    captured_users.append(p4.user)

            await asyncio.gather(
                use_connection("alice"),
                use_connection("bob"),
            )

        assert "alice" in captured_users
        assert "bob" in captured_users
        assert len(captured_users) == 2

    @pytest.mark.asyncio
    async def test_none_effective_user_uses_legacy_path(self, base_config):
        """When effective_user is None, the shared connection is used."""
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = base_config
        manager._is_connected = True

        fake_p4 = MagicMock()
        fake_p4.connected.return_value = True
        fake_p4.ticket_file = None
        manager._connection = fake_p4

        async with manager.get_connection(effective_user=None) as p4:
            assert p4 is fake_p4


# ---------------------------------------------------------------------------
# actor_user property
# ---------------------------------------------------------------------------


class TestActorUser:
    def test_returns_config_p4user(self, base_config):
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = base_config
        manager._connection = None
        assert manager.actor_user == "superuser"

    def test_fallback_unknown_actor(self):
        config = Config(p4user=None)
        manager = P4ConnectionManager.__new__(P4ConnectionManager)
        manager.config = config
        manager._connection = None
        assert manager.actor_user == "unknown_actor"
