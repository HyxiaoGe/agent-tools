"""产品的图：/v2/mcp 出的、产品上传的，按 id 取、删。都要产品密钥（CallerAuthMiddleware），只认所属产品。

- POST：产品上传一张图（比如用户在对话里发的），存进和出图同一个私有目录、归这个产品，回图片 id，之后可以当
  /v2/mcp 的 source_image_id 改。请求体就是图片本身（不用 multipart），类型按内容认、不看 Content-Type：
  PNG / JPEG / WebP、读得出宽高、不超过 4 MB（改图时原图以 base64 内联传给上游，和参考图同一个上限）。
  不调上游、不计费、不占生图的限流。
- GET：别的产品的、不存在的、删了的、id 格式不对的一律 404，不透露有没有这张图。图不会变，浏览器可以一直缓存
  （产品后端转给浏览器时带上这个头）。
- DELETE：删过再删、不是本产品的都回 204（幂等；不透露有没有）。不自动清理，产品删对话时调这个。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from src.api.generate import error_response
from src.models.errors import ErrorCode
from src.models.response import ErrorResponse, StoredImage
from src.services.image_store import UnsupportedImage
from src.services.references import MAX_REFERENCE_BYTES

router = APIRouter()

CACHE_CONTROL = "private, max-age=31536000, immutable"
MAX_UPLOAD_BYTES = MAX_REFERENCE_BYTES
UPLOAD_MODEL = "upload"  # 上传的图在 model 里记这个：不是哪个模型出的


@router.post(
    "/images",
    status_code=201,
    response_model=StoredImage,
    responses={413: {"model": ErrorResponse}, 415: {"model": ErrorResponse}},
)
async def upload_image(request: Request) -> Response:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_UPLOAD_BYTES:
        return error_response(ErrorCode.IMAGE_TOO_LARGE)
    data = bytearray()
    async for chunk in request.stream():  # 没给长度（分块传）也照样卡住，不整个读进内存
        data += chunk
        if len(data) > MAX_UPLOAD_BYTES:
            return error_response(ErrorCode.IMAGE_TOO_LARGE)
    store = request.app.state.image_store
    try:
        record = await store.save(app_id=request.state.image_caller.app_id, data=bytes(data), model=UPLOAD_MODEL)
    except UnsupportedImage:
        return error_response(ErrorCode.UNSUPPORTED_IMAGE)
    return JSONResponse(status_code=201, content=record.public().model_dump())


@router.get("/images/{image_id}", responses={404: {"model": ErrorResponse}})
async def get_image(image_id: str, request: Request) -> Response:
    store = request.app.state.image_store
    caller = request.state.image_caller
    record = await store.get(caller.app_id, image_id)
    path = store.path(record) if record else None
    if path is None or not path.is_file():
        return error_response(ErrorCode.IMAGE_NOT_FOUND)
    return FileResponse(
        path,
        media_type=record.mime_type,
        headers={"Cache-Control": CACHE_CONTROL, "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/images/{image_id}", status_code=204, response_class=Response)
async def delete_image(image_id: str, request: Request) -> Response:
    store = request.app.state.image_store
    await store.delete(request.state.image_caller.app_id, image_id)
    return Response(status_code=204)
