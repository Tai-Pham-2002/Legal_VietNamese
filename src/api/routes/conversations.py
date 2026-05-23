"""Conversation CRUD + message history."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from Legal_VietNamese.src.db.repositories import ConversationRepo

from ..deps import CurrentUserDep, SessionDep
from ..schemas import (
    ConversationCreate,
    ConversationOut,
    MessageOut,
)

router = APIRouter()


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    req: ConversationCreate, current: CurrentUserDep, session: SessionDep
) -> ConversationOut:
    user_id, tenant_id = current
    repo = ConversationRepo(session)
    c = await repo.create(
        tenant_id=tenant_id,
        user_id=user_id,
        title=req.title or "New conversation",
    )
    await session.commit()
    return ConversationOut(
        id=c.id,
        title=c.title,
        message_count=c.message_count,
        last_message_at=c.last_message_at,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    current: CurrentUserDep,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ConversationOut]:
    user_id, _ = current
    repo = ConversationRepo(session)
    items = await repo.list_for_user(user_id=user_id, limit=limit, offset=offset)
    return [
        ConversationOut(
            id=c.id,
            title=c.title,
            message_count=c.message_count,
            last_message_at=c.last_message_at,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in items
    ]


@router.get("/{conv_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conv_id: uuid.UUID, current: CurrentUserDep, session: SessionDep
) -> list[MessageOut]:
    user_id, _ = current
    repo = ConversationRepo(session)
    conv = await repo.get(conv_id, user_id=user_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    msgs = await repo.messages(conversation_id=conv_id, limit=200)
    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            meta=m.meta,
            tokens_in=m.tokens_in,
            tokens_out=m.tokens_out,
            created_at=m.created_at,
        )
        for m in msgs
    ]


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_conversation(
    conv_id: uuid.UUID, current: CurrentUserDep, session: SessionDep
) -> None:
    user_id, _ = current
    await ConversationRepo(session).archive(conv_id, user_id)
    await session.commit()
