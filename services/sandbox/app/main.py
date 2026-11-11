"""沙箱执行服务：在 gVisor 隔离、断网、有上限的一次性容器里跑一条命令。

自身不鉴权，单独一个内部网络、只有网关能到，经网关 /sandbox/... 访问；调用方是哪个产品只认网关写的 x-caller-app，
工作区按产品分开。服务拿着宿主 Docker 的控制入口（等于宿主的 root），所以调用方给的东西只有两种用法：
路径只在工作目录里按 fd 逐级打开；命令只作为容器里 bash -c 的一个参数。见 app/paths.py、app/runner.py。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from app import paths
from app.config import Settings
from app.runner import DockerRunner, Limits
from app.workspaces import Key, ServiceError, Workspaces

logger = logging.getLogger("sandbox")

# 回这个头、值为 0，网关就不把这次请求算进每日次数（见 gateway/app/proxy.py）。
NO_UPSTREAM_CALL_HEADER = "x-upstream-calls"
_SWEEP_INTERVAL_SECONDS = 3600
_COMMAND_MAX_CHARS = 20_000


class RunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=_COMMAND_MAX_CHARS)
    timeout_seconds: int | None = Field(default=None, ge=1)

    @field_validator("command")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        # 命令要作为一个参数交给 docker run，参数里不能有 NUL。
        if "\x00" in value:
            raise ValueError("command must not contain NUL characters")
        return value


def build_runner(settings: Settings) -> DockerRunner:
    return DockerRunner(
        docker_bin=settings.docker_bin,
        image=settings.sandbox_image,
        runtime=settings.sandbox_runtime,
        uid=settings.sandbox_uid,
        gid=settings.sandbox_gid,
        limits=Limits(
            memory_mb=settings.sandbox_memory_mb,
            cpus=settings.sandbox_cpus,
            tmp_mb=settings.sandbox_tmp_mb,
            nproc=settings.sandbox_nproc,
            pids_limit=settings.sandbox_pids_limit,
            nofile=settings.sandbox_nofile,
            file_size_mb=settings.sandbox_file_size_mb,
        ),
        output_limit_bytes=settings.output_limit_bytes,
    )


def _error(status: int, code: str, message: str, *, counted: bool = True, retry_after: int | None = None) -> Response:
    headers: dict[str, str] = {}
    if not counted:
        headers[NO_UPSTREAM_CALL_HEADER] = "0"
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


_PATH_ERROR_STATUS = {"not_found": 404, "too_large": 413}


def _key(request: Request, workspace: str) -> Key:
    """(产品, 工作区)。产品只认网关写的头；两样都只接受固定字符集，因为要拼进目录名。"""
    product = request.headers.get("x-caller-app", "")
    if not paths.PRODUCT_ID_PATTERN.fullmatch(product):
        raise ServiceError(400, "missing_caller", "Requests must come through the gateway.", counted=False)
    if not paths.WORKSPACE_ID_PATTERN.fullmatch(workspace):
        raise ServiceError(400, "invalid_workspace", "Workspace id must match [A-Za-z0-9_-]{1,64}.", counted=False)
    return product, workspace


WorkspaceKey = Annotated[Key, Depends(_key)]
FilePath = Annotated[str, Query(alias="path", min_length=1, max_length=paths.MAX_PATH_BYTES)]


def create_app(settings: Settings | None = None, runner: DockerRunner | None = None) -> FastAPI:
    resolved = settings or Settings()
    workspaces = Workspaces(resolved, runner or build_runner(resolved))

    async def sweep_forever() -> None:
        while True:
            try:
                await workspaces.sweep()
            except Exception:
                logger.exception("sandbox_sweep_failed")
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        await workspaces.startup()
        sweeper = asyncio.create_task(sweep_forever())
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper

    app = FastAPI(title="agent-tools sandbox", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.workspaces = workspaces

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> Response:
        return _error(exc.status, exc.code, exc.message, counted=exc.counted, retry_after=exc.retry_after)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> Response:
        # 不把请求内容带回去；哪个字段不对由调用方对照 README 查。
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        return _error(400, "invalid_request", f"Invalid request: {', '.join(fields)[:200]}", counted=False)

    @app.exception_handler(paths.PathError)
    async def _path_error(request: Request, exc: paths.PathError) -> Response:
        return _error(_PATH_ERROR_STATUS.get(exc.code, 400), exc.code, exc.message, counted=False)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        reason = workspaces.not_ready_reason
        return {"status": "ok", "ready": reason is None, "reason": reason}

    @app.put("/v1/workspaces/{workspace}/file")
    async def put_file(request: Request, key: WorkspaceKey, path: FilePath) -> dict[str, Any]:
        limit = resolved.max_file_mb * 1024 * 1024
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise ServiceError(413, "file_too_large", "File is too large.", counted=False)
        return await workspaces.put_file(key, path, bytes(body))

    @app.get("/v1/workspaces/{workspace}/file")
    async def get_file(key: WorkspaceKey, path: FilePath) -> Response:
        data = await workspaces.get_file(key, path)
        return Response(data, media_type="application/octet-stream")

    @app.delete("/v1/workspaces/{workspace}/file")
    async def delete_file(key: WorkspaceKey, path: FilePath) -> dict[str, Any]:
        return {"deleted": await workspaces.delete_file(key, path)}

    @app.get("/v1/workspaces/{workspace}/files")
    async def list_files(key: WorkspaceKey) -> dict[str, Any]:
        return await workspaces.list_files(key)

    @app.post("/v1/workspaces/{workspace}/run")
    async def run(key: WorkspaceKey, body: RunRequest) -> dict[str, Any]:
        timeout = min(body.timeout_seconds or resolved.default_timeout_seconds, resolved.max_timeout_seconds)
        return await workspaces.run(key, body.command, timeout)

    @app.post("/v1/workspaces/{workspace}/cancel")
    async def cancel(key: WorkspaceKey) -> dict[str, Any]:
        return {"cancelled": workspaces.cancel(key)}

    @app.delete("/v1/workspaces/{workspace}")
    async def delete_workspace(key: WorkspaceKey) -> dict[str, Any]:
        return {"deleted": await workspaces.delete_workspace(key)}

    return app


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
