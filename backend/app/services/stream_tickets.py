from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.schemas.streaming import StreamScope, StreamTicketClaims


STREAM_TICKET_KEY_PREFIX = "stream_ticket:"
MAX_STREAM_TICKET_TTL_SECONDS = 60
INVALID_STREAM_TICKET_DETAIL = "Invalid or expired stream ticket"
STREAM_TICKET_GETDEL_FALLBACK = (
    "local value = redis.call('GET', KEYS[1]); "
    "if value then redis.call('DEL', KEYS[1]); end; "
    "return value"
)


class StreamTicketError(ValueError):
    """Raised when a single-use stream ticket cannot be consumed."""


class StreamTicketService:
    def __init__(
        self,
        redis: Redis,
        *,
        max_ttl_seconds: int = MAX_STREAM_TICKET_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        requested_max_ttl = int(max_ttl_seconds)
        if requested_max_ttl <= 0:
            raise ValueError("max_ttl_seconds must be positive")
        self.redis = redis
        self.max_ttl_seconds = min(requested_max_ttl, MAX_STREAM_TICKET_TTL_SECONDS)
        self._clock = clock or time.time

    async def issue(self, claims: StreamTicketClaims, ttl_seconds: int = MAX_STREAM_TICKET_TTL_SECONDS) -> str:
        ttl = min(int(ttl_seconds), self.max_ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")

        raw_ticket = secrets.token_urlsafe(32)
        issued_at = int(self._clock())
        payload = {
            "scope": claims.scope.value,
            "principal_user_id": str(claims.principal_user_id),
            "patient_id": str(claims.patient_id),
            "care_episode_id": str(claims.care_episode_id) if claims.care_episode_id else None,
            "issued_at": issued_at,
            "expires_at": issued_at + ttl,
        }
        key = self._key(raw_ticket)
        stored = await self.redis.set(key, json.dumps(payload, separators=(",", ":")), ex=ttl, nx=True)
        if not stored:
            raise RuntimeError("Could not issue stream ticket")
        return raw_ticket

    async def consume(self, raw_ticket: str, expected_scope: StreamScope) -> StreamTicketClaims:
        if not isinstance(raw_ticket, str) or not raw_ticket:
            raise StreamTicketError(INVALID_STREAM_TICKET_DETAIL)

        key = self._key(raw_ticket)
        try:
            payload = await self.redis.getdel(key)
        except ResponseError as exc:
            detail = str(exc).lower()
            if "unknown command" not in detail or "getdel" not in detail:
                raise StreamTicketError(INVALID_STREAM_TICKET_DETAIL) from exc
            try:
                payload = await self.redis.eval(STREAM_TICKET_GETDEL_FALLBACK, 1, key)
            except Exception as fallback_exc:
                raise StreamTicketError(INVALID_STREAM_TICKET_DETAIL) from fallback_exc
        except Exception as exc:
            raise StreamTicketError(INVALID_STREAM_TICKET_DETAIL) from exc

        if not payload:
            raise StreamTicketError(INVALID_STREAM_TICKET_DETAIL)

        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            data = json.loads(payload)
            scope = StreamScope(data["scope"])
            principal_user_id = UUID(str(data["principal_user_id"]))
            patient_id = UUID(str(data["patient_id"]))
            care_episode_raw = data["care_episode_id"]
            care_episode_id = UUID(str(care_episode_raw)) if care_episode_raw else None
            issued_at = int(data["issued_at"])
            expires_at = int(data["expires_at"])
            if expires_at <= issued_at or expires_at <= int(self._clock()):
                raise ValueError("expired")
            if scope != StreamScope(expected_scope):
                raise ValueError("wrong scope")
            if scope is StreamScope.PATIENT_REHAB:
                if care_episode_id is not None:
                    raise ValueError("malformed patient ticket")
                if principal_user_id != patient_id:
                    raise ValueError("contradictory patient ticket")
            if scope is StreamScope.DOCTOR_EPISODE_MONITOR and care_episode_id is None:
                raise ValueError("malformed doctor ticket")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StreamTicketError(INVALID_STREAM_TICKET_DETAIL) from exc

        return StreamTicketClaims(
            scope=scope,
            principal_user_id=principal_user_id,
            patient_id=patient_id,
            care_episode_id=care_episode_id,
        )

    @staticmethod
    def _key(raw_ticket: str) -> str:
        digest = hashlib.sha256(raw_ticket.encode("utf-8")).hexdigest()
        return f"{STREAM_TICKET_KEY_PREFIX}{digest}"
