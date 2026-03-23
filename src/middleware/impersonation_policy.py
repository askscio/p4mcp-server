"""
Impersonation policy reason codes and enforcement helpers.

Reason codes are machine-readable strings returned in structured error
responses when an impersonation policy check fails.
"""


class ImpersonationReasonCode:
    """Standardized reason codes for impersonation policy denials."""

    IMPERSONATION_DISABLED = "IMPERSONATION_DISABLED"
    AS_USER_REQUIRED = "AS_USER_REQUIRED"
    AS_USER_INVALID = "AS_USER_INVALID"
    AS_USER_MISMATCH = "AS_USER_MISMATCH"


class ImpersonationPolicyError(Exception):
    """Raised when an impersonation policy check fails.

    Carries a machine-readable ``reason_code`` alongside a human-readable
    message so callers can programmatically react to the denial.
    """

    def __init__(self, message: str, reason_code: str):
        super().__init__(message)
        self.reason_code = reason_code
