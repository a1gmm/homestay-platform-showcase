"""运维问答 AI 助手端点 —— admin-only（老板/王总）。

安全内核在 services/assistant/：意图分类(json_object) → 白名单只读工具 → AI 解读 → 亮底稿 + 审计。
本端点只做：限角色（护栏③）、接住 LLM 降级转 503、把结果吐给前端。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.deps import DBSession, require_role
from app.schemas.assistant import AskRequest
from app.services.assistant.intent import AssistantLLMError
from app.services.assistant.service import answer_question

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/assistant", tags=["assistant"])


@router.post("/ask")
async def ask(
    payload: AskRequest,
    db: DBSession,
    current_user=Depends(require_role("admin")),
):
    """老板问一句话 → 追溯/查数结果 + 底稿。意图分类 LLM 不可用 → 503。"""
    try:
        history = [message.model_dump() for message in payload.history]
        return await answer_question(db, payload.question, current_user, history=history)
    except AssistantLLMError as e:
        logger.warning("assistant intent LLM unavailable: %s", e)
        raise HTTPException(status_code=503, detail="AI 服务暂时不可用，请稍后重试")
