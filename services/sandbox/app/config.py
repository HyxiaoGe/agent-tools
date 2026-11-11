from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = 8095

    # 工作目录放在这里（服务容器里的路径）。跑命令的容器是宿主的 Docker 起的，挂载要用宿主上的路径：
    # sandbox_host_data_dir 留空时，启动时从自己这个容器的挂载信息里查 /data 对应的宿主目录。
    sandbox_data_dir: str = "/data"
    sandbox_host_data_dir: str = ""

    docker_bin: str = "docker"
    # 跑命令用的镜像：就是本服务的镜像（里面预装了 Python 和数据分析的包），compose 传入当前部署的版本。
    sandbox_image: str = "agent-tools/sandbox:current"
    # 隔离用的容器运行时。改成 runc 就没有 gVisor 这一层，只能在本机调试用。
    sandbox_runtime: str = "runsc"
    # 命令在容器里用这个身份跑（镜像里建好的普通用户），工作目录归它。
    sandbox_uid: int = 10001
    sandbox_gid: int = 10001

    # 每条命令的上限。进程数有两道：nproc 管的是沙箱里看得到的进程（准），pids 管的是宿主上的线程
    # （gVisor 自己空转就占 40 个左右，只当兜底，设得宽）。
    sandbox_memory_mb: int = Field(default=1024, ge=64)
    sandbox_cpus: float = Field(default=1.0, gt=0)
    sandbox_tmp_mb: int = Field(default=256, ge=16)
    sandbox_nproc: int = Field(default=128, ge=8)
    sandbox_pids_limit: int = Field(default=512, ge=64)
    sandbox_nofile: int = Field(default=1024, ge=64)
    # 单个文件最大多少；和下面的工作目录总量一起防写满磁盘。
    sandbox_file_size_mb: int = Field(default=256, ge=1)

    default_timeout_seconds: int = Field(default=60, ge=1)
    max_timeout_seconds: int = Field(default=120, ge=1)
    # 标准输出、标准错误各留这么多字节（开头、结尾各一半），多的丢掉。
    output_limit_bytes: int = Field(default=64 * 1024, ge=1024)

    # 同时最多跑几条命令；满了的排队等，等这么久还轮不到就回 503。
    max_concurrent_runs: int = Field(default=1, ge=1)
    queue_wait_seconds: float = Field(default=20, ge=0)

    # 一个工作目录最多占多少磁盘、多少个目录项；跑的过程中超了就杀掉。
    workspace_limit_mb: int = Field(default=1024, ge=1)
    workspace_max_entries: int = Field(default=5000, ge=10)
    # 经接口放进来、取出去的单个文件上限（网关另有 10 MiB 的请求体上限）。
    max_file_mb: int = Field(default=20, ge=1)
    # 这么多天没用过的工作目录删掉。
    retention_days: int = Field(default=7, ge=1)
