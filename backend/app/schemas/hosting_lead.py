from typing import Literal

from pydantic import BaseModel, Field, field_validator


class HostingLeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    phone: str = Field(pattern=r"^1[3-9]\d{9}$")
    property_location: str = Field(min_length=1, max_length=200)

    @field_validator("name", "property_location")
    @classmethod
    def _strip_and_reject_blank(cls, v: str) -> str:
        # min_length=1 不挡纯空白（"  " 长度为 2）——strip 后再校验，
        # 否则空白称呼/位置会落库并出现在飞书通知里。
        v = v.strip()
        if not v:
            raise ValueError("不能为空白字符")
        return v


class HostingLeadOut(BaseModel):
    # already_registered 时 lead_id 为空字符串（最小暴露：不向匿名调用者
    # 回显已有记录的持久标识符）。
    lead_id: str
    status: Literal["ok", "already_registered"]
