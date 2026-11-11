# sandbox

沙箱执行服务：让产品里的模型能跑命令（分析表格、画图、写了脚本跑一下），命令跑在隔离的一次性容器里。产品经网关 `/sandbox/...` 调用，网关 `PRODUCTS` 里要给产品加上 `sandbox` 才放行（不在默认列表里）。

第一版只做“给一个工作目录、跑一条命令、把结果和产出的文件交回去”。不联网、不装包、不流式输出、不做网页预览。

## 隔离

每条命令起一个新容器，跑完就删（服务自己删，删之前问一句 Docker 它是不是超内存被杀的），容器之间只有工作目录里的文件留得下来。容器是这样起的（`app/runner.py` 的 `run_argv`，这些参数 2026-11-11 在 dev 上逐条验过）：

- 运行时用 gVisor（`--runtime=runsc`）：命令看到的是 gVisor 自己的内核（`4.19.0-gvisor`），不直接碰宿主内核。dev 的 Docker 默认运行时仍是 `runc`，只有这里起的容器用 `runsc`。
- 断网（`--network=none`）：只有回环网卡，连不到外网、内网和宿主。
- 根文件系统只读，能写的只有 `/tmp`（内存盘，算进内存上限）和工作目录；`uploads/` 另外挂成只读。
- 普通用户（uid 10001）、去掉全部特权（`--cap-drop=ALL`、`no-new-privileges`）。
- 上限：内存 1 GiB、1 核、沙箱里 128 个进程、单个文件 256 MB、默认 60 秒（最多 120 秒）。超时由服务杀掉整个容器，容器里另包一层 `timeout` 兜底。
- 输出不进 Docker 日志（`--log-driver=none`），服务日志里也不记命令和输出。

进程数有两道：`--ulimit nproc` 管沙箱里看得到的进程，准；`--pids-limit` 在 gVisor 下数的是宿主上的线程（gVisor 自己空转就占 40 个左右），只当兜底、设得宽（512）。

镜像一个两用：服务自己用它跑，跑命令的容器也用它。命令能用的是系统 Python 3.12 和 `runtime/requirements.in` 里的包（numpy、pandas、matplotlib、seaborn、scipy、openpyxl、xlrd、tabulate、charset-normalizer），中文字体是文泉驿微米黑，matplotlib 默认就用它。服务自己的代码和 `docker` 命令行在 `/app`，沙箱里的用户进不去。

## 这个服务自己的风险

服务挂着宿主的 `/var/run/docker.sock`，等于拿着宿主的 root。所以：

- 不开端口，也不放进其他服务共用的 `agent-tools-internal`（那里的容器互相都能连）：单独一个网络 `agent-tools-sandbox`（`--internal`，不通外网），里面只有它和网关。跑命令的容器里没有这个 socket。
- 调用方给的命令只作为容器里 `bash -c` 的一个参数，不经过宿主上的任何 shell。
- 调用方给的路径、工作区 id、产品名只接受固定字符集，逐级拆开检查（`app/paths.py` 的 `split_path`），不做“修正”。
- 命令能在工作目录里放软链接、管道、很深的目录。服务读写工作目录一律从目录 fd 出发逐级 `openat`，每一级都不跟随软链接，只认真目录和普通文件：让服务去读 `outputs/link`（指向 `/etc/passwd`）只会得到“不是普通文件”。
- `uploads/` 由服务建、归 root。跑命令前检查它还是个归 root 的真目录，不是就拒绝（Docker 按路径挂它，是软链接的话会顺着挂到别处）。

## 接口

都在 `/v1/workspaces/{workspace}` 下。`workspace` 由产品自己起（`[A-Za-z0-9_-]`，最长 64，比如对话 id），按产品分开：不同产品的同名工作区互不相干。产品是谁只认网关写的 `x-caller-app`。

| 方法和路径 | 作用 |
| --- | --- |
| `PUT /file?path=...` | 放一个文件，请求体就是文件内容。放进 `uploads/` 的命令只能读；放在别处的命令可以改。目录不存在会建，工作区不存在也会建 |
| `GET /file?path=...` | 取一个文件的内容 |
| `DELETE /file?path=...` | 删一个文件 |
| `GET /files` | 列出工作区里的文件（路径、大小） |
| `POST /run` | 跑一条命令，等它结束再返回 |
| `POST /cancel` | 停掉这个工作区正在跑的命令 |
| `DELETE /v1/workspaces/{workspace}` | 删掉整个工作区 |

`path` 是工作区里的相对路径：不能以 `/` 开头，不能有 `.`、`..`、空段、反斜杠、控制字符，最多 8 级。单个文件最大 20 MB（经网关上传另受网关 10 MiB 请求体上限）。

`POST /run` 的请求体：`{"command": "...", "timeout_seconds": 60}`，`timeout_seconds` 可省（默认 60，超过 120 按 120）。命令在 `/workspace` 下用 `bash -c` 跑。返回：

```json
{"exit_code": 0, "killed": null, "duration_ms": 1830,
 "stdout": "...", "stdout_truncated": false, "stderr": "", "stderr_truncated": false,
 "changed": [{"path": "outputs/图表.png", "size": 14054, "sha256": "..."}],
 "deleted": [], "changed_truncated": false,
 "workspace": {"disk_bytes": 53248, "entries": 6, "over_quota": false}}
```

- 命令自己失败（退出码非 0）也是 200：这是给模型看的结果，不是服务的错误。
- `killed`：`timeout`（超时）、`cancelled`（被 `/cancel` 停掉）、`memory`（超过内存上限，被系统杀掉，命令自己来不及报错）、`disk_quota`（跑的过程中把工作区写超了），这时 `exit_code` 是 137。
- `stdout` / `stderr` 各留 64 KB：开头、结尾各一半，中间的丢掉并注明丢了多少字节。
- `changed`：这次运行新增或改过的普通文件（不含 `uploads/`），`outputs/` 里的排前面，最多 200 个；`deleted` 是删掉的。产品据此决定把哪些文件登记成交付物，再用 `GET /file` 取内容。

错误是 `{"error": {"code": "...", "message": "..."}}`：

| 状态 | code | 含义 |
| --- | --- | --- |
| 400 | `invalid_path` / `not_a_file` / `not_a_directory` / `is_a_directory` / `invalid_workspace` / `missing_caller` | 参数不对 |
| 404 | `not_found` / `workspace_not_found` | 文件或工作区不存在 |
| 409 | `workspace_busy` | 这个工作区还有命令在跑（文件操作等 10 秒、跑命令等 20 秒后放弃） |
| 409 | `workspace_corrupted` | `uploads/` 被换掉了，删掉工作区重来 |
| 413 | `file_too_large` / `too_large` / `workspace_over_quota` | 文件太大，或工作区已经超限（1 GiB / 5000 个目录项），先删东西 |
| 503 | `sandbox_busy` | 别的命令占着（同时只跑 1 条，排队 20 秒还轮不到），带 `Retry-After` |
| 503 | `sandbox_unavailable` | 起不了容器（Docker、gVisor 或镜像不在），`/health` 的 `reason` 里有原因 |

没起容器的请求都带 `X-Upstream-Calls: 0`，网关不算进每日次数；网关也只把 `/run` 算作调用，存取文件不算。

`GET /health`：`{"status": "ok", "ready": true, "reason": null}`。服务活着但起不了容器时 `ready` 是 `false`，`reason` 是 `docker_unreachable` / `runtime_missing` / `image_missing` / `host_path_unknown` / `run_failed`，之后每 10 秒在有请求时重探一次。

## 数据

工作目录在 `storage/workspaces/<产品>/<工作区>/`（compose 把 `./storage` 挂成 `/data`；跑命令的容器直接挂宿主上的这个目录，服务启动时从自己的挂载信息里查出宿主路径）。目录归 uid 10001、权限 700，宿主上别的普通用户看不到里面。`storage/meta/` 里记每个工作区最后一次用的时间，7 天没用过的每小时清一次。

服务重启时会清掉上次没来得及删的容器（按标签 `agent-tools.sandbox=1`）。

## 配置

没有密钥。上限都能用环境变量调，见 [.env.example](.env.example) 和 `app/config.py`。

## 本地开发

```bash
uv sync
uv run pytest -q        # 用一个假的 docker 脚本在本机直接跑命令，不需要 Docker
ruff check . && ruff format --check .
```

真实的隔离只能在装了 gVisor 的机器上验：`docker exec -i sandbox-service python - < scripts/smoke_sandbox.py`（部署流水线每次都跑），它真起容器，检查内核是 gVisor、断网、只读的地方写不了、图表里的中文画得出来、超时、内存和进程数上限生效、跑完不留容器。

改沙箱里的包：改 `runtime/requirements.in`，按文件头上的命令重新生成 `runtime/requirements.txt`。

## 还没做的

联网（装包要走白名单代理）、流式输出、网页预览、同时跑多条命令（现在是 1 条，dev 内存紧）、全部工作区加起来的磁盘总上限、Office 文档相关的包。
