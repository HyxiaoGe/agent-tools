"""工作区：每个产品下按工作区 id 一个目录，留在磁盘上；跑命令时挂进一次性的容器。

目录约定：`uploads/` 是放给命令看的原件（归 root，跑命令时单独挂成只读）；`outputs/` 约定放交付物；
其余位置随便用。同一个工作区的操作排队做，跑命令期间不能同时读写文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app import paths
from app.config import Settings
from app.runner import DockerRunner, SandboxUnavailable

logger = logging.getLogger("sandbox")

UPLOADS_DIR = "uploads"
OUTPUTS_DIR = "outputs"
# 一次运行最多报这么多个变动的文件，多的只说“还有”。
MAX_CHANGED = 200
# 读写文件等同一工作区里别的操作，最多等这么久。
_FILE_LOCK_WAIT = 10.0
# 没就绪时隔这么久再探一次。
_PROBE_INTERVAL = 10.0

# 宿主上的数据目录：绝对路径，只含不会被 --mount 的解析拆开的字符。
_HOST_PATH_PATTERN = re.compile(r"/[A-Za-z0-9_./ -]*")

Key = tuple[str, str]


class ServiceError(Exception):
    def __init__(
        self, status: int, code: str, message: str, *, counted: bool = True, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        # counted=False：这次请求没起容器（参数不对、没排上、没就绪），网关不该算进每日次数。
        self.counted = counted
        self.retry_after = retry_after


class Workspaces:
    def __init__(self, settings: Settings, runner: DockerRunner) -> None:
        self._settings = settings
        self._runner = runner
        data = Path(settings.sandbox_data_dir)
        self._root = data / "workspaces"
        self._meta = data / "meta"
        # 宿主上对应 sandbox_data_dir 的目录；没配就在启动时从自己容器的挂载里查。
        self._host_data_dir: str | None = settings.sandbox_host_data_dir.rstrip("/") or None
        # 在容器里以 root 运行时才改属主；本机跑测试不是 root，文件就归当前用户。
        self._owner = (settings.sandbox_uid, settings.sandbox_gid) if os.geteuid() == 0 else None
        self._max_file_bytes = settings.max_file_mb * 1024 * 1024
        self._locks: dict[Key, asyncio.Lock] = {}
        self._active: dict[Key, asyncio.Event] = {}
        self._slots = asyncio.Semaphore(settings.max_concurrent_runs)
        self._not_ready: str | None = "starting"
        self._probed_at = 0.0
        # 跑命令期间隔多久看一次磁盘用量。
        self.check_interval = 1.0

    # ---- 就绪 ----

    @property
    def not_ready_reason(self) -> str | None:
        return self._not_ready

    async def startup(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._meta.mkdir(parents=True, exist_ok=True)
        reaped = await self._runner.reap()
        await self._probe()
        logger.info(
            "sandbox_started ready=%s reason=%s runtime=%s reaped=%d",
            self._not_ready is None,
            self._not_ready,
            self._runner.runtime,
            reaped,
        )

    async def _probe(self) -> None:
        self._probed_at = time.monotonic()
        if self._host_data_dir is None:
            source = await self._runner.bind_source(socket.gethostname(), self._settings.sandbox_data_dir)
            if source is None:
                self._not_ready = "host_path_unknown"
                return
            self._host_data_dir = source.rstrip("/")
        # 这个路径要拼进 docker run 的 --mount（逗号分隔的键值），带逗号、引号的会被拆成别的选项。
        if not _HOST_PATH_PATTERN.fullmatch(self._host_data_dir):
            self._not_ready = "host_path_unknown"
            return
        self._not_ready = await self._runner.probe()

    async def _require_ready(self) -> None:
        if self._not_ready is not None and time.monotonic() - self._probed_at >= _PROBE_INTERVAL:
            await self._probe()
        if self._not_ready is not None:
            raise ServiceError(
                503, "sandbox_unavailable", "Command execution is not available right now.", counted=False
            )

    # ---- 目录 ----

    @asynccontextmanager
    async def _locked(self, key: Key, wait: float) -> AsyncGenerator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with asyncio.timeout(wait):
                await lock.acquire()
        except TimeoutError:
            raise ServiceError(
                409, "workspace_busy", "Another operation is still running in this workspace.", counted=False
            ) from None
        try:
            yield
        finally:
            lock.release()

    def _directory(self, key: Key) -> Path:
        return self._root / key[0] / key[1]

    def _marker(self, key: Key) -> Path:
        # 记“最后一次用是什么时候”的空文件，放在工作目录外面：目录里的时间戳命令自己能改。
        return self._meta / key[0] / key[1]

    def _open(self, key: Key, *, create: bool) -> int:
        """打开工作目录，回它的 fd（调用方负责关）。create 时没有就建，并把约定的子目录补齐。"""
        directory = self._directory(key)
        created = False
        if create:
            # 产品这一级归 root，沙箱里的用户动不了，工作目录自己没法被换成软链接。
            directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(directory, 0o700)
                created = True
            except FileExistsError:
                pass
        try:
            root_fd = paths.open_root(directory)
        except FileNotFoundError:
            raise ServiceError(404, "workspace_not_found", "No such workspace.", counted=False) from None
        try:
            if created:
                os.fchmod(root_fd, 0o700)
                if self._owner is not None:
                    os.fchown(root_fd, *self._owner)
            if create:
                self._ensure_layout(root_fd)
            marker = self._marker(key)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except BaseException:
            os.close(root_fd)
            raise
        return root_fd

    def _ensure_layout(self, root_fd: int) -> None:
        # uploads 必须是服务自己建的真目录：跑命令时 Docker 按路径挂它，是软链接的话会顺着挂到别处去。
        try:
            info = os.stat(UPLOADS_DIR, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(UPLOADS_DIR, 0o755, dir_fd=root_fd)
            os.chmod(UPLOADS_DIR, 0o755, dir_fd=root_fd)
        else:
            if not stat.S_ISDIR(info.st_mode) or (self._owner is not None and info.st_uid != 0):
                raise ServiceError(
                    409,
                    "workspace_corrupted",
                    "The workspace layout was tampered with; delete the workspace and start over.",
                    counted=False,
                )
        # outputs 先建好，省得命令自己建；已经有同名的东西就不管它。
        try:
            os.mkdir(OUTPUTS_DIR, 0o755, dir_fd=root_fd)
        except FileExistsError:
            return
        os.chmod(OUTPUTS_DIR, 0o755, dir_fd=root_fd)
        if self._owner is not None:
            os.chown(OUTPUTS_DIR, *self._owner, dir_fd=root_fd, follow_symlinks=False)

    def _scan(self, root_fd: int) -> paths.Usage:
        return paths.scan(root_fd, max_entries=self._settings.workspace_max_entries)

    def _over_quota(self, usage: paths.Usage) -> bool:
        return usage.truncated or usage.disk_bytes > self._settings.workspace_limit_mb * 1024 * 1024

    # ---- 文件 ----

    async def put_file(self, key: Key, raw_path: str, data: bytes) -> dict[str, Any]:
        parts = paths.split_path(raw_path)
        if len(data) > self._max_file_bytes:
            raise ServiceError(413, "file_too_large", "File is too large.", counted=False)

        def work() -> None:
            root_fd = self._open(key, create=True)
            try:
                # uploads 里的归 root，命令改不了；别处的归沙箱里的用户，命令可以接着改。
                owner = None if parts[0] == UPLOADS_DIR else self._owner
                paths.write_file(root_fd, parts, data, owner=owner)
            finally:
                os.close(root_fd)

        async with self._locked(key, _FILE_LOCK_WAIT):
            await asyncio.to_thread(work)
        return {"path": "/".join(parts), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    async def get_file(self, key: Key, raw_path: str) -> bytes:
        parts = paths.split_path(raw_path)

        def work() -> bytes:
            root_fd = self._open(key, create=False)
            try:
                return paths.read_file(root_fd, parts, self._max_file_bytes)
            finally:
                os.close(root_fd)

        async with self._locked(key, _FILE_LOCK_WAIT):
            return await asyncio.to_thread(work)

    async def delete_file(self, key: Key, raw_path: str) -> bool:
        parts = paths.split_path(raw_path)

        def work() -> bool:
            root_fd = self._open(key, create=False)
            try:
                return paths.delete_file(root_fd, parts)
            finally:
                os.close(root_fd)

        async with self._locked(key, _FILE_LOCK_WAIT):
            return await asyncio.to_thread(work)

    async def list_files(self, key: Key) -> dict[str, Any]:
        def work() -> paths.Usage:
            root_fd = self._open(key, create=False)
            try:
                return self._scan(root_fd)
            finally:
                os.close(root_fd)

        async with self._locked(key, _FILE_LOCK_WAIT):
            usage = await asyncio.to_thread(work)
        return {
            "files": [{"path": entry.path, "size": entry.size} for _, entry in sorted(usage.files.items())],
            "truncated": usage.truncated,
            "disk_bytes": usage.disk_bytes,
        }

    async def delete_workspace(self, key: Key) -> bool:
        async with self._locked(key, _FILE_LOCK_WAIT):
            return await asyncio.to_thread(self._remove, key)

    def _remove(self, key: Key) -> bool:
        directory = self._directory(key)
        existed = os.path.lexists(directory)
        if existed:
            try:
                # Linux 上 rmtree 从目录 fd 往下删，不跟随软链接。
                shutil.rmtree(directory)
            except (RecursionError, OSError):
                # 命令可以把目录嵌到几千层：rmtree 是递归的、每层占一个 fd，删不动；rm 不受这个限制，同样不跟随软链接。
                subprocess.run(["rm", "-rf", "--", str(directory)], check=True, timeout=300)
        self._marker(key).unlink(missing_ok=True)
        return existed

    # ---- 跑命令 ----

    async def run(self, key: Key, command: str, timeout_seconds: int) -> dict[str, Any]:
        await self._require_ready()
        wait = self._settings.queue_wait_seconds
        async with self._locked(key, wait):
            try:
                async with asyncio.timeout(wait):
                    await self._slots.acquire()
            except TimeoutError:
                raise ServiceError(
                    503,
                    "sandbox_busy",
                    "Other commands are running; retry shortly.",
                    counted=False,
                    retry_after=5,
                ) from None
            try:
                return await self._run(key, command, timeout_seconds)
            finally:
                self._slots.release()

    async def _run(self, key: Key, command: str, timeout_seconds: int) -> dict[str, Any]:
        root_fd = await asyncio.to_thread(self._open, key, create=True)
        try:
            before = await asyncio.to_thread(self._scan, root_fd)
            if self._over_quota(before):
                raise ServiceError(
                    413,
                    "workspace_over_quota",
                    "The workspace is over its disk or file-count limit; delete files or the workspace first.",
                    counted=False,
                )

            async def check() -> str | None:
                usage = await asyncio.to_thread(self._scan, root_fd)
                return "disk_quota" if self._over_quota(usage) else None

            stop = asyncio.Event()
            self._active[key] = stop
            try:
                outcome = await self._runner.run(
                    name=f"sbx-{secrets.token_hex(8)}",
                    host_workspace=f"{self._host_data_dir}/workspaces/{key[0]}/{key[1]}",
                    command=command,
                    timeout_seconds=timeout_seconds,
                    stop=stop,
                    check=check,
                    check_interval=self.check_interval,
                )
            except SandboxUnavailable as exc:
                # 下次请求重新探一遍（比如镜像刚被新一次部署换掉）。
                self._not_ready = "run_failed"
                self._probed_at = 0.0
                logger.error("sandbox_run_failed product=%s reason=%s", key[0], exc)
                raise ServiceError(
                    503, "sandbox_unavailable", "Command execution is not available right now.", counted=False
                ) from None
            finally:
                self._active.pop(key, None)
            after = await asyncio.to_thread(self._scan, root_fd)
            changed, deleted, more = await asyncio.to_thread(self._diff, root_fd, before, after)
        finally:
            os.close(root_fd)
        # 命令和输出是用户的数据，不进日志。
        logger.info(
            "sandbox_run product=%s workspace=%s exit=%s killed=%s duration_ms=%d changed=%d",
            key[0],
            key[1],
            outcome.exit_code,
            outcome.killed,
            outcome.duration_ms,
            len(changed),
        )
        return {
            "exit_code": outcome.exit_code,
            "killed": outcome.killed,
            "duration_ms": outcome.duration_ms,
            "stdout": outcome.stdout.text(),
            "stdout_truncated": outcome.stdout.truncated,
            "stderr": outcome.stderr.text(),
            "stderr_truncated": outcome.stderr.truncated,
            "changed": changed,
            "deleted": deleted,
            "changed_truncated": more,
            "workspace": {
                "disk_bytes": after.disk_bytes,
                "entries": after.entries,
                "over_quota": self._over_quota(after),
            },
        }

    def _diff(
        self, root_fd: int, before: paths.Usage, after: paths.Usage
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        """这次运行新增或改过的文件、删掉的文件。uploads 里的不看（命令改不了）。"""
        prefix = UPLOADS_DIR + "/"
        changed = [
            entry
            for path, entry in after.files.items()
            if not path.startswith(prefix)
            and ((old := before.files.get(path)) is None or (old.size, old.mtime_ns) != (entry.size, entry.mtime_ns))
        ]
        # 报不完时先报 outputs 里的。
        changed.sort(key=lambda entry: (not entry.path.startswith(OUTPUTS_DIR + "/"), entry.path))
        deleted = sorted(path for path in before.files if path not in after.files and not path.startswith(prefix))
        more = after.truncated or len(changed) > MAX_CHANGED or len(deleted) > MAX_CHANGED
        items = [
            {
                "path": entry.path,
                "size": entry.size,
                "sha256": paths.sha256_file(root_fd, paths.split_path(entry.path), self._max_file_bytes),
            }
            for entry in changed[:MAX_CHANGED]
        ]
        return items, deleted[:MAX_CHANGED], more

    def cancel(self, key: Key) -> bool:
        """停掉这个工作区正在跑的命令；没有在跑的回 False。"""
        stop = self._active.get(key)
        if stop is None:
            return False
        stop.set()
        return True

    # ---- 清理 ----

    async def sweep(self) -> int:
        """删掉超过保留期没用过的工作区。"""
        cutoff = time.time() - self._settings.retention_days * 86400
        removed = 0
        for key in await asyncio.to_thread(self._stale, cutoff):
            lock = self._locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                continue
            async with lock:
                # 排到这里时可能刚被用过，再看一眼。
                if await asyncio.to_thread(self._is_stale, key, cutoff):
                    await asyncio.to_thread(self._remove, key)
                    removed += 1
        if removed:
            logger.info("sandbox_swept removed=%d", removed)
        return removed

    def _is_stale(self, key: Key, cutoff: float) -> bool:
        try:
            return self._marker(key).stat().st_mtime < cutoff
        except FileNotFoundError:
            return False

    def _stale(self, cutoff: float) -> list[Key]:
        stale: list[Key] = []
        if not self._meta.is_dir():
            return stale
        for product_dir in self._meta.iterdir():
            if not paths.PRODUCT_ID_PATTERN.fullmatch(product_dir.name) or not product_dir.is_dir():
                continue
            for marker in product_dir.iterdir():
                key = (product_dir.name, marker.name)
                if paths.WORKSPACE_ID_PATTERN.fullmatch(marker.name) and self._is_stale(key, cutoff):
                    stale.append(key)
        return stale
