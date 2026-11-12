# sandbox

沙箱执行服务，说明见 [README](README.md)。`app/runner.py` 起容器，`app/paths.py` 是工作目录里的路径规则和文件操作，`app/workspaces.py` 管工作区（排队、上限、清理），`app/main.py` 是接口。

- 服务挂着宿主的 docker.sock，等于宿主的 root。改这里的代码先想清楚调用方给的每样东西最后落在哪：命令只能是容器里 `bash -c` 的一个参数；路径只能经 `paths.split_path` 拆开后按 fd 逐级打开；工作区 id、产品名只认固定字符集。不要在宿主上用 shell 拼命令，不要用 `os.path.join` + `open` 读写工作目录。
- 工作目录里的东西是不可信的代码留下的：软链接、管道、超深目录、超多文件都可能有。新加的文件操作要在 `tests/test_paths.py` 里补上对应的用例。
- `run_argv` 里的隔离参数（`--runtime`、`--network=none`、`--read-only`、`--cap-drop=ALL`、`--user`、各项上限）每一条都在 dev 上验过，`tests/test_runner.py` 逐条断言。去掉或放宽任何一条都要先问 Sean。
- 不联网是第一版的前提。要让沙箱联网（装包等）是产品决定，先问 Sean，不要顺手加 `--network`。
- 命令、输出、文件名是用户的数据，不进日志；日志只记产品、工作区 id、退出码、耗时。
- 给调用方的 `message` 用英文（会原样给到模型）；不带宿主路径和 Docker 的报错原文。
- 没起容器的失败要带 `counted=False`（响应头 `X-Upstream-Calls: 0`），网关才不会算进每日次数。
- 镜像一个两用，服务和沙箱容器共用：不要把 `/app/.venv/bin` 放进镜像的 `PATH`，不要放开 `/app` 的权限。
- `runtime/` 里是装进镜像、给沙箱里的命令用的东西（包清单、`convert-doc` 和它的样式），不是服务的代码。`convert-doc` 打印的话是给模型看的，用英文。字体、Word 模板这类二进制文件在构建镜像时生成，不放进仓库。
- 单测用 `tests/conftest.py` 里的假 docker 在本机直接跑命令，验的是服务的逻辑，不是隔离；隔离靠 `scripts/smoke_sandbox.py` 在 dev 上真跑。
