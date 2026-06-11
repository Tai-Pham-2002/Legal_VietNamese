"""Auth: register / login / refresh / logout."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status

from src.core.security import (
    decode_token,
    hash_password,
    issue_access,
    issue_refresh,
    verify_password,
)
from src.core.settings import get_settings
from src.db.repositories import UserRepo

from ..deps import SessionDep
from ..schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)

router = APIRouter()


def _token_pair(user_id: uuid.UUID, tenant_id: uuid.UUID, repo: UserRepo,
                user_agent: str | None = None, ip: str | None = None) -> tuple[TokenResponse, str]:
    s = get_settings().security
    access, ap = issue_access(user_id, tenant_id)
    refresh, rp = issue_refresh(user_id, tenant_id)
    return (
        TokenResponse(
            access_token=access,
            refresh_token=refresh,
            expires_in=s.jwt_access_ttl_min * 60,
        ),
        refresh,
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(req: RegisterRequest, session: SessionDep) -> UserOut:
    repo = UserRepo(session)
    tenant = await repo.get_or_create_tenant(slug=req.tenant_slug, name=req.tenant_slug)

    existing = await repo.by_email(tenant.id, req.email)
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = await repo.create(
        tenant_id=tenant.id,
        email=req.email,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
    )
    await session.commit()
    return UserOut(
        id=user.id, email=user.email, display_name=user.display_name, tenant_id=tenant.id
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request, session: SessionDep) -> TokenResponse:
    repo = UserRepo(session)
    tenant = await repo.get_or_create_tenant(slug=req.tenant_slug, name=req.tenant_slug)
    user = await repo.by_email(tenant.id, req.email)
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "inactive user")

    tokens, refresh = _token_pair(user.id, tenant.id, repo)
    expires_at = datetime.fromtimestamp(
        datetime.now(UTC).timestamp() + get_settings().security.jwt_refresh_ttl_days * 86400,
        tz=UTC,
    )
    await repo.save_refresh(
        user_id=user.id,
        token=refresh,
        expires_at=expires_at,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    await repo.touch_login(user.id)
    await session.commit()
    return tokens


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest, session: SessionDep) -> TokenResponse:
    try:
        payload = decode_token(req.refresh_token)
    except Exception as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from e
    if payload.typ != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")

    repo = UserRepo(session)
    rt = await repo.get_refresh(req.refresh_token)
    if rt is None or rt.revoked_at is not None or rt.expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh revoked or expired")

    # rotate
    await repo.revoke_refresh(req.refresh_token)
    user_id = uuid.UUID(payload.sub)
    tenant_id = uuid.UUID(payload.tid)
    tokens, new_refresh = _token_pair(user_id, tenant_id, repo)
    expires_at = datetime.fromtimestamp(
        datetime.now(UTC).timestamp() + get_settings().security.jwt_refresh_ttl_days * 86400,
        tz=UTC,
    )
    await repo.save_refresh(user_id=user_id, token=new_refresh, expires_at=expires_at)
    await session.commit()
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(req: RefreshRequest, session: SessionDep) -> None:
    repo = UserRepo(session)
    await repo.revoke_refresh(req.refresh_token)
    await session.commit()
