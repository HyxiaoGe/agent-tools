FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# 构建机可用 --build-arg 换 PyPI 镜像源（pip 直接读这个变量），默认官方源。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py direct_read.py quota.py mcp_server.py page_text.py url_safety.py ./
COPY scripts/ scripts/

# 进程段错误时把各线程的 Python 栈打到日志里（11-04 dev 上崩过一次，exit 139，没留下线索）
ENV PYTHONFAULTHANDLER=1

EXPOSE 8091

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8091", "--no-access-log"]
