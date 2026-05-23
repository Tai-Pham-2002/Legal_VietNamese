"""Auth schemas."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=255)
    tenant_slug: str = Field(default="default", min_length=1, max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_slug: str = "default"


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int  # seconds


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    tenant_id: uuid.UUID
