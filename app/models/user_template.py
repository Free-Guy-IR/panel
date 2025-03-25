from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserTemplate(BaseModel):
    name: str | None = None
    data_limit: int | None = Field(ge=0, default=None, description="data_limit can be 0 or greater")
    expire_duration: int | None = Field(
        ge=0, default=None, description="expire_duration can be 0 or greater in seconds"
    )
    username_prefix: str | None = Field(max_length=20, min_length=1, default=None)
    username_suffix: str | None = Field(max_length=20, min_length=1, default=None)
    group_ids: list[int] = []


class UserTemplateCreate(UserTemplate):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "my template 1",
                "username_prefix": None,
                "username_suffix": None,
                "group_ids": [1, 3, 5],
                "data_limit": 0,
                "expire_duration": 0,
            }
        }
    )

    @field_validator("group_ids", mode="after")
    @classmethod
    def group_ids_validator(cls, v):
        if not v or len(v) < 1:
            raise ValueError("you must select at least one group")
        return v


class UserTemplateModify(UserTemplate):
    group_ids: list[int] | None = None
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "my template 1",
                "username_prefix": None,
                "username_suffix": None,
                "group_ids": [1, 3, 5],
                "data_limit": 0,
                "expire_duration": 0,
            }
        }
    )


class UserTemplateResponse(UserTemplate):
    id: int

    model_config = ConfigDict(from_attributes=True)
