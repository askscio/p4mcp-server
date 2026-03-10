"""
Shared fixtures for P4 MCP Server impersonation tests.

All P4 interactions are faked/mocked so no live Perforce server is required.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

from src.core.config import Config
from src.core.connection import P4ConnectionManager


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_config():
    """Minimal config with impersonation disabled (default)."""
    return Config(
        p4port="ssl:perforce:1666",
        p4user="superuser",
        p4client="test_client",
        log_level="INFO",
        impersonation_enabled=False,
    )


@pytest.fixture
def impersonation_config():
    """Config with impersonation fully enabled."""
    return Config(
        p4port="ssl:perforce:1666",
        p4user="superuser",
        p4client="test_client",
        log_level="INFO",
        impersonation_enabled=True,
    )


@pytest.fixture
def bad_impersonation_config_no_p4user():
    """Config where impersonation is on but P4USER is missing."""
    return Config(
        p4port="ssl:perforce:1666",
        p4user=None,
        p4client="test_client",
        log_level="INFO",
        impersonation_enabled=True,
    )


# ---------------------------------------------------------------------------
# Fake P4 object
# ---------------------------------------------------------------------------


class FakeP4:
    """Lightweight stand-in for a P4Python ``P4`` object."""

    def __init__(self, user="superuser"):
        self.user = user
        self.port = "ssl:perforce:1666"
        self.client = "test_client"
        self.prog = ""
        self.version = ""
        self.ticket_file = None
        self._connected = False

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    def connected(self):
        return self._connected

    def run(self, *args, **kwargs):
        return [{"userName": self.user, "serverVersion": "P4D/LINUX/2024.1/1234567"}]


@pytest.fixture
def fake_p4():
    return FakeP4()


# ---------------------------------------------------------------------------
# Connection manager with faked P4
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_connection_manager(base_config, fake_p4):
    """A P4ConnectionManager whose ``get_connection`` yields a FakeP4."""
    manager = MagicMock(spec=P4ConnectionManager)
    manager.config = base_config
    manager.session_id = "test-session-id"
    manager.actor_user = base_config.p4user

    @asynccontextmanager
    async def _get_connection(effective_user=None):
        p4 = FakeP4(user=effective_user or base_config.p4user)
        yield p4

    manager.get_connection = _get_connection
    return manager
