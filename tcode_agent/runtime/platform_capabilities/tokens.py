"""Opaque, in-process, per-Run bridge tokens with revocation and call limits."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from .models import PlatformCapabilitySnapshot


class BridgeAuthorizationError(PermissionError):
    code = "bridge_authorization_failed"


@dataclass(slots=True)
class BridgeTokenRecord:
    token_id: str
    snapshot: PlatformCapabilitySnapshot
    context: Any
    expires_at: float
    remaining_calls: int
    revoked: bool = False
    revoke_reason: str = ""


class BridgeTokenStore:
    def __init__(self) -> None:
        self._records: dict[str, BridgeTokenRecord] = {}
        self._lock = threading.RLock()

    @staticmethod
    def token_id(token: str) -> str:
        return "btok_" + hashlib.sha256(str(token).encode()).hexdigest()[:20]

    def issue(
        self,
        snapshot: PlatformCapabilitySnapshot,
        context: Any,
        *,
        expires_at: float,
        max_calls: int,
    ) -> tuple[str, BridgeTokenRecord]:
        token = "tcode_bridge_" + secrets.token_urlsafe(36)
        record = BridgeTokenRecord(
            token_id=self.token_id(token),
            snapshot=snapshot,
            context=context,
            expires_at=float(expires_at),
            remaining_calls=max(1, int(max_calls)),
        )
        with self._lock:
            self._records[token] = record
        return token, record

    def validate(self, token: str) -> BridgeTokenRecord:
        with self._lock:
            record = self._records.get(str(token or ""))
            if record is None or record.revoked:
                raise BridgeAuthorizationError("Bridge token is invalid or revoked")
            if time.time() >= record.expires_at:
                record.revoked = True
                record.revoke_reason = "expired"
                raise BridgeAuthorizationError("Bridge token has expired")
            return record

    def consume(self, token: str, capability_id: str) -> BridgeTokenRecord:
        with self._lock:
            record = self.validate(token)
            if not record.snapshot.has(capability_id):
                error = BridgeAuthorizationError(
                    f"Capability is not admitted for this Run: {capability_id}"
                )
                error.code = "capability_not_admitted"
                raise error
            if record.remaining_calls <= 0:
                record.revoked = True
                record.revoke_reason = "call_limit_exhausted"
                error = BridgeAuthorizationError("Bridge token call limit is exhausted")
                error.code = "bridge_call_limit_exhausted"
                raise error
            record.remaining_calls -= 1
            return record

    def revoke(self, token: str, reason: str = "run_finished") -> bool:
        with self._lock:
            record = self._records.get(str(token or ""))
            if record is None:
                return False
            record.revoked = True
            record.revoke_reason = str(reason or "revoked")[:200]
            return True


__all__ = [
    "BridgeAuthorizationError",
    "BridgeTokenRecord",
    "BridgeTokenStore",
]
