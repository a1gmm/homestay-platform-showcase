"""运维问答助手的请求/响应结构。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=500)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, description="老板的大白话问题")
    history: list[ConversationMessage] = Field(default_factory=list, max_length=6)
