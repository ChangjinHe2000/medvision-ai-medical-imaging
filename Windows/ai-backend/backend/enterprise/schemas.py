from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .privacy import contains_sensitive_text


class InitialAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "doctor", "viewer"] = "doctor"
    organization_id: str | None = Field(default=None, max_length=64)


class UserStatusRequest(BaseModel):
    active: bool


class CreateCaseRequest(BaseModel):
    case_type: Literal["brain_mri", "ppgl_ct"]
    display_name: str = Field(default="未命名病例", min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def reject_patient_identifiers(cls, value: str) -> str:
        if contains_sensitive_text(value):
            raise ValueError("病例显示名不能包含姓名、手机号、证件号或病历号")
        return value


class RenameCaseRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("display_name")
    @classmethod
    def reject_patient_identifiers(cls, value: str) -> str:
        if contains_sensitive_text(value):
            raise ValueError("病例显示名不能包含敏感身份信息")
        return value


class CaseGrantRequest(BaseModel):
    user_id: str
    permission: Literal["read", "edit", "manage"]


class TrashCaseRequest(BaseModel):
    reason_code: Literal["user_request", "duplicate", "test_cleanup", "retention_expired", "other"] = "user_request"


class PurgeCaseRequest(BaseModel):
    reason_code: Literal["approved_purge", "retention_expired"]
    confirmation: str


class RetentionRequest(BaseModel):
    profile: Literal["unclassified", "development_test", "research", "clinical_outpatient", "clinical_inpatient"]
    retain_until: datetime | None = None


class LegalHoldRequest(BaseModel):
    active: bool
    reason: str | None = Field(default=None, max_length=80)


class SendMessageRequest(BaseModel):
    recipient_id: str
    content: str = Field(min_length=1, max_length=2000)
