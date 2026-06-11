"""User + RefreshToken repository."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import RefreshToken, Tenant, User


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ----- tenants --------------------------------------------------------
    async def get_or_create_tenant(self, slug: str, name: str) -> Tenant:
        q = select(Tenant).where(Tenant.slug == slug)
        t = (await self.s.execute(q)).scalar_one_or_none()
        if t:
            return t
        t = Tenant(name=name, slug=slug)
        self.s.add(t)
        await self.s.flush()
        return t

    # ----- users ----------------------------------------------------------
    async def by_email(self, tenant_id: uuid.UUID, email: str) -> User | None:
        q = select(User).where(User.tenant_id == tenant_id, User.email == email.lower())
        return (await self.s.execute(q)).scalar_one_or_none()

    async def by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.s.get(User, user_id)

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        email: str,
        password_hash: str,
        display_name: str | None = None,
    ) -> User:
        u = User(
            tenant_id=tenant_id,
            email=email.lower(),
            password_hash=password_hash,
            display_name=display_name,
        )
        self.s.add(u)
        await self.s.flush()
        return u

    async def touch_login(self, user_id: uuid.UUID) -> None:
        u = await self.s.get(User, user_id)
        if u:
            u.last_login_at = datetime.now(UTC)

    # ----- refresh tokens -------------------------------------------------
    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def save_refresh(
        self,
        *,
        user_id: uuid.UUID,
        token: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        rt = RefreshToken(
            user_id=user_id,
            token_hash=self._hash_token(token),
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self.s.add(rt)
        await self.s.flush()
        return rt

    async def get_refresh(self, token: str) -> RefreshToken | None:
        q = select(RefreshToken).where(RefreshToken.token_hash == self._hash_token(token))
        return (await self.s.execute(q)).scalar_one_or_none()

    async def revoke_refresh(self, token: str) -> None:
        rt = await self.get_refresh(token)
        if rt and rt.revoked_at is None:
            rt.revoked_at = datetime.now(UTC)
