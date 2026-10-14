"""Pydantic models shared by OCR, BullMQ jobs, and validation."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DocumentType = Literal["national_id", "passport", "driving_license", "unknown"]


class OCRDetection(BaseModel):
    """One text detection returned by PaddleOCR."""

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: list[list[float]] = Field(default_factory=list)


class IdentityFields(BaseModel):
    """Identity fields extracted from an Algerian identity document."""

    document_type: DocumentType = "unknown"
    document_type_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    first_name: str | None = None
    last_name: str | None = None
    first_name_ar: str | None = None
    last_name_ar: str | None = None
    arabic_name: str | None = None
    birth_date: str | None = None
    birth_place: str | None = None
    gender: str | None = None
    nin: str | None = None
    document_number: str | None = None


class OCRResult(BaseModel):
    """Stable result contract stored as a BullMQ job return value."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    fields: IdentityFields = Field(default_factory=IdentityFields)
    raw_text: list[str] = Field(default_factory=list)
    detections: list[OCRDetection] = Field(default_factory=list)
    processing_time_ms: float = Field(default=0.0, ge=0.0)
    error: str | None = None


class ValidationResult(BaseModel):
    """Result of comparing OCR fields with a database record."""

    matched: bool
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


JsonObject = dict[str, Any]
