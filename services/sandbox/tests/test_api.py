"""接口这一层：产品隔离、路径检查、跑命令后报出变动的文件、排队和取消。底下用 conftest 里的假 docker。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.main import build_runner, create_app

pytestmark = pytest.mark.anyio

LINGDRAFT = {"x-caller-app": "lingdraft"}
FUSION = {"x-caller-app": "fusion"}


def make_settings(tmp_path: Path, fake_docker: str, **overrides) -> Settings:
    data = tmp_path / "data"
    values = {
        "sandbox_data_dir": str(data),
        "sandbox_host_data_dir": str(data),
        "docker_bin": fake_docker,
        "queue_wait_seconds": 0.3,
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
async def make_client(tmp_path, fake_docker):
    clients: list[httpx.AsyncClient] = []

    async def factory(**overrides) -> httpx.AsyncClient:
        settings = make_settings(tmp_path, fake_docker, **overrides)
        app = create_app(settings, build_runner(settings))
        app.state.workspaces.check_interval = 0.2
        await app.state.workspaces.startup()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sandbox")
        client.app = app  # type: ignore[attr-defined]
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()


@pytest.fixture
async def client(make_client):
    return await make_client()


async def wait_until_running(client, workspace: str, product: str = "lingdraft", timeout: float = 10.0) -> None:
    """等到这个工作区的命令真的跑起来了；runner 忙的时候起进程慢，不靠固定的 sleep。"""
    deadline = time.monotonic() + timeout
    while (product, workspace) not in client.app.state.workspaces._active:
        assert time.monotonic() < deadline, "command did not start in time"
        await asyncio.sleep(0.05)


async def run(client, workspace: str, command: str, headers=LINGDRAFT, **body):
    return await client.post(f"/v1/workspaces/{workspace}/run", json={"command": command, **body}, headers=headers)


async def test_health_reports_ready(client):
    response = await client.get("/health")
    assert response.json() == {"status": "ok", "ready": True, "reason": None}


async def test_requests_must_carry_the_gateway_identity(client):
    response = await client.get("/v1/workspaces/c1/files")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_caller"
    assert response.headers["x-upstream-calls"] == "0"
    response = await client.get("/v1/workspaces/c1/files", headers={"x-caller-app": "../etc"})
    assert response.status_code == 400


@pytest.mark.parametrize("workspace", ["a.b", "-abc", "a" * 65, "a%2Fb", "有中文"])
async def test_workspace_id_is_restricted(client, workspace):
    response = await run(client, workspace, "echo hi")
    assert response.status_code in (400, 404)
    assert not any((Path(client.app.state.workspaces._root)).glob("*/*"))


async def test_put_get_delete_file_roundtrip(client):
    path = "uploads/销售 数据.csv"
    body = "月份,销售额\n一月,10\n".encode()
    put = await client.put("/v1/workspaces/c1/file", params={"path": path}, content=body, headers=LINGDRAFT)
    assert put.status_code == 200
    assert put.json() == {"path": path, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}

    got = await client.get("/v1/workspaces/c1/file", params={"path": path}, headers=LINGDRAFT)
    assert got.status_code == 200 and got.content == body

    listed = await client.get("/v1/workspaces/c1/files", headers=LINGDRAFT)
    assert listed.json()["files"] == [{"path": path, "size": len(body)}]

    deleted = await client.delete("/v1/workspaces/c1/file", params={"path": path}, headers=LINGDRAFT)
    assert deleted.json() == {"deleted": True}
    missing = await client.get("/v1/workspaces/c1/file", params={"path": path}, headers=LINGDRAFT)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a//b", "a/../../b", "a\\b", "uploads/../../x"])
async def test_paths_outside_the_workspace_are_rejected(client, path):
    for method in ("PUT", "GET", "DELETE"):
        response = await client.request(
            method, "/v1/workspaces/c1/file", params={"path": path}, content=b"x", headers=LINGDRAFT
        )
        assert response.status_code == 400, (method, path)
        assert response.json()["error"]["code"] == "invalid_path"


async def test_products_do_not_see_each_others_workspaces(client):
    await client.put("/v1/workspaces/c1/file", params={"path": "a.txt"}, content=b"lingdraft", headers=LINGDRAFT)
    other = await client.get("/v1/workspaces/c1/file", params={"path": "a.txt"}, headers=FUSION)
    assert other.status_code == 404 and other.json()["error"]["code"] == "workspace_not_found"
    await client.put("/v1/workspaces/c1/file", params={"path": "a.txt"}, content=b"fusion", headers=FUSION)
    mine = await client.get("/v1/workspaces/c1/file", params={"path": "a.txt"}, headers=LINGDRAFT)
    assert mine.content == b"lingdraft"


async def test_oversized_file_is_rejected(make_client):
    client = await make_client(max_file_mb=1)
    response = await client.put(
        "/v1/workspaces/c1/file", params={"path": "big.bin"}, content=b"x" * (1024 * 1024 + 1), headers=LINGDRAFT
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "file_too_large"


async def test_run_reports_output_and_changed_files(client):
    await client.put("/v1/workspaces/c1/file", params={"path": "uploads/in.txt"}, content=b"3\n", headers=LINGDRAFT)
    await client.put("/v1/workspaces/c1/file", params={"path": "keep.txt"}, content=b"same", headers=LINGDRAFT)
    await client.put("/v1/workspaces/c1/file", params={"path": "old.txt"}, content=b"old", headers=LINGDRAFT)
    await client.put("/v1/workspaces/c1/file", params={"path": "edit.txt"}, content=b"v1", headers=LINGDRAFT)

    command = "cat uploads/in.txt; echo 图表 > outputs/图表.txt; echo v2-longer > edit.txt; rm old.txt; echo warn >&2"
    response = await run(client, "c1", command)

    assert response.status_code == 200
    result = response.json()
    assert result["exit_code"] == 0 and result["killed"] is None
    assert result["stdout"] == "3\n" and result["stderr"] == "warn\n"
    assert result["stdout_truncated"] is False
    # outputs 里的排在前面；没动过的 keep.txt、uploads 里的原件不报
    assert [item["path"] for item in result["changed"]] == ["outputs/图表.txt", "edit.txt"]
    assert result["changed"][0]["sha256"] == hashlib.sha256("图表\n".encode()).hexdigest()
    assert result["changed"][0]["size"] == len("图表\n".encode())
    assert result["deleted"] == ["old.txt"]
    assert result["changed_truncated"] is False
    assert result["workspace"]["over_quota"] is False

    got = await client.get("/v1/workspaces/c1/file", params={"path": "outputs/图表.txt"}, headers=LINGDRAFT)
    assert got.content == "图表\n".encode()


async def test_failing_command_is_a_normal_result(client):
    response = await run(client, "c1", "echo boom >&2; exit 7")
    assert response.status_code == 200
    assert response.json()["exit_code"] == 7
    assert "x-upstream-calls" not in response.headers


async def test_links_planted_by_a_command_cannot_be_fetched(client, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the workspace")
    command = f"ln -s {secret} outputs/link.txt; ln -s {tmp_path} outputs/dir; mkfifo outputs/pipe; echo ok"
    result = (await run(client, "c1", command)).json()
    assert result["stdout"] == "ok\n"
    assert result["changed"] == []

    for path, code in [
        ("outputs/link.txt", "not_a_file"),
        ("outputs/dir/secret.txt", "not_a_directory"),
        ("outputs/pipe", "not_a_file"),
    ]:
        response = await client.get("/v1/workspaces/c1/file", params={"path": path}, headers=LINGDRAFT)
        assert response.status_code == 400 and response.json()["error"]["code"] == code, path

    # 往软链接指的目录里写也不行，外面不会多出文件
    response = await client.put(
        "/v1/workspaces/c1/file", params={"path": "outputs/dir/planted.txt"}, content=b"x", headers=LINGDRAFT
    )
    assert response.status_code == 400
    assert not (tmp_path / "planted.txt").exists()


async def test_tampered_uploads_directory_blocks_the_workspace(client):
    await run(client, "c1", "true")
    directory = Path(client.app.state.workspaces._root) / "lingdraft" / "c1"
    (directory / "uploads").rmdir()
    (directory / "uploads").symlink_to("/etc")
    response = await run(client, "c1", "ls uploads")
    assert response.status_code == 409 and response.json()["error"]["code"] == "workspace_corrupted"
    assert response.headers["x-upstream-calls"] == "0"


async def test_timeout_is_clamped_and_reported(make_client):
    client = await make_client(max_timeout_seconds=1)
    response = await run(client, "c1", "sleep 30", timeout_seconds=600)
    result = response.json()
    assert result["killed"] == "timeout" and result["exit_code"] == 137


async def test_cancel_stops_the_running_command(client):
    task = asyncio.create_task(run(client, "c1", "sleep 30"))
    await wait_until_running(client, "c1")
    cancelled = await client.post("/v1/workspaces/c1/cancel", headers=LINGDRAFT)
    assert cancelled.json() == {"cancelled": True}
    result = (await asyncio.wait_for(task, 10)).json()
    assert result["killed"] == "cancelled"
    again = await client.post("/v1/workspaces/c1/cancel", headers=LINGDRAFT)
    assert again.json() == {"cancelled": False}
    # 别的产品停不了它：同名工作区按产品分开
    assert (await client.post("/v1/workspaces/c1/cancel", headers=FUSION)).json() == {"cancelled": False}


async def test_only_one_command_runs_at_a_time(client):
    task = asyncio.create_task(run(client, "c1", "sleep 30"))
    await wait_until_running(client, "c1")
    try:
        busy = await run(client, "c2", "echo hi")
        assert busy.status_code == 503 and busy.json()["error"]["code"] == "sandbox_busy"
        assert busy.headers["x-upstream-calls"] == "0" and busy.headers["retry-after"] == "5"

        same = await run(client, "c1", "echo hi")
        assert same.status_code == 409 and same.json()["error"]["code"] == "workspace_busy"
    finally:
        await client.post("/v1/workspaces/c1/cancel", headers=LINGDRAFT)
        await asyncio.wait_for(task, 10)
    # 前一条结束后就能跑了
    assert (await run(client, "c2", "echo hi")).json()["stdout"] == "hi\n"


async def test_queued_command_runs_when_the_slot_frees_up(make_client):
    client = await make_client(queue_wait_seconds=10)
    first = asyncio.create_task(run(client, "c1", "sleep 1; echo first"))
    await asyncio.sleep(0.3)
    second = await run(client, "c2", "echo second")
    assert second.json()["stdout"] == "second\n"
    assert (await first).json()["stdout"] == "first\n"


async def test_workspace_over_quota_refuses_to_run(make_client):
    client = await make_client(workspace_limit_mb=1)
    await client.put(
        "/v1/workspaces/c1/file", params={"path": "big.bin"}, content=os.urandom(2 * 1024 * 1024), headers=LINGDRAFT
    )
    response = await run(client, "c1", "echo hi")
    assert response.status_code == 413 and response.json()["error"]["code"] == "workspace_over_quota"
    assert response.headers["x-upstream-calls"] == "0"


async def test_command_that_fills_the_disk_is_killed(make_client):
    client = await make_client(workspace_limit_mb=1)
    command = "head -c 3000000 /dev/urandom > outputs/big.bin; sleep 30"
    result = (await run(client, "c1", command)).json()
    assert result["killed"] == "disk_quota"
    assert result["workspace"]["over_quota"] is True


async def test_too_many_files_counts_as_over_quota(make_client):
    client = await make_client(workspace_max_entries=10)
    result = (await run(client, "c1", "for i in $(seq 1 30); do touch f$i; done")).json()
    assert result["workspace"]["over_quota"] is True
    assert result["changed_truncated"] is True
    assert (await run(client, "c1", "echo hi")).status_code == 413


async def test_run_is_unavailable_when_runtime_is_missing(make_client, docker_state):
    (docker_state / "no_runtime").touch()
    client = await make_client()
    health = (await client.get("/health")).json()
    assert health["ready"] is False and health["reason"] == "runtime_missing"
    response = await run(client, "c1", "echo hi")
    assert response.status_code == 503 and response.json()["error"]["code"] == "sandbox_unavailable"
    assert response.headers["x-upstream-calls"] == "0"
    # 文件接口不受影响
    put = await client.put("/v1/workspaces/c1/file", params={"path": "a.txt"}, content=b"x", headers=LINGDRAFT)
    assert put.status_code == 200


async def test_docker_failure_marks_the_service_not_ready(client, docker_state):
    (docker_state / "fail_run").touch()
    response = await run(client, "c1", "echo hi")
    assert response.status_code == 503 and response.json()["error"]["code"] == "sandbox_unavailable"
    assert (await client.get("/health")).json()["reason"] == "run_failed"
    (docker_state / "fail_run").unlink()
    assert (await run(client, "c1", "echo hi")).json()["stdout"] == "hi\n"
    assert (await client.get("/health")).json()["ready"] is True


async def test_host_path_is_discovered_from_own_mounts(tmp_path, fake_docker, monkeypatch):
    settings = make_settings(tmp_path, fake_docker, sandbox_host_data_dir="")
    monkeypatch.setenv("FAKE_DOCKER_DATA_DIR", settings.sandbox_data_dir)
    app = create_app(settings, build_runner(settings))
    await app.state.workspaces.startup()
    assert app.state.workspaces._host_data_dir == "/host/storage"


async def test_delete_workspace_removes_everything(client):
    await client.put("/v1/workspaces/c1/file", params={"path": "a.txt"}, content=b"x", headers=LINGDRAFT)
    assert (await client.delete("/v1/workspaces/c1", headers=LINGDRAFT)).json() == {"deleted": True}
    assert (await client.delete("/v1/workspaces/c1", headers=LINGDRAFT)).json() == {"deleted": False}
    assert (await client.get("/v1/workspaces/c1/files", headers=LINGDRAFT)).status_code == 404


async def test_sweep_removes_only_stale_workspaces(client):
    for workspace in ("old", "fresh"):
        await client.put(f"/v1/workspaces/{workspace}/file", params={"path": "a.txt"}, content=b"x", headers=LINGDRAFT)
    workspaces = client.app.state.workspaces
    stale = time.time() - 8 * 86400
    os.utime(workspaces._marker(("lingdraft", "old")), (stale, stale))

    assert await workspaces.sweep() == 1

    assert (await client.get("/v1/workspaces/old/files", headers=LINGDRAFT)).status_code == 404
    assert (await client.get("/v1/workspaces/fresh/files", headers=LINGDRAFT)).status_code == 200


async def test_malformed_run_requests_are_rejected_without_counting(client):
    for body in ({}, {"command": ""}, {"command": "echo hi\x00"}, {"command": "echo", "timeout_seconds": 0}):
        response = await client.post("/v1/workspaces/c1/run", json=body, headers=LINGDRAFT)
        assert response.status_code == 400, body
        assert response.json()["error"]["code"] == "invalid_request"
        assert response.headers["x-upstream-calls"] == "0"
    response = await client.post("/v1/workspaces/c1/run", content=b"not json", headers=LINGDRAFT)
    assert response.status_code == 400


async def test_host_path_that_would_split_the_mount_option_is_refused(tmp_path, fake_docker):
    settings = make_settings(tmp_path, fake_docker, sandbox_host_data_dir="/srv/data,readonly")
    app = create_app(settings, build_runner(settings))
    await app.state.workspaces.startup()
    assert app.state.workspaces.not_ready_reason == "host_path_unknown"


async def test_workspace_nested_too_deep_for_rmtree_can_still_be_deleted(client):
    await client.put("/v1/workspaces/c1/file", params={"path": "a.txt"}, content=b"x", headers=LINGDRAFT)
    directory = Path(client.app.state.workspaces._root) / "lingdraft" / "c1"
    fd = os.open(directory, os.O_RDONLY)
    try:
        for _ in range(1500):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = child
    finally:
        os.close(fd)

    listed = await client.get("/v1/workspaces/c1/files", headers=LINGDRAFT)
    assert listed.json()["truncated"] is True
    assert (await client.delete("/v1/workspaces/c1", headers=LINGDRAFT)).json() == {"deleted": True}
    assert not directory.exists()
