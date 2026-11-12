"""部署冒烟：在 sandbox-service 容器内真跑几条命令，确认隔离在、装的包能用、超时杀得掉、文件取得回来。

一共起六个沙箱容器：分析表格出图（顺带查内核是 gVisor、断网、改不了只读的地方）、不留 __pycache__、
把一份报告转成 PDF 和 Word、超时、进程数上限、内存上限（会真的占满 1 GiB 再被杀掉，一两秒）。不经网关，不占每日次数。

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

# 一份小报告：中文、带中文的公式、钱数（$ 不该被当成公式的记号）、右对齐的数字和一长段说明的表格、代码、
# 上一步画的图、表情。
# 报告放在 uploads/（只读），图在 outputs/：产品就是这么放的，图要按当前目录找得到
REPORT = r"""# 季度经营报告

**结论**：转化率 $\text{转化率} = \frac{\text{下单人数}}{\text{访问人数}}$ 比去年高，另有 \(E = mc^2\)。

报价 $100–$200/月（原价 $250）。

| 指标 | 2024 年 | 2025 年 | 说明 |
| --- | ---: | ---: | --- |
| 收入 | 1,000 | 1,250 | 这一格的说明故意写得长一些，用来看各列的宽度是不是按内容分的，而不是每列一样宽 |

```python
print("你好")
```

![各月销售额](outputs/图表.png)

- [x] 做完的 ✅
""".encode()

# 一张网页，字体清单里点了表情字体的名（Bootstrap 之类的默认写法）：数字不该因此用表情字体里的全角数字来画
PAGE = b"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body { font-family: "Segoe UI", Roboto, "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif; }
</style></head><body><p>0123456789</p></body></html>
"""

DOCUMENTS = r"""
set -e
convert-doc uploads/报告.md outputs/报告.pdf
convert-doc uploads/报告.md outputs/报告.docx
convert-doc uploads/页面.html outputs/页面.pdf
python - <<'PY'
import json, re, zipfile

import docx, pptx
from pypdf import PdfReader


def fonts(reader):
    names = set()
    for page in reader.pages:
        for font in (page["/Resources"].get("/Font") or {}).values():
            names.add(str(font.get_object()["/BaseFont"]))
    return sorted(names)


report = PdfReader("outputs/报告.pdf")
package = zipfile.ZipFile("outputs/报告.docx")
body = package.read("word/document.xml").decode()
facts = {
    "pages": len(report.pages),
    "text": report.pages[0].extract_text()[:40],
    "dollars": sum(page.extract_text().count("$") for page in report.pages),
    # 只数大图：表情在 PDF 里也是一张小图
    "pictures": sum(image.image.width >= 300 for page in report.pages for image in page.images),
    "report_fonts": fonts(report),
    "page_fonts": fonts(PdfReader("outputs/页面.pdf")),
    "word_paragraphs": len(docx.Document("outputs/报告.docx").paragraphs),
    "word_formulas": body.count("<m:oMath>"),
    "word_columns": [int(width) for width in re.findall(r'<w:gridCol w:w="(\d+)"', body)],
    "word_pictures": sum(name.startswith("word/media/") for name in package.namelist()),
    "pptx": pptx.__version__,
}
print(json.dumps(facts, ensure_ascii=False))
PY
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

    # 3. 出文档：报告转成 PDF、Word，网页转成 PDF
    for path, content in (("uploads/报告.md", REPORT), ("uploads/页面.html", PAGE)):
        status, raw = call("PUT", f"/v1/workspaces/{WORKSPACE}/file", params={"path": path}, body=content)
        check(status == 200, f"upload of {path} returned {status}: {raw[:200]!r}")
    result = run(DOCUMENTS)
    check(result["exit_code"] == 0, f"conversion exited {result['exit_code']}: {result['stderr'][-600:]}")
    notes, _, last = result["stdout"].strip().rpartition("\n")
    facts = json.loads(last)
    # 图取到了、公式都画出来了：不然 convert-doc 会在输出里说
    check("Left out" not in notes and "could not be typeset" not in notes, f"conversion left things out: {notes}")
    check(facts["pages"] >= 1 and "季度经营报告" in facts["text"], f"PDF text is not readable: {facts}")
    check(facts["dollars"] == 3, f"dollar amounts were read as formulas: {facts}")
    check(facts["pictures"] == 1, f"the chart is not in the PDF: {facts}")
    names = " ".join(facts["report_fonts"]).lower()
    check("cjk" in names and "emoji" in names, f"PDF is missing the Chinese or the emoji font: {facts}")
    check(
        facts["page_fonts"] and "emoji" not in " ".join(facts["page_fonts"]).lower(),
        f"digits were drawn with the emoji font: {facts}",
    )
    check(facts["word_paragraphs"] > 3 and facts["word_formulas"] == 2, f"Word file is incomplete: {facts}")
    check(facts["word_pictures"] == 1, f"the chart is not in the Word file: {facts}")
    columns = facts["word_columns"]
    check(len(columns) == 4 and columns[3] > 2 * columns[1], f"table columns were not sized by content: {facts}")
    changed = {item["path"] for item in result["changed"]}
    check({"outputs/报告.pdf", "outputs/报告.docx"} <= changed, f"documents missing from changed: {sorted(changed)}")
    for path, magic in (("outputs/报告.pdf", b"%PDF-"), ("outputs/报告.docx", b"PK\x03\x04")):
        status, raw = call("GET", f"/v1/workspaces/{WORKSPACE}/file", params={"path": path})
        check(status == 200 and raw.startswith(magic) and len(raw) > 5000, f"{path} is not a real document")
    check(result["duration_ms"] < 40_000, f"conversion took too long: {result['duration_ms']} ms")
    print(f"documents ok in {result['duration_ms']} ms, python-pptx {facts['pptx']}")

    # 4. 超时
    result = run("echo started; sleep 60", timeout_seconds=2)
    check(result["killed"] == "timeout" and result["stdout"] == "started\n", f"timeout not enforced: {result}")
    check(result["duration_ms"] < 20_000, f"timeout took too long: {result['duration_ms']} ms")
    print(f"timeout ok in {result['duration_ms']} ms")

    # 5. 进程数上限
    result = run(FORK, timeout_seconds=30)
    count, _, reason = result["stdout"].strip().partition(" ")
    check(count.isdigit() and int(count) < 200 and reason != "unlimited", f"process limit not enforced: {result}")
    print(f"process limit ok: stopped at {count} ({reason})")

    # 6. 内存上限：要的比上限多，整个沙箱被系统杀掉，服务要说得出原因
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
