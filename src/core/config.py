"""
Configuration management for P4 MCP server
"""

import os
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _parse_bool_env(name: str, default: bool = False) -> bool:
    """Safely parse a boolean environment variable.

    Accepted truthy values (case-insensitive): ``"1"``, ``"true"``, ``"yes"``.
    Everything else (including unset) resolves to *default*.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


@dataclass
class Config:
    """Configuration for P4 MCP server"""

    # P4 connection settings
    p4port: Optional[str] = None
    p4user: Optional[str] = None
    p4client: Optional[str] = None

    # Tool settings
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL, OFF, QUIET

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
            "impersonation_enabled": self.impersonation_enabled,
        }
