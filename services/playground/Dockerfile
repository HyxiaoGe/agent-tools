FROM python:3.12-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir uv==0.11.16
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --no-cache
COPY app/ app/
RUN mkdir -p /app/storage && chown 65532:65532 /app/storage
USER 65532:65532
EXPOSE 8101
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8101", "--no-access-log"]
