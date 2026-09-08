from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


CASE_SCHEMA_VERSION = "1.0"
CASE_WORKFLOW = "brats_brain_tumour"


class CaseStatus(str, Enum):
    CREATED = "created"
    UPLOADING = "uploading"
    VALIDATING = "validating"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    INVALID = "invalid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


ALLOWED_CASE_STATUS_TRANSITIONS: Mapping[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.CREATED: frozenset({CaseStatus.UPLOADING, CaseStatus.CANCELLED}),
    CaseStatus.UPLOADING: frozenset(
        {CaseStatus.VALIDATING, CaseStatus.INVALID, CaseStatus.FAILED, CaseStatus.CANCELLED}
    ),
    CaseStatus.VALIDATING: frozenset(
        {CaseStatus.UPLOADED, CaseStatus.INVALID, CaseStatus.FAILED, CaseStatus.CANCELLED}
    ),
    CaseStatus.INVALID: frozenset({CaseStatus.UPLOADING, CaseStatus.CANCELLED}),
    CaseStatus.UPLOADED: frozenset(
        {CaseStatus.QUEUED, CaseStatus.UPLOADING, CaseStatus.CANCELLED}
    ),
    CaseStatus.QUEUED: frozenset(
        {CaseStatus.RUNNING, CaseStatus.FAILED, CaseStatus.CANCELLED, CaseStatus.TIMED_OUT}
    ),
    CaseStatus.RUNNING: frozenset(
        {CaseStatus.COMPLETED, CaseStatus.FAILED, CaseStatus.CANCELLED, CaseStatus.TIMED_OUT}
    ),
    CaseStatus.COMPLETED: frozenset({CaseStatus.QUEUED}),
    CaseStatus.FAILED: frozenset(
        {CaseStatus.QUEUED, CaseStatus.UPLOADING, CaseStatus.CANCELLED}
    ),
    CaseStatus.CANCELLED: frozenset({CaseStatus.UPLOADING}),
    CaseStatus.TIMED_OUT: frozenset({CaseStatus.QUEUED, CaseStatus.UPLOADING, CaseStatus.CANCELLED}),
}


class ModalityDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Literal["flair", "t1", "t1ce", "t2"]
    display_name: str
    channel_index: int = Field(ge=0, le=3)
    nnunet_suffix: str = Field(pattern=r"^000[0-3]$")


MRI_MODALITIES: Mapping[str, ModalityDefinition] = {
    "flair": ModalityDefinition(
        key="flair",
        display_name="FLAIR",
        channel_index=0,
        nnunet_suffix="0000",
    ),
    "t1": ModalityDefinition(
        key="t1",
        display_name="T1",
        channel_index=1,
        nnunet_suffix="0001",
    ),
    "t1ce": ModalityDefinition(
        key="t1ce",
        display_name="T1CE",
        channel_index=2,
        nnunet_suffix="0002",
    ),
    "t2": ModalityDefinition(
        key="t2",
        display_name="T2",
        channel_index=3,
        nnunet_suffix="0003",
    ),
}


class CaseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = CASE_SCHEMA_VERSION
    case_id: str
    display_name: Optional[str] = Field(default=None, max_length=80)
    owner_user_id: Optional[int] = None
    workflow: Literal["brats_brain_tumour"] = CASE_WORKFLOW
    required_modalities: List[Literal["flair", "t1", "t1ce", "t2"]]
    created_at: datetime
    updated_at: datetime


class CaseRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)


class UploadPreflightRequest(BaseModel):
    """Sizes only: client filenames are intentionally excluded for privacy."""

    model_config = ConfigDict(extra="forbid")

    modalities: Dict[str, int] = Field(min_length=1, max_length=4)


class NiftiMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shape: Tuple[int, int, int]
    spacing: Tuple[float, float, float]
    orientation: Tuple[str, str, str]
    affine: List[List[float]]
    dtype: str


class ModalityUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modality: Literal["flair", "t1", "t1ce", "t2"]
    channel_index: int = Field(ge=0, le=3)
    nnunet_suffix: str = Field(pattern=r"^000[0-3]$")
    uploaded: bool = False
    # Accepted when reading legacy manifests, but never written back to disk or APIs.
    original_filename: Optional[str] = Field(default=None, exclude=True)
    stored_filename: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    uploaded_at: Optional[datetime] = None
    nifti: Optional[NiftiMetadata] = None


class UploadManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = CASE_SCHEMA_VERSION
    case_id: str
    workflow: Literal["brats_brain_tumour"] = CASE_WORKFLOW
    complete: bool = False
    modalities: Dict[str, ModalityUpload]
    created_at: datetime
    updated_at: datetime


class StatusHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CaseStatus
    message: str
    progress: int = Field(ge=0, le=100)
    timestamp: datetime


class CaseStatusRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = CASE_SCHEMA_VERSION
    case_id: str
    status: CaseStatus
    message: str
    progress: int = Field(ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    history: List[StatusHistoryEntry] = Field(default_factory=list)
