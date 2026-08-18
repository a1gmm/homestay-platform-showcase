from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional
import re


class SendOtpRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)

    def validate_phone(self) -> None:
        if not re.fullmatch(r"1[3-9]\d{9}", self.phone):
            raise ValueError("手机号格式不正确")


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str = Field(..., min_length=4, max_length=6)
    name: Optional[str] = None


class CustomerToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    customer: "CustomerOut"


class CustomerOut(BaseModel):
    phone: str
    name: Optional[str] = None
    id_number: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    id_number: Optional[str] = None


CustomerToken.model_rebuild()
