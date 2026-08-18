from pydantic import BaseModel, field_validator
from app.models.user import UserRole


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    role: str
    display_name: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserCreate(BaseModel):
    username: str
    display_name: str
    password: str
    role: UserRole = UserRole.operator
    phone: str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码长度不能少于8位")
        if v.isdigit() or v.isalpha():
            raise ValueError("密码必须包含字母和数字")
        return v


class UserOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: UserRole
    is_active: bool
    phone: str | None

    model_config = {"from_attributes": True}
