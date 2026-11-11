"""测试用的假 docker：一个脚本，按服务调用 docker 的方式接参数，在本机直接跑命令，不需要真的 Docker。

run：进到 --mount 的源目录里用 bash -c 跑最后一个参数（也就是调用方的命令），被杀时像真 docker 一样回 137。
kill：按容器名杀掉对应的进程组。其余管理命令回固定结果，可以用状态目录里的标记文件改成失败
（fail_run、no_runtime、no_image、leftover、oom）。
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, signal, sys

state = os.environ["FAKE_DOCKER_STATE"]
args = sys.argv[1:]
command = args[0]


def flag(name):
    return os.path.exists(os.path.join(state, name))


if command == "run":
    if flag("fail_run"):
        sys.stderr.write("docker: Error response from daemon: unknown or invalid runtime name: runsc.\n")
        sys.stderr.write("See 'docker run --help'.\n")
        sys.exit(125)
    name = args[args.index("--name") + 1]
    source = next(a for a in args if a.startswith("type=bind,source=")).split(",")[1].split("=", 1)[1]
    with open(os.path.join(state, "last_run.json"), "w") as f:
        json.dump(args, f)
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.chdir(source)
        os.execv("/bin/bash", ["bash", "-c", args[-1]])
    pidfile = os.path.join(state, name + ".pid")
    with open(pidfile, "w") as f:
        f.write(str(pid))
    _, status = os.waitpid(pid, 0)
    os.remove(pidfile)
    sys.exit(128 + os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status))
elif command == "kill":
    try:
        with open(os.path.join(state, args[1] + ".pid")) as f:
            pid = int(f.read())
    except FileNotFoundError:
        sys.exit(1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            sys.exit(1)
elif command == "ps":
    if flag("leftover"):
        print("deadbeef0001")
elif command == "rm":
    with open(os.path.join(state, "removed.log"), "a") as f:
        f.write(" ".join(args[1:]) + "\n")
elif command == "info":
    print(json.dumps({"runc": {}} if flag("no_runtime") else {"runc": {}, "runsc": {}}))
elif command == "image":
    if flag("no_image"):
        sys.exit(1)
    print("sha256:0000")
elif command == "inspect" and "OOMKilled" in args[2]:
    print("true" if flag("oom") else "false")
elif command == "inspect":
    print(json.dumps([
        {"Type": "volume", "Source": "/var/lib/docker/volumes/x/_data", "Destination": "/cache"},
        {"Type": "bind", "Source": "/host/storage", "Destination": os.environ.get("FAKE_DOCKER_DATA_DIR", "/data")},
    ]))
else:
    sys.exit(2)
"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def docker_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "docker-state"
    state.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    return state


@pytest.fixture
def fake_docker(tmp_path: Path, docker_state: Path) -> str:
    script = tmp_path / "docker"
    script.write_text(FAKE_DOCKER)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)
