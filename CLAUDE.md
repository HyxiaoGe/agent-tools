# agent-tools 协作约定

本仓库合并了 image / search / reader / trends / amap / context7 / weather / flyai / tmap 工具服务与统一网关，见 [README](README.md)，端口、部署与流水线见 [docs/operations.md](docs/operations.md)。改某个服务时读取该服务目录下的 CLAUDE.md / README；跨服务改动（统一入口、共享鉴权）同时核对各服务的调用方。

- 回复、代码注释和提交使用中文，提交标题为 `<type>: <中文描述>`，时间按 `Asia/Shanghai`。
- 合入 `main` 会触发 dev 部署（只部署有改动的服务）；合并后盯部署结果。
- 服务之间不互相 import，各自保留独立的依赖与镜像。
- 密钥只放 dev 部署目录的 `.env`，不入库、不打印、不写进日志或模型可见内容。
