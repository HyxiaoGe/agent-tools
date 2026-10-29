FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# 构建机可用 --build-arg 换 PyPI 镜像源（pip 直接读这个变量），默认官方源。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py direct_read.py ./
COPY scripts/ scripts/

EXPOSE 8091

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8091", "--no-access-log"]
