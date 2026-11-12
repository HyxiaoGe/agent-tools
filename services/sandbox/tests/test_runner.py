"""跑命令这一层：传给 docker 的参数、超时、停止、输出截断、Docker 自己失败。用 conftest 里的假 docker。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.runner import CONTAINER_LABEL, DockerRunner, Limits, OutputBuffer, SandboxUnavailable

pytestmark = pytest.mark.anyio

LIMITS = Limits(memory_mb=1024, cpus=1.0, tmp_mb=256, nproc=128, pids_limit=512, nofile=1024, file_size_mb=256)


def make_runner(docker_bin: str, *, output_limit_bytes: int = 4096) -> DockerRunner:
    return DockerRunner(
        docker_bin=docker_bin,
        image="agent-tools/sandbox:test",
        runtime="runsc",
        uid=10001,
        gid=10001,
        limits=LIMITS,
        output_limit_bytes=output_limit_bytes,
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    directory = tmp_path / "ws"
    (directory / "uploads").mkdir(parents=True)
    return directory


async def wait_until(condition, timeout: float = 10.0) -> None:
    """等到条件成立；runner 忙的时候起进程慢，不靠固定的 sleep。"""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


async def run(runner: DockerRunner, workdir: Path, command: str, timeout_seconds: int = 10, **kwargs):
    return await runner.run(
        name="sbx-test",
        host_workspace=str(workdir),
        command=command,
        timeout_seconds=timeout_seconds,
        stop=kwargs.pop("stop", asyncio.Event()),
        **kwargs,
    )


def test_run_argv_carries_every_isolation_flag():
    command = "echo $(id) && cat 'a b' | head -1; rm -rf /"
    argv = make_runner("docker").run_argv(
        name="sbx-1", host_workspace="/host/storage/workspaces/lingdraft/c1", command=command, timeout_seconds=60
    )
    for flag in (
        "--pull=never",
        "--runtime=runsc",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=10001:10001",
        "--memory=1024m",
        "--memory-swap=1024m",
        "--cpus=1.0",
        "--pids-limit=512",
        "--log-driver=none",
        "--entrypoint=/usr/bin/timeout",
    ):
        assert flag in argv, flag
    assert argv[argv.index("--label") + 1] == CONTAINER_LABEL
    ulimits = [argv[i + 1] for i, value in enumerate(argv) if value == "--ulimit"]
    assert ulimits == ["nproc=128:128", "nofile=1024:1024", f"fsize={256 * 1024 * 1024}:{256 * 1024 * 1024}"]
    mounts = [argv[i + 1] for i, value in enumerate(argv) if value == "--mount"]
    assert mounts == [
        "type=bind,source=/host/storage/workspaces/lingdraft/c1,target=/workspace",
        "type=bind,source=/host/storage/workspaces/lingdraft/c1/uploads,target=/workspace/uploads,readonly",
    ]
    assert argv[argv.index("--tmpfs") + 1] == "/tmp:rw,nosuid,nodev,size=256m"
    # 工具的缓存不留在工作目录里（会被当成这次运行产出的文件）。
    for env in ("PYTHONDONTWRITEBYTECODE=1", "PYTEST_ADDOPTS=-p no:cacheprovider", "RUFF_CACHE_DIR=/tmp/ruff"):
        assert f"--env={env}" in argv, env
    # 命令原样作为 bash -c 的唯一参数，排在最后；镜像之后是容器里 timeout 的参数。
    image_at = argv.index("agent-tools/sandbox:test")
    assert argv[image_at + 1 :] == ["--signal=KILL", "70", "/bin/bash", "-c", command]
    # 容器由服务自己删（先问是不是超内存被杀的），不用 --rm。
    assert "--rm" not in argv
    # 不把宿主的东西带进去：没有 --privileged、没有 docker.sock，网络、进程、IPC 都不和宿主共用。
    options = argv[:image_at]
    assert not any("privileged" in option or "docker.sock" in option for option in options)
    assert not any(option.endswith("=host") for option in options)
    assert not any(option in ("-v", "--volume", "--cap-add", "--device") for option in options)


async def test_run_returns_output_and_exit_code(fake_docker, docker_state, workdir):
    outcome = await run(make_runner(fake_docker), workdir, "echo out; echo err >&2; echo hi > made.txt; exit 3")
    assert outcome.exit_code == 3
    assert outcome.killed is None
    assert outcome.stdout.text() == "out\n"
    assert outcome.stderr.text() == "err\n"
    assert (workdir / "made.txt").read_text() == "hi\n"
    assert "--force sbx-test" in (docker_state / "removed.log").read_text()
    # 假 docker 记下了它收到的参数：命令没有被宿主上的 shell 解释过。
    assert json.loads((docker_state / "last_run.json").read_text())[-1].startswith("echo out;")


async def test_timeout_kills_the_container(fake_docker, docker_state, workdir):
    started = time.monotonic()
    outcome = await run(make_runner(fake_docker), workdir, "echo before; sleep 30; echo after", timeout_seconds=1)
    assert time.monotonic() - started < 10
    assert outcome.killed == "timeout"
    assert outcome.exit_code == 137
    assert outcome.stdout.text() == "before\n"
    assert "sbx-test" in (docker_state / "removed.log").read_text()


async def test_stop_event_cancels_a_running_command(fake_docker, docker_state, workdir):
    stop = asyncio.Event()
    task = asyncio.create_task(run(make_runner(fake_docker), workdir, "sleep 30", stop=stop))
    await wait_until((docker_state / "sbx-test.pid").exists)
    stop.set()
    outcome = await asyncio.wait_for(task, 10)
    assert outcome.killed == "cancelled"
    assert outcome.exit_code == 137


async def test_stop_before_the_container_exists_still_stops_it(fake_docker, docker_state, workdir):
    # 刚发起就停：第一次 kill 时容器还没建出来，要接着再杀，不能干等到超时
    stop = asyncio.Event()
    stop.set()
    started = time.monotonic()
    outcome = await run(make_runner(fake_docker), workdir, "sleep 30", stop=stop)
    assert outcome.killed == "cancelled"
    assert time.monotonic() - started < 8
    assert not (docker_state / "sbx-test.pid").exists()


async def test_check_callback_can_stop_the_command(fake_docker, workdir):
    calls = 0

    async def check() -> str | None:
        nonlocal calls
        calls += 1
        return "disk_quota" if calls >= 2 else None

    outcome = await run(make_runner(fake_docker), workdir, "sleep 30", check=check, check_interval=0.2)
    assert outcome.killed == "disk_quota"
    assert calls == 2


async def test_cancelled_request_does_not_leave_the_container_running(fake_docker, docker_state, workdir):
    task = asyncio.create_task(run(make_runner(fake_docker), workdir, "sleep 30"))
    await wait_until((docker_state / "sbx-test.pid").exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (docker_state / "sbx-test.pid").exists()


async def test_long_output_keeps_head_and_tail(fake_docker, workdir):
    command = "echo START; for i in $(seq 1 5000); do echo line-$i; done; echo END"
    outcome = await run(make_runner(fake_docker, output_limit_bytes=2048), workdir, command)
    text = outcome.stdout.text()
    assert outcome.stdout.truncated is True
    assert text.startswith("START\nline-1\n")
    assert text.endswith("line-5000\nEND\n")
    assert "bytes omitted" in text
    assert len(text) < 2048 + 100


async def test_out_of_memory_kill_is_reported(fake_docker, docker_state, workdir):
    (docker_state / "oom").touch()
    outcome = await run(make_runner(fake_docker), workdir, "kill -KILL $$")
    assert outcome.exit_code == 137
    assert outcome.killed == "memory"
    assert "--force sbx-test" in (docker_state / "removed.log").read_text()


async def test_docker_failure_is_not_reported_as_a_command_failure(fake_docker, docker_state, workdir):
    (docker_state / "fail_run").touch()
    with pytest.raises(SandboxUnavailable) as error:
        await run(make_runner(fake_docker), workdir, "echo hi")
    assert "invalid runtime name" in str(error.value)


async def test_command_exiting_125_on_its_own_is_just_a_result(fake_docker, workdir):
    outcome = await run(make_runner(fake_docker), workdir, "echo oops >&2; exit 125")
    assert outcome.exit_code == 125


async def test_probe_reports_what_is_missing(fake_docker, docker_state):
    runner = make_runner(fake_docker)
    assert await runner.probe() is None
    (docker_state / "no_image").touch()
    assert await runner.probe() == "image_missing"
    (docker_state / "no_runtime").touch()
    assert await runner.probe() == "runtime_missing"
    assert await make_runner("/nonexistent/docker").probe() == "docker_unreachable"


async def test_bind_source_finds_the_host_directory(fake_docker):
    runner = make_runner(fake_docker)
    assert await runner.bind_source("self", "/data") == "/host/storage"
    assert await runner.bind_source("self", "/cache") is None  # 命名卷不算
    assert await runner.bind_source("self", "/elsewhere") is None


async def test_reap_removes_leftover_containers(fake_docker, docker_state):
    runner = make_runner(fake_docker)
    assert await runner.reap() == 0
    (docker_state / "leftover").touch()
    assert await runner.reap() == 1
    assert "deadbeef0001" in (docker_state / "removed.log").read_text()


def test_output_buffer_is_exact_below_the_limit():
    buffer = OutputBuffer(10)
    for chunk in (b"abc", b"defg", b"hij"):
        buffer.feed(chunk)
    assert buffer.truncated is False
    assert buffer.text() == "abcdefghij"
    buffer.feed(b"k")
    assert buffer.truncated is True
    assert buffer.text() == "abcde\n[... 1 bytes omitted ...]\nghijk"
