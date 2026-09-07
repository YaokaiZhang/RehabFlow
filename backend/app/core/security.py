from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import get_settings

bearer_scheme = HTTPBearer(auto_error=False)
PASSWORD_HASH_PREFIX = "bcrypt_sha256$"


@dataclass(frozen=True)
class Principal(dict[str, object]):
	"""Typed identity with complete compatibility for legacy mapping callers."""

	role: Literal["patient", "doctor"]
	user_id: UUID
	subject: str | None = None

	def __post_init__(self) -> None:
		dict.__init__(
			self,
			role=self.role,
			user_id=self.user_id,
			sub=self.subject,
		)


def _password_digest(password: str) -> bytes:
	return hashlib.sha256(password.encode("utf-8")).hexdigest().encode("ascii")


def hash_password(password: str) -> str:
	"""Hash a password without bcrypt 72-byte input limit."""
	hashed = bcrypt.hashpw(_password_digest(password), bcrypt.gensalt()).decode("ascii")
	return f"{PASSWORD_HASH_PREFIX}{hashed}"


def verify_password(plain_password: str, password_hash: str) -> bool:
	try:
		if password_hash.startswith(PASSWORD_HASH_PREFIX):
			expected = password_hash.removeprefix(PASSWORD_HASH_PREFIX).encode("ascii")
			return hmac.compare_digest(bcrypt.hashpw(_password_digest(plain_password), expected), expected)

		plain_bytes = plain_password.encode("utf-8")
		if len(plain_bytes) > 72:
			return False
		expected = password_hash.encode("ascii")
		return hmac.compare_digest(bcrypt.hashpw(plain_bytes, expected), expected)
	except (TypeError, ValueError, bcrypt.Error):
		return False


def create_access_token(subject: str, role: Literal["patient", "doctor"], user_id: str) -> str:
	settings = get_settings()
	now = datetime.now(timezone.utc)
	expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
	payload = {
		"sub": subject,
		"role": role,
		"user_id": user_id,
		"iat": int(now.timestamp()),
		"exp": int(expire.timestamp()),
	}
	return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
	settings = get_settings()
	try:
		return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
	except JWTError as exc:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc


def get_current_principal(
	credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
	if credentials is None or not credentials.credentials:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

	claims = decode_token(credentials.credentials)
	try:
		role = claims["role"]
		if role not in ("patient", "doctor"):
			raise ValueError("invalid role")
		return Principal(
			subject=str(claims["sub"]),
			role=role,
			user_id=UUID(str(claims["user_id"])),
		)
	except (KeyError, TypeError, ValueError) as exc:
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims") from exc
