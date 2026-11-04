"""MCP 工具 generate_image（/v2/mcp）：产品经网关 /image/v2/mcp 给自己的模型用。旧的 /mcp 契约不动（Fusion 现在的代码和 Claude Code 在用）。

和旧工具的区别（规则来自 Fusion 和灵稿各自踩过的坑，两边原来各写一遍）：
- 图存在服务里、归调用方产品，只给 id 不给地址；产品后端按 id 经网关取图（GET /v1/images/{id}），删对话时按 id 删；
- 只开放模型该填的参数：不开放 model、size、fallback_model、reference_images；grounding 固定开（Fusion 15725c99），
  regenerate 映射成 no_cache（Fusion 61ac1ed3）；
- 比例的可选值按主模型和默认备用链算，保证主模型失败时一定能换备用（见 generator.tool_aspect_ratios）；
- 改图收服务自己的图片 id：查不到、不是本产品的、格式不对都回 source_image_unavailable，不出图、不计费、不占服务里的限流
  （编造的编号也一样，Fusion 6b566d01）；没填比例时沿用原图形状（Fusion b6a2b37a）；
- 给模型的文字不带地址、不带模型名；失败按错误码给不同说法（Fusion 61ac1ed3、2595e6d8）。

说明（英文，模型看的）写在这里，Fusion 和灵稿共用；产品可以在后面接自己的一段（比如卡片上已经标了模型）。
每轮最多几张、图是不是这段对话里的，由产品管：服务只认到产品。
失败：structuredContent 是 {ok:false, error:{code, message, retryable, ...}}，文字是给模型的说明；isError 保持 false，
和 weather、search、reader 的 MCP 一样。

这个模块不用 `from __future__ import annotations`：比例的 Literal 启动时才算出来，注解要是真的类型。
"""

from contextlib import suppress
from typing import Annotated, Literal

import structlog
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from src import __version__
from src.config import Settings
from src.mcp.server import request_context
from src.models.errors import ERROR_SPECS, ErrorCode, validation_error_fields
from src.models.request import GenerateRequest, ImageQuality
from src.models.response import StoredGenerateResult
from src.services.cache import CacheService
from src.services.generator import (
    GenerationError,
    OwnedTarget,
    closest_aspect_ratio,
    handle_generate,
    tool_aspect_ratios,
)
from src.services.image_store import ImageStore

logger = structlog.get_logger()

TOOL_NAME = "generate_image"
MAX_PROMPT_CHARS = 1000
MAX_STYLE_CHARS = 60
MAX_REFERENCE_SUBJECT_CHARS = 80

owned_mcp = FastMCP(
    "image-service",
    version=__version__,
    instructions="Use generate_image to create an image, or to edit an image it generated earlier by passing that "
    "image's id as source_image_id. Images are stored by the service; fetch them by id over REST.",
)

DESCRIPTION = (
    "Generate a new image from a text description, or edit an image generated earlier with this tool. Call it only "
    "when the user asks you to create, draw, or design an image, such as an illustration, poster, icon, avatar, or "
    "scene, or to change an image generated earlier in this conversation. Do not call it to find existing photos, to "
    "look up facts, or to illustrate an answer the user did not ask to visualize. Each call has a real cost: generate "
    "one image per request unless the user explicitly asks for several."
)
PROMPT_DESCRIPTION = (
    "Detailed visual description of the image: subject, setting, composition, lighting, colors, and any text that "
    "must appear in the image. Write it in the user's language or in English. Name a real product, place, person, or "
    "event exactly as the user did, and do not invent appearance details that the conversation does not establish, "
    "such as camera layout, colors, or design features; set reference_subject when the exact look of a real object "
    "matters. When editing with source_image_id, write the edit instruction instead: what to change, and what to keep "
    "as it is."
)
ASPECT_RATIO_DESCRIPTION = (
    "Image aspect ratio. Use 1:1 unless the user asks for another shape: landscape such as 16:9, 3:2, 4:3, or 21:9 "
    "for an ultra-wide banner; portrait such as 9:16, 2:3, 3:4, or 4:5 for a poster or phone wallpaper. Use the more "
    "extreme ratios only for a long banner or strip the user asks for. When editing with source_image_id, omit it to "
    "keep the original's shape."
)
STYLE_DESCRIPTION = (
    "Optional style hint, such as watercolor, photorealistic, flat illustration, oil painting, or pixel art."
)
REGENERATE_DESCRIPTION = (
    "Set to true only when the user asks for another version of an image already generated in this conversation and "
    "the description stays the same. The service then draws a new image instead of returning the earlier one for an "
    "identical request. Omit it otherwise."
)
REFERENCE_SUBJECT_DESCRIPTION = (
    "Set only when the image must show a specific real-world object whose exact appearance matters, such as a named "
    "product model, vehicle, building, or landmark. Use its exact name as the user wrote it, for example a phone model "
    "name. The service then looks up current reference photos of it so the drawing matches the real object. Omit it "
    "for generic, imaginary, or stylized subjects."
)
QUALITY_DESCRIPTION = (
    "Optional quality tier. Omit it, or use standard, for everyday images, drafts, and edits: the standard model is "
    "fast and cheaper. Use high only when the user asks for a polished, high-quality final image, such as print-ready "
    "artwork or a showcase poster; it costs more and usually takes 30 seconds or longer. Text in the image or a "
    "reference_subject alone does not call for high."
)
SOURCE_IMAGE_ID_DESCRIPTION = (
    "Set only to edit an image generated earlier in this conversation: the image id given in that image's result. Use "
    "it when the user wants to change that image while keeping the rest, such as replacing the background, adding or "
    "removing an object, changing colors, or adjusting the text in it; to edit an edited image again, use the id of "
    "the latest version the user is referring to. Omit it to create a new image, and use regenerate instead for a "
    "fresh redraw of the same description. Omit aspect_ratio to keep the original's shape. Each edit creates a new "
    "image and leaves the original unchanged. Only images generated in this conversation can be edited."
)

NO_IMAGE = " Do not describe the image as if it exists and do not output any image link."


def success_text(result: StoredGenerateResult, reference_subject: str | None, edited: bool) -> str:
    text = (
        f"The image was {'edited: a new image was generated from the original, which is unchanged' if edited else 'generated'}. "
        f"Image id: {result.image.id}. Use it only as source_image_id if the user later asks to change this image; "
        "never show it to the user. Do not output an image link, a markdown image, or the image id."
    )
    status = result.reference_search.status if result.reference_search else None
    if reference_subject and status in ("none_accepted", "search_failed"):
        # Fusion 38f4bd15：没找到参考图时如实说外观可能不同
        text += (
            f" No usable reference photo of {reference_subject} was found, so it was drawn without one; tell the user "
            "plainly that its appearance may differ from the real one."
        )
    return text + (
        f" Reply briefly in the user's language: say {'what was changed' if edited else 'what was drawn'} in one or two "
        "sentences and offer to adjust it."
    )


def failure_text(code: ErrorCode, message: str, fallback_from: dict[str, str] | None) -> str:
    retryable = ERROR_SPECS[code].retryable
    first = (fallback_from or {}).get("code")
    if code == ErrorCode.SOURCE_IMAGE_UNAVAILABLE:
        # Fusion 2595e6d8：回放验证过（旧说法 12 次里 4 次说成改了原图，这段 12 次全部如实）
        return (
            "The image to edit could not be found among the editable images of this conversation: the id may be "
            "wrong. Do not call again with this id or a guessed one. The original cannot be changed, so a new image "
            "drawn from its description will not keep it exactly. To redraw it, call this tool again without "
            "source_image_id; once that new image is generated, tell the user plainly that the original could not be "
            "edited directly and that this is a new image redrawn from the description, so details may differ. Never "
            "say that the original was changed or that its other parts were kept. If you do not redraw it, tell the "
            "user that this image cannot be edited directly, and offer to redraw it from its description."
        )
    if code == ErrorCode.CONTENT_FILTERED:
        blocked = (
            "The primary image model refused this request under its content safety filter, and the fallback model "
            "also refused it."
            if first
            else "The image model refused this request under its content safety filter."
        )
        return (
            f"{blocked} Rewording the same request and calling again will not help, so do not retry on your own. Tell "
            "the user plainly that this image could not be generated because the image provider's content filter "
            "blocked it; do not guess a more specific reason. Do not quietly generate a different image that drops "
            "what the user asked for; you may offer an alternative, such as an original design inspired by the "
            "request, and generate it only if the user agrees." + NO_IMAGE
        )
    if first == ErrorCode.CONTENT_FILTERED.value:
        text = (
            "The primary image model refused this request under its content safety filter, and the fallback model "
            f"then failed (error code: {code.value}). Tell the user plainly that the image could not be generated this "
            "time."
        )
        return text + (" They can try again later." if retryable else "") + NO_IMAGE
    if code == ErrorCode.QUOTA_EXCEEDED:
        return (
            "No image was generated: the image service has used up its quota for today, so further calls will fail "
            "the same way. Do not call again today; tell the user plainly that images cannot be generated right now "
            "and that they can try again tomorrow." + NO_IMAGE
        )
    if code == ErrorCode.INVALID_PARAMS:
        return (
            f"No image was generated: the arguments were invalid ({message}). Call again only if the fix is clear from "
            "this message; otherwise tell the user plainly that the image could not be generated." + NO_IMAGE
        )
    if retryable:
        return (
            f"Image generation failed temporarily (error code: {code.value}). Tell the user plainly that the image "
            "could not be generated this time and that they can try again." + NO_IMAGE
        )
    return (
        f"Image generation failed (error code: {code.value}). Tell the user plainly that the image could not be "
        "generated this time." + NO_IMAGE
    )


def failure(
    code: ErrorCode,
    message: str | None = None,
    *,
    fields: list[str] | None = None,
    fallback_from: dict[str, str] | None = None,
    retry_after: int | None = None,
) -> ToolResult:
    spec = ERROR_SPECS[code]
    message = message or spec.message
    error: dict[str, object] = {"code": code.value, "message": message, "retryable": spec.retryable}
    if fields:
        error["fields"] = fields
    if fallback_from:
        error["fallback_from"] = fallback_from
    if retry_after is not None:
        error["retry_after"] = retry_after
    return ToolResult(
        content=failure_text(code, message, fallback_from), structured_content={"ok": False, "error": error}
    )


def success(result: StoredGenerateResult, reference_subject: str | None, edited: bool) -> ToolResult:
    return ToolResult(
        content=success_text(result, reference_subject, edited),
        structured_content={"ok": True, **result.model_dump()},
    )


def _optional(value: str | None) -> str | None:
    """空字符串、全是空格的可选参数按没填处理（Fusion 把 source_image_id: "" 判成找不到原图，画新图也失败）。"""
    value = " ".join(value.split()) if value else None
    return value or None


def init_owned_mcp(
    settings: Settings,
    cache: CacheService,
    store: ImageStore,
    http_client=None,  # noqa: ANN001
) -> tuple[str, ...]:
    """按当前模型配置注册 generate_image（比例的可选值启动时才算得出来）；返回开放的比例。"""
    ratios = tool_aspect_ratios(settings)
    AspectRatio = Literal[ratios]  # noqa: N806

    async def generate_image(
        prompt: Annotated[str, Field(min_length=1, max_length=MAX_PROMPT_CHARS, description=PROMPT_DESCRIPTION)],
        aspect_ratio: Annotated[AspectRatio | None, Field(description=ASPECT_RATIO_DESCRIPTION)] = None,
        style: Annotated[str | None, Field(max_length=MAX_STYLE_CHARS, description=STYLE_DESCRIPTION)] = None,
        regenerate: Annotated[bool, Field(description=REGENERATE_DESCRIPTION)] = False,
        reference_subject: Annotated[
            str | None, Field(max_length=MAX_REFERENCE_SUBJECT_CHARS, description=REFERENCE_SUBJECT_DESCRIPTION)
        ] = None,
        quality: Annotated[ImageQuality | None, Field(description=QUALITY_DESCRIPTION)] = None,
        source_image_id: Annotated[str | None, Field(max_length=64, description=SOURCE_IMAGE_ID_DESCRIPTION)] = None,
    ) -> ToolResult:
        lifetime, caller = request_context()
        if caller is None:
            return failure(ErrorCode.UNAUTHORIZED)
        source = data = None
        if (source_id := _optional(source_image_id)) is not None:
            source = await store.get(caller.app_id, source_id)
            data = await store.read(source) if source else None
            if data is None:
                logger.info("source_image_unavailable", app_id=caller.app_id, found=source is not None)
                return failure(ErrorCode.SOURCE_IMAGE_UNAVAILABLE)
        ratio = aspect_ratio or (closest_aspect_ratio(source.width, source.height, ratios) if source else "1:1")
        subject = _optional(reference_subject)
        try:
            request = GenerateRequest(
                prompt=prompt,
                aspect_ratio=ratio,
                style=_optional(style),
                quality=quality,
                grounding=True,
                no_cache=regenerate,
                reference_subject=subject,
            )
        except ValidationError as e:
            errors = e.errors()
            return failure(
                ErrorCode.INVALID_PARAMS,
                errors[0].get("msg") if errors else None,
                fields=validation_error_fields(errors),
            )
        try:
            result = await handle_generate(
                request,
                settings,
                cache,
                http_client=http_client,
                entry="mcp_v2",
                lifetime=lifetime,
                caller=caller,
                owned=OwnedTarget(store, source, data),
            )
        except GenerationError as e:
            return failure(e.code, e.message, fields=e.fields, fallback_from=e.fallback_from, retry_after=e.retry_after)
        except Exception:
            # handle_generate 已经记了栈，不给调用方
            return failure(ErrorCode.INTERNAL_ERROR)
        return success(result, subject, edited=source is not None)

    with suppress(KeyError):  # 第一次注册时还没有
        owned_mcp.local_provider.remove_tool(TOOL_NAME)
    owned_mcp.tool(
        name=TOOL_NAME,
        description=DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
        output_schema=None,
    )(generate_image)
    return ratios
