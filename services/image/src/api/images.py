"""/v2/mcp 出的图：按 id 取、删。都要产品密钥（CallerAuthMiddleware），只认所属产品。

- GET：别的产品的、不存在的、删了的、id 格式不对的一律 404，不透露有没有这张图。图不会变，浏览器可以一直缓存
  （产品后端转给浏览器时带上这个头）。
- DELETE：删过再删、不是本产品的都回 204（幂等；不透露有没有）。不自动清理，产品删对话时调这个。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from src.api.generate import error_response
from src.models.errors import ErrorCode
from src.models.response import ErrorResponse

router = APIRouter()

CACHE_CONTROL = "private, max-age=31536000, immutable"


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
