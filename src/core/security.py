"""
Auth primitives: JWT issue/verify, password hashing (argon2).

Tách 'security' khỏi route layer để dùng được trong worker / scripts khi cần.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import BaseModel

from .settings import get_settings

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False


class TokenPayload(BaseModel):
    sub: str  # user_id
    tid: str  # tenant_id
    typ: Literal["access", "refresh"]
    jti: str  # token id (cho revoke)
    exp: int
    iat: int


def _build(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    typ: Literal["access", "refresh"],
    ttl: timedelta,
) -> tuple[str, TokenPayload]:
    s = get_settings()
    now = datetime.now(UTC)
    payload = TokenPayload(
        sub=str(user_id),
        tid=str(tenant_id),
        typ=typ,
        jti=str(uuid.uuid4()),
        iat=int(now.timestamp()),
        exp=int((now + ttl).timestamp()),
    )
    token = jwt.encode(
        payload.model_dump(),
        s.app.secret_key.get_secret_value(),
        algorithm=s.security.jwt_alg,
    )
    return token, payload


def issue_access(user_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[str, TokenPayload]:
    s = get_settings().security
    return _build(user_id, tenant_id, "access", timedelta(minutes=s.jwt_access_ttl_min))


def issue_refresh(user_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[str, TokenPayload]:
    s = get_settings().security
    return _build(user_id, tenant_id, "refresh", timedelta(days=s.jwt_refresh_ttl_days))


def decode_token(token: str) -> TokenPayload:
    s = get_settings()
    data: dict[str, Any] = jwt.decode(
        token,
        s.app.secret_key.get_secret_value(),
        algorithms=[s.security.jwt_alg],
    )
    return TokenPayload(**data)
