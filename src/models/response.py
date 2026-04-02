from __future__ import annotations

from pydantic import BaseModel


class GenerateResponse(BaseModel):
    id: str
    image_url: str
    model: str
    cached: bool
    created_at: str


class ModelInfoResponse(BaseModel):
    id: str
    name: str
    provider: str


class ModelsListResponse(BaseModel):
    models: list[ModelInfoResponse]


class HealthCheck(BaseModel):
    name: str
    status: str


class HealthResponse(BaseModel):
    status: str  # "healthy", "degraded", "unhealthy"
    version: str
    checks: dict[str, str]


class ErrorResponse(BaseModel):
    error: str
    message: str
    retry_after: int | None = None
