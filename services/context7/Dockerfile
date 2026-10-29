FROM python:3.12-slim

WORKDIR /app

# 依赖按 uv.lock 安装，与测试时一致；uv 从 PyPI 装，不依赖 ghcr.io。
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir uv==0.11.16

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --no-cache

COPY app/ app/

EXPOSE 3335

CMD ["python", "-m", "app.server"]
