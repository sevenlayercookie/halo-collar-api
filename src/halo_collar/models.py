"""Small stable models for authentication and instant corrections."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class CorrectionType(str, Enum):
    """Correction names observed in the official iOS client's REST traffic."""

    WARNING = "Warning"
    FIRST_TIME = "FirstTime"
    ESCALATION = "Escalation"
    RETURN_WHISTLE = "ReturnWhistle"
    GOOD_BEHAVIOR = "GoodBehavior"
    HEADING_HOME = "HeadingHome"

    @property
    def expiration_seconds(self) -> int:
        if self in {
            CorrectionType.RETURN_WHISTLE,
            CorrectionType.GOOD_BEHAVIOR,
            CorrectionType.HEADING_HOME,
        }:
            return 7
        return 4

    @classmethod
    def parse(cls, value: str | CorrectionType) -> CorrectionType:
        if isinstance(value, cls):
            return value
        normalized = value.replace("-", "").replace("_", "").casefold()
        for item in cls:
            if item.value.casefold() == normalized:
                return item
        choices = ", ".join(item.value for item in cls)
        raise ValueError(f"Unknown correction type {value!r}. Choose one of: {choices}")


@dataclass(slots=True)
class TokenSet:
    """OAuth tokens plus the local expiry calculated from ``expires_in``."""

    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"
    scope: str = ""

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(timezone.utc).timestamp()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=str(value["access_token"]),
            refresh_token=str(value["refresh_token"]),
            expires_at=float(value["expires_at"]),
            token_type=str(value.get("token_type", "Bearer")),
            scope=str(value.get("scope", "")),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationFlow:
    """Ephemeral values required to validate and complete a PKCE login."""

    url: str
    code_verifier: str
    state: str
    nonce: str
