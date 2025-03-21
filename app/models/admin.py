from typing import Optional

from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, field_validator


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class Admin(BaseModel):
    username: str
    is_sudo: bool
    telegram_id: int | None = None
    discord_webhook: str | None = None
    users_usage: int = 0
    is_disabled: bool = False
    sub_template: str | None = None
    sub_domain: str | None = None
    profile_title: str | None = None
    support_url: str | None = None
    users_usage: int | None = None
    lifetime_used_traffic: int | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("users_usage", mode="before")
    def cast_to_int(cls, v):
        if v is None:  # Allow None values
            return v
        if isinstance(v, float):  # Allow float to int conversion
            return int(v)
        if isinstance(v, int):  # Allow integers directly
            return v
        raise ValueError("must be an integer or a float, not a string")  # Reject strings


class AdminModify(BaseModel):
    password: str | None = None
    is_sudo: bool
    telegram_id: int | None = None
    discord_webhook: str | None = None
    is_disabled: bool | None = None
    sub_template: str | None = None
    sub_domain: str | None = None
    profile_title: str | None = None
    support_url: str | None = None

    @property
    def hashed_password(self):
        if self.password:
            return pwd_context.hash(self.password)

    @field_validator("discord_webhook")
    @classmethod
    def validate_discord_webhook(cls, value):
        if value and not value.startswith("https://discord.com"):
            raise ValueError("Discord webhook must start with 'https://discord.com'")
        return value


class AdminCreate(AdminModify):
    username: str
    password: str


class AdminPartialModify(AdminModify):
    __annotations__ = {k: Optional[v] for k, v in AdminModify.__annotations__.items()}


class AdminInDB(Admin):
    hashed_password: str

    def verify_password(self, plain_password):
        return pwd_context.verify(plain_password, self.hashed_password)


class AdminValidationResult(BaseModel):
    username: str
    is_sudo: bool
    is_disabled: bool
