from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    """Caller-facing error codes. Generic on purpose: no caller-specific semantics."""

    INVALID_PARAMS = "invalid_params"
    MODEL_NOT_FOUND = "model_not_found"
    CONTENT_FILTERED = "content_filtered"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_ERROR = "upstream_error"
    NO_IMAGE_RETURNED = "no_image_returned"
    CONFIG_MISSING = "config_missing"
    GENERATION_IN_PROGRESS = "generation_in_progress"
    INTERNAL_ERROR = "internal_error"
    USAGE_UNAVAILABLE = "usage_unavailable"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    ADMISSION_UNAVAILABLE = "admission_unavailable"
    QUOTA_EXCEEDED = "quota_exceeded"
    SOURCE_IMAGE_UNAVAILABLE = "source_image_unavailable"
    IMAGE_NOT_FOUND = "image_not_found"
    IMAGE_TOO_LARGE = "image_too_large"
    UNSUPPORTED_IMAGE = "unsupported_image"


@dataclass(frozen=True)
class ErrorSpec:
    http_status: int
    retryable: bool  # whether an identical retry may succeed
    message: str  # fixed caller-facing text; upstream wording only goes to logs


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.UNAUTHORIZED: ErrorSpec(401, False, "A product service key is required."),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, True, "Generation rate or concurrency limit exceeded."),
    ErrorCode.ADMISSION_UNAVAILABLE: ErrorSpec(503, True, "Generation admission control is temporarily unavailable."),
    ErrorCode.INVALID_PARAMS: ErrorSpec(422, False, "Request parameters are invalid."),
    ErrorCode.MODEL_NOT_FOUND: ErrorSpec(400, False, "Model not found. Use GET /v1/models for available models."),
    ErrorCode.CONTENT_FILTERED: ErrorSpec(
        422,
        False,
        "The request was blocked by the upstream content safety filter. Rephrasing to evade the filter will not help.",
    ),
    ErrorCode.UPSTREAM_TIMEOUT: ErrorSpec(504, True, "The upstream model timed out. Please retry."),
    ErrorCode.UPSTREAM_UNAVAILABLE: ErrorSpec(
        502, True, "The upstream model is temporarily unavailable. Please retry."
    ),
    ErrorCode.UPSTREAM_ERROR: ErrorSpec(502, False, "The upstream model rejected the request."),
    ErrorCode.NO_IMAGE_RETURNED: ErrorSpec(502, True, "The upstream model returned no image."),
    ErrorCode.CONFIG_MISSING: ErrorSpec(503, False, "The service is missing configuration for this model."),
    ErrorCode.GENERATION_IN_PROGRESS: ErrorSpec(
        503, True, "An identical request is still being generated. Please retry shortly."
    ),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, False, "Internal error while generating the image."),
    ErrorCode.USAGE_UNAVAILABLE: ErrorSpec(503, False, "Generation accounting is temporarily unavailable."),
    ErrorCode.QUOTA_EXCEEDED: ErrorSpec(
        429, False, "The daily quota for this model's provider is used up; it resets at 00:00 Asia/Shanghai."
    ),
    # /v2/mcp 改图：source_image_id 不是本产品的图（不存在、删了、别的产品的、格式不对都一样，不透露是哪种）。
    ErrorCode.SOURCE_IMAGE_UNAVAILABLE: ErrorSpec(
        404, False, "The image to edit was not found among this product's images."
    ),
    ErrorCode.IMAGE_NOT_FOUND: ErrorSpec(404, False, "Image not found."),
    # POST /v1/images 上传：超过大小上限、不是认得出宽高的 PNG / JPEG / WebP
    ErrorCode.IMAGE_TOO_LARGE: ErrorSpec(413, False, "The image is larger than 4 MB."),
    ErrorCode.UNSUPPORTED_IMAGE: ErrorSpec(
        415, False, "The image must be a PNG, JPEG or WebP file with a readable size."
    ),
}


def validation_error_fields(errors: list[dict]) -> list[str]:
    """Field names from pydantic/FastAPI validation errors, in order, de-duplicated.

    FastAPI prefixes body locations with ``"body"``; plain pydantic errors don't.
    """
    fields: list[str] = []
    for err in errors:
        loc = [part for part in err.get("loc", ()) if part != "body"]
        name = str(loc[0]) if loc else "body"
        if name not in fields:
            fields.append(name)
    return fields
