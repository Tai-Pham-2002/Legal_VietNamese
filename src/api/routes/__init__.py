"""HTTP routes."""

from fastapi import APIRouter

from . import auth, chat, conversations, files, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/v1/auth", tags=["auth"])
api_router.include_router(conversations.router, prefix="/v1/conversations", tags=["conversations"])
api_router.include_router(chat.router, prefix="/v1/chat", tags=["chat"])
api_router.include_router(files.router, prefix="/v1/files", tags=["files"])

__all__ = ["api_router"]
