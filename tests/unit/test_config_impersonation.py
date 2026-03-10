"""
Tests for impersonation configuration loading and secure defaults.

Covers plan test cases:
1. Config loads enabled/superuser flags correctly and secure defaults hold.
2. Startup fails fast when enabled but superuser preconditions fail.
"""

import os
import pytest
from unittest.mock import patch

from src.core.config import Config, _parse_bool_env, DEFAULT_SEARCH_MAX_RESULTS_CAP
from src.server import P4MCPServer, ImpersonationConfigError


# ---------------------------------------------------------------------------
# _parse_bool_env helper
# ---------------------------------------------------------------------------


class TestParseBoolEnv:
    def test_returns_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _parse_bool_env("NONEXISTENT_VAR", False) is False
            assert _parse_bool_env("NONEXISTENT_VAR", True) is True

    @pytest.mark.parametrize(
        "value", ["1", "true", "True", "TRUE", "yes", "YES", " true "]
    )
    def test_truthy_values(self, value):
        with patch.dict(os.environ, {"TEST_VAR": value}):
            assert _parse_bool_env("TEST_VAR") is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "random", ""])
    def test_falsy_values(self, value):
        with patch.dict(os.environ, {"TEST_VAR": value}):
            assert _parse_bool_env("TEST_VAR") is False


# ---------------------------------------------------------------------------
# Config.load() – impersonation fields
# ---------------------------------------------------------------------------


class TestConfigImpersonationLoading:
    def test_defaults_to_disabled(self):
        """Secure defaults: impersonation OFF when env vars absent."""
        with patch.dict(os.environ, {}, clear=True):
            config = Config.load()
        assert config.impersonation_enabled is False

    def test_loads_enabled_flag(self):
        env = {
            "MCP_IMPERSONATION_ENABLED": "true",
            "P4PORT": "ssl:p4:1666",
            "P4USER": "super",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.load()
        assert config.impersonation_enabled is True

    @pytest.mark.parametrize("enabled", [True, False])
    def test_to_dict_reflects_impersonation_setting(self, enabled):
        config = Config(impersonation_enabled=enabled)
        d = config.to_dict()
        assert "impersonation_enabled" in d
        assert d["impersonation_enabled"] is enabled

    def test_search_cap_defaults_to_200(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.load()
        assert config.search_max_results_cap == DEFAULT_SEARCH_MAX_RESULTS_CAP

    def test_search_cap_loads_from_env(self):
        with patch.dict(os.environ, {"MCP_SEARCH_MAX_RESULTS_CAP": "75"}, clear=True):
            config = Config.load()
        assert config.search_max_results_cap == 75

    @pytest.mark.parametrize("raw", ["0", "-1", "abc", "  "])
    def test_search_cap_invalid_values_fallback_to_default(self, raw):
        with patch.dict(os.environ, {"MCP_SEARCH_MAX_RESULTS_CAP": raw}, clear=True):
            config = Config.load()
        assert config.search_max_results_cap == DEFAULT_SEARCH_MAX_RESULTS_CAP


# ---------------------------------------------------------------------------
# Startup fail-fast validation
# ---------------------------------------------------------------------------


class TestStartupValidation:
    def test_valid_impersonation_config_passes(self, impersonation_config):
        """No error when impersonation config is valid."""
        P4MCPServer._validate_impersonation_config(
            impersonation_config
        )  # should not raise

    def test_disabled_impersonation_always_passes(self, base_config):
        """No validation when impersonation is disabled."""
        P4MCPServer._validate_impersonation_config(base_config)  # should not raise

    def test_fails_when_p4user_missing(self, bad_impersonation_config_no_p4user):
        """Fail fast: impersonation enabled without P4USER."""
        with pytest.raises(ImpersonationConfigError, match="P4USER"):
            P4MCPServer._validate_impersonation_config(
                bad_impersonation_config_no_p4user
            )
