"""Pydantic schemas for API request/response models."""
from typing import Optional
from pydantic import BaseModel, Field


class AddKaekRequest(BaseModel):
    kaek: str = Field(..., description="Single KAEK value, e.g. 123456789/01/0/0")


class ImportKaeksRequest(BaseModel):
    kaeks: list[str] = Field(..., description="List of KAEK strings")
    text: Optional[str] = Field(None, description="Free-form text to parse for KAEKs")


class GenerateTestRequest(BaseModel):
    count: int = Field(5, ge=1, le=50)
    prefix: str = Field("", description="Optional prefix for test KAEKs")


class ProcessRequest(BaseModel):
    kaeks: Optional[list[str]] = Field(None, description="Specific KAEKs to process; null = all pending")


class RetryRequest(BaseModel):
    kaek: str


class AreaSearchRequest(BaseModel):
    area_name: str = Field("", description="Municipality / area name in Greek")
    map_url: Optional[str] = Field(None, description="Direct map URL to parse")
    kaek_list: Optional[list[str]] = Field(None, description="Pre-supplied KAEK list")
    kaek_text: Optional[str] = Field(None, description="Free-form text containing KAEKs")
    min_area_sqm: Optional[float] = Field(None, ge=0)
    max_area_sqm: Optional[float] = Field(None, ge=0)
    has_building: str = Field("any", pattern="^(yes|no|any)$")
    has_burdens: str = Field("any", pattern="^(yes|no|any)$")
    kaek_pattern: str = Field("", description="KAEK suffix pattern e.g. /0/0")
    download_missing: bool = Field(True, description="Download PDFs for newly discovered KAEKs")
    only_completed: bool = Field(True)


class FilterRequest(BaseModel):
    min_area_sqm: Optional[float] = None
    max_area_sqm: Optional[float] = None
    has_building: str = Field("any", pattern="^(yes|no|any|unknown)$")
    has_burdens: str = Field("any", pattern="^(yes|no|any|unknown)$")
    kaek_pattern: str = ""
    only_completed: bool = True


class ParseRequest(BaseModel):
    kaek: str
    pdf_type: str = Field("perigrafiki", pattern="^(perigrafiki|xoriki)$")


class ConfigUpdateRequest(BaseModel):
    min_delay: Optional[float] = Field(None, ge=0.5)
    max_delay: Optional[float] = Field(None, ge=1.0)
    headless: Optional[bool] = None
    max_retries: Optional[int] = Field(None, ge=0, le=5)


class KaekJobResponse(BaseModel):
    id: int
    kaek: str
    status: str
    retry_count: int
    pdf_perigrafiki_path: Optional[str]
    pdf_xoriki_path: Optional[str]
    pdf_perigrafiki_ok: bool
    pdf_xoriki_ok: bool
    failure_reason: Optional[str]
    created_at: str
    updated_at: str


class DashboardStats(BaseModel):
    total: int
    pending: int
    processing: int
    completed: int
    partial: int
    failed: int
    pdf_perigrafiki_count: int
    pdf_xoriki_count: int


class LogEntry(BaseModel):
    id: int
    kaek: Optional[str]
    step: str
    status: str
    message: Optional[str]
    created_at: str


class ParseResult(BaseModel):
    kaek: str
    pdf_type: str
    area_sqm: Optional[float]
    area_stremma: Optional[float]
    has_building: str
    has_burdens: str
    property_type: Optional[str]
    burdens_detail: Optional[str]
    notes: Optional[str]
    confidence: float
    parse_method: Optional[str]
    pdf_perigrafiki_path: Optional[str]
    pdf_xoriki_path: Optional[str]
    job_status: Optional[str]
