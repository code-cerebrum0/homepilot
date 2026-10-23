from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.ai.chat import OllamaUnavailable, run_chat, stream_chat
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user, membership_or_404
from app.errors import AppError
from app.models import User
from app.rate_limit import chat_limiter
from app.schemas import ChatIn, ChatOut

router = APIRouter(tags=["assistant"])


@router.post("/households/{household_id}/chat", response_model=ChatOut)
def chat(
    household_id: int,
    body: ChatIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership_or_404(db, user, household_id)
    if not chat_limiter.allow(f"chat:{user.id}", get_settings().chat_rate_limit_per_minute):
        raise AppError(429, "rate_limited", "Too many messages. Please slow down.")
    try:
        out = run_chat(db, household_id, user.id, body.message, body.conversation_id)
        response.headers["X-MCP-Calls"] = str(len(out.mcp_calls))
        return out
    except OllamaUnavailable:
        raise AppError(503, "server_error", "The AI assistant is currently unavailable")


@router.post("/households/{household_id}/chat/stream")
def chat_stream(
    household_id: int,
    body: ChatIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Server-Sent Events endpoint for streaming chat responses.

    SSE event types:
      tool   {"name": str, "status": "running"|"success"|"error", "summary"?: str}
      token  {"text": str}
      done   {"conversation_id": str, "tool_calls": [...]}
      error  {"message": str}
    """
    membership_or_404(db, user, household_id)
    if not chat_limiter.allow(f"chat:{user.id}", get_settings().chat_rate_limit_per_minute):
        raise AppError(429, "rate_limited", "Too many messages. Please slow down.")

    generator = stream_chat(db, household_id, user.id, body.message, body.conversation_id)

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
