FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
# 构建机可用 --build-arg 换 PyPI 镜像源（pip 直接读这个变量），默认官方源。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir .

COPY app/ app/

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
