# 只从这里取 docker 命令行（静态二进制），版本和 dev 上的 Docker 一致。
FROM docker:29.1.3-cli AS docker-cli

# 一个镜像两用：服务自己用它跑（/app 里的代码和 .venv）；服务起的沙箱容器也用它（系统 Python 和下面装的包）。
FROM python:3.12-slim

# 依赖按 uv.lock 安装，与测试时一致；uv 从 PyPI 装，不依赖 ghcr.io。
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    DOCKER_BIN=/app/bin/docker
# 构建机可用 --build-arg 换 PyPI 镜像源（pip 直接读这个变量），默认官方源。
ARG PIP_INDEX_URL=https://pypi.org/simple

# 中文字体（图表里的中文）、时区数据；命令在沙箱里用的普通用户（号码和 SANDBOX_UID / SANDBOX_GID 一致）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /bin/bash sandbox

# 沙箱里的命令能用的包，装进系统 Python；服务自己的依赖在 /app/.venv，两边不混。
COPY runtime/requirements.txt runtime/matplotlibrc /opt/sandbox/
RUN pip install --no-cache-dir --require-hashes -r /opt/sandbox/requirements.txt

WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.16

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --no-cache

COPY --from=docker-cli /usr/local/bin/docker bin/docker
COPY app/ app/
# 沙箱里的普通用户进不了 /app：看不到服务的代码，也碰不到 docker 命令行。
RUN chmod 700 /app

EXPOSE 8095

# 用 .venv 解释器的完整路径启动，不把 .venv 放进 PATH：沙箱容器沿用这个镜像，里面的 python 得是系统的那个。
CMD ["/app/.venv/bin/python", "-m", "app.main"]
