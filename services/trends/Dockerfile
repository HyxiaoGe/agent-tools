# TrendRadar 官方 MCP 镜像：Windows 构建机经代理拉取后推到 ACR，dev 拉不动 Docker Hub。
# 升级时只改这里的版本号，并和 dev 上 TrendRadar 抓取端的版本保持配套；
# contract_server.py 里的可选值要按新版本上游校验重新核对。
FROM wantcat/trendradar-mcp:4.1.1

COPY contract_server.py /app/contract_server.py
CMD ["python", "/app/contract_server.py"]
