"""Typed errors with stable error codes for CLI contracts."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SubagentError(Exception):
    """Error mapped to the versioned CLI error envelope."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return payload
