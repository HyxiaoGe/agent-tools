"""部署冒烟：在 sandbox-service 容器内真跑几条命令，确认隔离在、装的包能用、超时杀得掉、文件取得回来。

一共起五个沙箱容器：分析表格出图（顺带查内核是 gVisor、断网、改不了只读的地方）、不留 __pycache__、
超时、进程数上限、内存上限（会真的占满 1 GiB 再被杀掉，一两秒）。不经网关，不占每日次数。

用法：docker exec -i sandbox-service python - < scripts/smoke_sandbox.py
"""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:8095"
HEADERS = {"x-caller-app": "smoke"}
WORKSPACE = f"smoke-{secrets.token_hex(4)}"
CSV = "月份,销售额,成本\n一月,120,80\n二月,135,90\n三月,-20,95\n".encode()

ANALYZE = r"""
set -e
cat > analyze.py <<'PY'
import json, os, platform, socket, warnings

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# 缺字形是 UserWarning，当成错误：图里的中文和负号必须画得出来
warnings.simplefilter("error", UserWarning)

facts = {"kernel": platform.release(), "uid": os.getuid(), "cwd": os.getcwd()}

try:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    facts["network"] = "reachable"
except OSError as exc:
    facts["network"] = type(exc).__name__

def writable(path):
    try:
        with open(path, "w") as f:
            f.write("x")
        return True
    except OSError:
        return False

facts["can_write"] = {p: writable(p) for p in ("uploads/x.txt", "/etc/x", "/app/x", "/tmp/x", "scratch.txt")}
facts["sees_service_code"] = os.access("/app", os.R_OK)
facts["sees_docker_socket"] = os.path.exists("/var/run/docker.sock")

df = pd.read_csv("uploads/销售.csv")
facts["rows"] = len(df)
facts["total"] = int(df["销售额"].sum())

fig, ax = plt.subplots(figsize=(6, 4))
sns.barplot(data=df, x="月份", y="销售额", ax=ax)
ax.set_title("各月销售额（含负数）")
fig.savefig("outputs/图表.png", dpi=120)
df.assign(利润=df["销售额"] - df["成本"]).to_excel("outputs/汇总.xlsx", index=False)
print(json.dumps(facts, ensure_ascii=False))
PY
python analyze.py
"""

FORK = r"""python - <<'PY'
import os, sys, time
children = 0
try:
    for _ in range(400):
        pid = os.fork()
        if pid == 0:
            time.sleep(20)
            os._exit(0)
        children += 1
except OSError as exc:
    print(children, type(exc).__name__)
    sys.exit(0)
print(children, "unlimited")
PY"""


def call(method, path, *, params=None, body=None, json_body=None, timeout=200):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = dict(HEADERS)
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def run(command, **extra):
    status, raw = call("POST", f"/v1/workspaces/{WORKSPACE}/run", json_body={"command": command, **extra})
    if status != 200:
        fail(f"run returned {status}: {raw[:300]!r}")
    return json.loads(raw)


def fail(message):
    call("DELETE", f"/v1/workspaces/{WORKSPACE}")
    sys.exit(f"smoke failed: {message}")


def check(condition, message):
    if not condition:
        fail(message)


def main():
    status, raw = call("GET", "/health")
    health = json.loads(raw)
    check(status == 200 and health.get("ready"), f"service not ready: {health}")

    status, raw = call("PUT", f"/v1/workspaces/{WORKSPACE}/file", params={"path": "uploads/销售.csv"}, body=CSV)
    check(status == 200, f"upload returned {status}: {raw[:200]!r}")

    # 1. 分析表格出图
    result = run(ANALYZE)
    check(result["exit_code"] == 0, f"analysis exited {result['exit_code']}: {result['stderr'][-600:]}")
    facts = json.loads(result["stdout"].strip().splitlines()[-1])
    check("gvisor" in facts["kernel"], f"not running under gVisor: kernel {facts['kernel']}")
    check(facts["uid"] == 10001 and facts["cwd"] == "/workspace", f"unexpected identity: {facts}")
    check(facts["network"] != "reachable", "sandbox can reach the network")
    expected = {"uploads/x.txt": False, "/etc/x": False, "/app/x": False, "/tmp/x": True, "scratch.txt": True}
    check(facts["can_write"] == expected, f"unexpected writable paths: {facts['can_write']}")
    check(not facts["sees_service_code"] and not facts["sees_docker_socket"], f"sandbox sees too much: {facts}")
    check(facts["rows"] == 3 and facts["total"] == 235, f"wrong analysis result: {facts}")
    changed = {item["path"]: item for item in result["changed"]}
    check(
        {"outputs/图表.png", "outputs/汇总.xlsx", "analyze.py", "scratch.txt"} <= set(changed),
        f"changed files missing: {sorted(changed)}",
    )
    check(not any("__pycache__" in path for path in changed), f"bytecode cache left behind: {sorted(changed)}")
    print(f"analysis ok in {result['duration_ms']} ms, kernel {facts['kernel']}")
    if result["stderr"].strip():
        # 不算失败，但留在部署日志里：包之间的过时提醒模型每次画图都会看到
        print(f"analysis stderr: {result['stderr'][-800:]}")

    status, png = call("GET", f"/v1/workspaces/{WORKSPACE}/file", params={"path": "outputs/图表.png"})
    check(status == 200 and png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 5000, "chart is not a real PNG")
    check(hashlib.sha256(png).hexdigest() == changed["outputs/图表.png"]["sha256"], "chart digest mismatch")
    status, raw = call("GET", f"/v1/workspaces/{WORKSPACE}/file", params={"path": "uploads/销售.csv"})
    check(status == 200 and raw == CSV, "upload was modified")

    # 2. 上一条命令留下的文件还在；容器是新的（/tmp 里的没了）
    result = run("test -f scratch.txt && test ! -e /tmp/x && echo 'x = 1' > mod.py && python -c 'import mod' && ls -a")
    check(result["exit_code"] == 0, f"second run exited {result['exit_code']}: {result['stderr'][-300:]}")
    check("__pycache__" not in result["stdout"], f"bytecode cache written into the workspace: {result['stdout']}")
    print(f"second run ok in {result['duration_ms']} ms")

    # 3. 超时
    result = run("echo started; sleep 60", timeout_seconds=2)
    check(result["killed"] == "timeout" and result["stdout"] == "started\n", f"timeout not enforced: {result}")
    check(result["duration_ms"] < 20_000, f"timeout took too long: {result['duration_ms']} ms")
    print(f"timeout ok in {result['duration_ms']} ms")

    # 4. 进程数上限
    result = run(FORK, timeout_seconds=30)
    count, _, reason = result["stdout"].strip().partition(" ")
    check(count.isdigit() and int(count) < 200 and reason != "unlimited", f"process limit not enforced: {result}")
    print(f"process limit ok: stopped at {count} ({reason})")

    # 5. 内存上限：要的比上限多，整个沙箱被系统杀掉，服务要说得出原因
    result = run("python -c \"x = b'a' * (3 * 1024 ** 3); print(len(x))\"", timeout_seconds=60)
    check(result["killed"] == "memory" and result["exit_code"] == 137, f"memory limit not enforced: {result}")
    print(f"memory limit ok in {result['duration_ms']} ms")

    status, raw = call("DELETE", f"/v1/workspaces/{WORKSPACE}")
    check(status == 200 and json.loads(raw) == {"deleted": True}, f"workspace delete returned {status}")

    # 跑完不留容器
    docker = os.environ.get("DOCKER_BIN", "docker")
    left = subprocess.run(
        [docker, "ps", "--all", "--quiet", "--filter", "label=agent-tools.sandbox=1"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    check(left.returncode == 0 and not left.stdout.strip(), f"sandbox containers left behind: {left.stdout!r}")
    print("smoke ok")


if __name__ == "__main__":
    main()
