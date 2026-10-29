FROM python:3.12-slim

WORKDIR /app

# 依赖按 uv.lock 安装，与测试时一致；uv 从 PyPI 装，不依赖 ghcr.io。
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
# 构建机可用 --build-arg 换 PyPI 镜像源（pip 直接读这个变量），默认官方源。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir uv==0.11.16

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --no-cache

COPY app/ app/

EXPOSE 3334

CMD ["python", "-m", "app.server"]
