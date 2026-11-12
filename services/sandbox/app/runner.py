"""用宿主的 Docker 起一次性的容器跑命令。

每条命令一个新容器，跑完就删（实测 gVisor 下起一次约 0.4 秒）：不留常驻容器，超时直接杀掉整个容器，
不会有残留进程。不用 --rm：命令结束后要先问 Docker 它是不是因为超内存被杀的，再由这里删。
调用方给的命令只作为容器里 bash -c 的一个参数传进去，不经过宿主上的任何 shell。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("sandbox")

# 给容器打的标签：服务重启时按它清掉上次没来得及删的容器。
CONTAINER_LABEL = "agent-tools.sandbox=1"
_DOCKER_CALL_TIMEOUT = 15
# docker run 自己失败（镜像、运行时不在等）时的退出码；容器里的命令也可能回 125，再看报错开头区分。
_DOCKER_FAILED_EXIT = 125
_DOCKER_ERROR_MARKERS = ("docker: Error", "Unable to find image")


class SandboxUnavailable(Exception):
    """Docker 这一层没把容器跑起来，不是命令本身失败。"""


class OutputBuffer:
    """只留开头和结尾各一半：开头看得出在跑什么，报错栈一般在结尾。中间的丢掉并注明丢了多少。"""

    def __init__(self, limit: int) -> None:
        self._half = max(1, limit // 2)
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self._half - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self._tail += chunk
            if len(self._tail) > self._half:
                del self._tail[: len(self._tail) - self._half]

    @property
    def truncated(self) -> bool:
        return self.total > len(self._head) + len(self._tail)

    def text(self) -> str:
        if not self.truncated:
            return bytes(self._head + self._tail).decode("utf-8", "replace")
        omitted = self.total - len(self._head) - len(self._tail)
        head = self._head.decode("utf-8", "replace")
        tail = self._tail.decode("utf-8", "replace")
        return f"{head}\n[... {omitted} bytes omitted ...]\n{tail}"


@dataclass(frozen=True)
class Limits:
    memory_mb: int
    cpus: float
    tmp_mb: int
    nproc: int
    pids_limit: int
    nofile: int
    file_size_mb: int


@dataclass(frozen=True)
class RunOutcome:
    # docker run 的退出码，也就是容器里命令的退出码；被杀掉时是 137。连 docker 命令都没正常结束时是 None。
    exit_code: int | None
    stdout: OutputBuffer
    stderr: OutputBuffer
    # 不是自己结束的原因：timeout / cancelled / memory（超内存被系统杀掉）/ check 回调给的原因（如 disk_quota）；
    # 正常结束是 None。
    killed: str | None
    duration_ms: int


class DockerRunner:
    def __init__(
        self,
        *,
        docker_bin: str,
        image: str,
        runtime: str,
        uid: int,
        gid: int,
        limits: Limits,
        output_limit_bytes: int,
    ) -> None:
        self._docker = docker_bin
        self.image = image
        self.runtime = runtime
        self._user = f"{uid}:{gid}"
        self._limits = limits
        self._output_limit = output_limit_bytes

    def run_argv(self, *, name: str, host_workspace: str, command: str, timeout_seconds: int) -> list[str]:
        limits = self._limits
        file_bytes = limits.file_size_mb * 1024 * 1024
        return [
            self._docker,
            "run",
            "--pull=never",
            "--name",
            name,
            "--label",
            CONTAINER_LABEL,
            f"--runtime={self.runtime}",
            # 断网：容器里只有回环网卡。
            "--network=none",
            # 根文件系统只读；能写的只有 /tmp（内存盘，算进内存上限）和挂进来的工作目录。
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={limits.tmp_mb}m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--user={self._user}",
            f"--memory={limits.memory_mb}m",
            f"--memory-swap={limits.memory_mb}m",
            f"--cpus={limits.cpus}",
            f"--pids-limit={limits.pids_limit}",
            "--ulimit",
            f"nproc={limits.nproc}:{limits.nproc}",
            "--ulimit",
            f"nofile={limits.nofile}:{limits.nofile}",
            "--ulimit",
            f"fsize={file_bytes}:{file_bytes}",
            # 输出直接从这条 docker run 的管道里读，不让 Docker 另存一份日志（里面是用户的数据）。
            "--log-driver=none",
            "--hostname=sandbox",
            # --mount 在源目录不存在时报错，不会像 -v 那样替你建一个。
            "--mount",
            f"type=bind,source={host_workspace},target=/workspace",
            "--mount",
            f"type=bind,source={host_workspace}/uploads,target=/workspace/uploads,readonly",
            "--workdir=/workspace",
            # 不继承镜像里给服务自己用的环境变量。
            "--env=HOME=/tmp",
            "--env=PATH=/usr/local/bin:/usr/bin:/bin",
            "--env=LANG=C.UTF-8",
            "--env=TZ=Asia/Shanghai",
            "--env=PYTHONUNBUFFERED=1",
            # 不在工作目录里留 __pycache__（会被当成这次运行产出的文件）；镜像里装好的包自带编译结果，不受影响。
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=MPLCONFIGDIR=/tmp/matplotlib",
            "--env=MATPLOTLIBRC=/opt/sandbox/matplotlibrc",
            # pytest、ruff 的缓存目录同理，不留在工作目录里：一个不写，一个写到 /tmp。
            "--env=PYTEST_ADDOPTS=-p no:cacheprovider",
            "--env=RUFF_CACHE_DIR=/tmp/ruff",
            # 容器里再包一层 timeout：服务自己掐表是主要手段，这层防的是服务中途挂掉、没人来杀。
            "--entrypoint=/usr/bin/timeout",
            self.image,
            "--signal=KILL",
            str(timeout_seconds + 10),
            "/bin/bash",
            "-c",
            command,
        ]

    async def run(
        self,
        *,
        name: str,
        host_workspace: str,
        command: str,
        timeout_seconds: int,
        stop: asyncio.Event,
        check: Callable[[], Awaitable[str | None]] | None = None,
        check_interval: float = 1.0,
    ) -> RunOutcome:
        """跑一条命令直到它结束、超时、stop 被置位，或 check 回了一个原因（比如磁盘超限）。"""
        argv = self.run_argv(name=name, host_workspace=host_workspace, command=command, timeout_seconds=timeout_seconds)
        stdout, stderr = OutputBuffer(self._output_limit), OutputBuffer(self._output_limit)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        pumps = asyncio.gather(_pump(process.stdout, stdout), _pump(process.stderr, stderr))
        exited = asyncio.ensure_future(process.wait())
        stopped = asyncio.ensure_future(stop.wait())
        killed: str | None = None
        try:
            while True:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    killed = "timeout"
                    break
                done, _ = await asyncio.wait(
                    {exited, stopped}, timeout=min(check_interval, remaining), return_when=asyncio.FIRST_COMPLETED
                )
                if exited in done:
                    break
                if stopped in done:
                    killed = "cancelled"
                    break
                if check is not None and (reason := await check()):
                    killed = reason
                    break
            ended = time.monotonic()
            if killed is not None:
                await self._stop(name, process, exited)
            else:
                # 自己结束的：超内存时系统直接杀掉整个沙箱，命令来不及留下任何报错，只有 Docker 知道原因。
                if await self._oom_killed(name):
                    killed = "memory"
                await self._call("rm", "--force", name)
            await pumps
        except BaseException:
            # 请求被取消、服务在关：容器不能留着继续跑。
            await asyncio.shield(self._stop(name, process, exited))
            pumps.cancel()
            raise
        finally:
            stopped.cancel()
        duration_ms = int((ended - started) * 1000)
        exit_code = process.returncode if process.returncode is not None and process.returncode >= 0 else None
        if killed is None and exit_code == _DOCKER_FAILED_EXIT:
            for line in stderr.text()[:2000].splitlines():
                if any(marker in line for marker in _DOCKER_ERROR_MARKERS):
                    raise SandboxUnavailable(line.strip()[:300])
        return RunOutcome(exit_code=exit_code, stdout=stdout, stderr=stderr, killed=killed, duration_ms=duration_ms)

    async def _stop(self, name: str, process: asyncio.subprocess.Process, exited: asyncio.Future[int]) -> None:
        """杀掉容器并等 docker run 退出，最后强删容器。

        刚发起就被停的时候容器可能还没建出来，kill 会扑空：隔一秒再杀一次；一直不退就连 docker run 一起杀。
        """
        for _ in range(_DOCKER_CALL_TIMEOUT):
            await self._call("kill", name)
            try:
                await asyncio.wait_for(asyncio.shield(exited), 1)
                break
            except TimeoutError:
                continue
        else:
            if process.returncode is None:
                process.kill()
            await exited
        await self._call("rm", "--force", name)

    async def _oom_killed(self, name: str) -> bool:
        code, out = await self._call("inspect", "--format", "{{.State.OOMKilled}}", name)
        return code == 0 and out.strip() == "true"

    async def _call(self, *args: str) -> tuple[int, str]:
        """跑一条 docker 管理命令，回退出码和标准输出。失败只回非零，由调用方决定要不要紧。"""
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return 127, ""
        try:
            out, _ = await asyncio.wait_for(process.communicate(), _DOCKER_CALL_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, ""
        return process.returncode or 0, out.decode("utf-8", "replace")

    async def reap(self) -> int:
        """删掉带本服务标签的容器（上次服务退出时没清掉的）。"""
        code, out = await self._call("ps", "--all", "--quiet", "--filter", f"label={CONTAINER_LABEL}")
        ids = out.split() if code == 0 else []
        if ids:
            await self._call("rm", "--force", *ids)
        return len(ids)

    async def probe(self) -> str | None:
        """能不能跑：回 None 表示就绪，否则回原因。"""
        code, out = await self._call("info", "--format", "{{json .Runtimes}}")
        if code != 0:
            return "docker_unreachable"
        try:
            runtimes = json.loads(out)
        except ValueError:
            return "docker_unreachable"
        if not isinstance(runtimes, dict) or self.runtime not in runtimes:
            return "runtime_missing"
        code, _ = await self._call("image", "inspect", "--format", "{{.Id}}", self.image)
        return None if code == 0 else "image_missing"

    async def bind_source(self, container: str, destination: str) -> str | None:
        """查某个容器里 destination 这个挂载点对应的宿主目录。"""
        code, out = await self._call("inspect", "--format", "{{json .Mounts}}", container)
        if code != 0:
            return None
        try:
            mounts = json.loads(out)
        except ValueError:
            return None
        for mount in mounts if isinstance(mounts, list) else []:
            if isinstance(mount, dict) and mount.get("Destination") == destination and mount.get("Type") == "bind":
                source = mount.get("Source")
                return source if isinstance(source, str) and source.startswith("/") else None
        return None


async def _pump(stream: asyncio.StreamReader, buffer: OutputBuffer) -> None:
    while chunk := await stream.read(65536):
        buffer.feed(chunk)
