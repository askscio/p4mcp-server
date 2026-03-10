"""
Configuration management for P4 MCP server
"""

import os
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_MAX_RESULTS_CAP = 200


def _parse_bool_env(name: str, default: bool = False) -> bool:
    """Safely parse a boolean environment variable.

    Accepted truthy values (case-insensitive): ``"1"``, ``"true"``, ``"yes"``.
    Everything else (including unset) resolves to *default*.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


def _parse_positive_int_env(name: str, default: int) -> int:
    """Safely parse a positive integer environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default

    try:
        value = int(raw.strip())
        if value <= 0:
            raise ValueError("must be positive")
        return value
    except Exception:
        logger.warning(
            "Invalid %s value '%s'; using default=%s", name, raw, default
        )
        return default


@dataclass
class Config:
    """Configuration for P4 MCP server"""

    # P4 connection settings
    p4port: Optional[str] = None
    p4user: Optional[str] = None
    p4client: Optional[str] = None

    # Tool settings
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL, OFF, QUIET
    search_max_results_cap: int = DEFAULT_SEARCH_MAX_RESULTS_CAP

    # Impersonation settings
    impersonation_enabled: bool = False

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from file or environment variables"""

        # Default configuration
        config_data = {
            "p4port": os.getenv("P4PORT"),
            "p4user": os.getenv("P4USER"),
            "p4client": os.getenv("P4CLIENT"),
            "log_level": os.getenv("LOG_LEVEL", "INFO"),
            "search_max_results_cap": _parse_positive_int_env(
                "MCP_SEARCH_MAX_RESULTS_CAP", DEFAULT_SEARCH_MAX_RESULTS_CAP
            ),
            "impersonation_enabled": _parse_bool_env(
                "MCP_IMPERSONATION_ENABLED", False
            ),
        }
        config_data = {k: v for k, v in config_data.items() if v is not None}
        return cls(**config_data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary"""
        return {
            "p4port": self.p4port,
            "p4user": self.p4user,
            "p4client": self.p4client,
            "log_level": self.log_level,
            "search_max_results_cap": self.search_max_results_cap,
            "impersonation_enabled": self.impersonation_enabled,
        }
