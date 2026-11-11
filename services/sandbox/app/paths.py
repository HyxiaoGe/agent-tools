"""工作目录里的路径规则和文件操作。

沙箱里跑的是不可信的代码，它能在工作目录里放软链接、管道、很深的目录。服务自己以 root 运行，还拿着
Docker 的控制入口，所以这里的读写一律从工作目录的目录 fd 出发逐级打开（openat），每一级都不跟随软链接，
只认真目录和普通文件：读写落不到工作目录外面。
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
PRODUCT_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")

MAX_PATH_BYTES = 512
MAX_NAME_BYTES = 255
MAX_DEPTH = 8
# 扫描工作目录时最多下到多深；更深的不再往下走，按“超限”处理（否则藏在深处的大文件算不进用量）。
MAX_SCAN_DEPTH = 32

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# 中间某一级是软链接或不是目录时，不同系统回的错误码不一样。
_NOT_A_DIRECTORY = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)
_READ_CHUNK = 1 << 20


class PathError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def split_path(raw: str) -> tuple[str, ...]:
    """把调用方给的相对路径拆成一级一级的名字；不合规的直接拒绝，不做“修正”。"""
    if not raw or len(raw.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES or raw.startswith("/"):
        raise PathError("invalid_path", "Path must be a relative path inside the workspace.")
    parts = tuple(raw.split("/"))
    if len(parts) > MAX_DEPTH:
        raise PathError("invalid_path", "Path is nested too deeply.")
    for part in parts:
        if part in ("", ".", ".."):
            raise PathError("invalid_path", "Path must not contain empty, '.' or '..' segments.")
        try:
            encoded = part.encode("utf-8")
        except UnicodeEncodeError:
            raise PathError("invalid_path", "Path must be valid UTF-8.") from None
        if len(encoded) > MAX_NAME_BYTES:
            raise PathError("invalid_path", "A path segment is too long.")
        if any(ord(char) < 32 or char in "\x7f\\" for char in part):
            raise PathError("invalid_path", "Path must not contain control characters or backslashes.")
    return parts


def is_clean_path(raw: str) -> bool:
    try:
        split_path(raw)
    except PathError:
        return False
    return True


def open_root(path: str | os.PathLike[str]) -> int:
    """打开工作目录本身；它必须是真目录，不能是软链接。"""
    return os.open(path, _DIR_FLAGS)


def _open_dir(name: str, dir_fd: int) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in _NOT_A_DIRECTORY:
            raise PathError("not_a_directory", "A path segment is not a directory.") from None
        raise


@contextmanager
def _parent(root_fd: int, parts: tuple[str, ...], *, create: bool, owner: tuple[int, int] | None) -> Generator[int]:
    """逐级走到最后一级的上级目录，给出它的 fd。create 时缺的目录补上，归 owner。"""
    fd = os.dup(root_fd)
    try:
        for name in parts[:-1]:
            try:
                child = _open_dir(name, fd)
            except FileNotFoundError:
                if not create:
                    raise PathError("not_found", "No such file.") from None
                os.mkdir(name, 0o755, dir_fd=fd)
                child = _open_dir(name, fd)
                os.fchmod(child, 0o755)
                if owner is not None:
                    os.fchown(child, *owner)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _open_regular(root_fd: int, parts: tuple[str, ...]) -> tuple[int, os.stat_result]:
    """只读打开一个普通文件。软链接、目录、管道、套接字一律不算文件。"""
    with _parent(root_fd, parts, create=False, owner=None) as parent:
        try:
            # O_NONBLOCK：万一是个管道，打开不会卡住，下面按“不是普通文件”拒掉。
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            raise PathError("not_found", "No such file.") from None
        except OSError as exc:
            if exc.errno in (*_NOT_A_DIRECTORY, errno.ENXIO, errno.EOPNOTSUPP):
                raise PathError("not_a_file", "Not a regular file.") from None
            raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise PathError("not_a_file", "Not a regular file.")
    return fd, info


def read_file(root_fd: int, parts: tuple[str, ...], max_bytes: int) -> bytes:
    fd, info = _open_regular(root_fd, parts)
    try:
        if info.st_size > max_bytes:
            raise PathError("too_large", "File is too large to fetch.")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(_READ_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise PathError("too_large", "File is too large to fetch.")
        return data
    finally:
        os.close(fd)


def sha256_file(root_fd: int, parts: tuple[str, ...], max_bytes: int) -> str | None:
    """算内容摘要；文件不在了、不是普通文件或太大时回 None。"""
    try:
        fd, info = _open_regular(root_fd, parts)
    except (PathError, OSError):
        return None
    try:
        if info.st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        while chunk := os.read(fd, _READ_CHUNK):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def write_file(root_fd: int, parts: tuple[str, ...], data: bytes, *, owner: tuple[int, int] | None) -> None:
    """写一个文件（已有的换掉）。原位置是软链接或管道也只是把那个名字删掉重建，不会顺着它写出去。"""
    with _parent(root_fd, parts, create=True, owner=owner) as parent:
        name = parts[-1]
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        except (IsADirectoryError, PermissionError):  # 目录：Linux 回 EISDIR，macOS 回 EPERM
            raise PathError("is_a_directory", "A directory already exists at this path.") from None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(name, flags, 0o644, dir_fd=parent)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchmod(fd, 0o644)
        if owner is not None:
            os.fchown(fd, *owner)
    finally:
        os.close(fd)


def delete_file(root_fd: int, parts: tuple[str, ...]) -> bool:
    try:
        with _parent(root_fd, parts, create=False, owner=None) as parent:
            try:
                os.unlink(parts[-1], dir_fd=parent)
            except FileNotFoundError:
                return False
            except (IsADirectoryError, PermissionError):
                raise PathError("is_a_directory", "Path is a directory.") from None
    except PathError as exc:
        if exc.code == "not_found":
            return False
        raise
    return True


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class Usage:
    # 路径合规的普通文件；名字里有控制字符之类的只计入用量，不列出来。
    files: dict[str, FileEntry]
    # 所有目录项的个数（目录、软链接、其他东西都算）。
    entries: int
    # 实际占用的磁盘字节数（按块算，稀疏文件不虚高）。
    disk_bytes: int
    # 没扫完：目录项太多或目录太深。调用方按超限处理。
    truncated: bool


def _open_path(root_fd: int, parts: tuple[str, ...]) -> int:
    """从工作目录逐级打开一个子目录，回它的 fd。"""
    fd = os.dup(root_fd)
    try:
        for name in parts:
            child = _open_dir(name, fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def scan(root_fd: int, *, max_entries: int) -> Usage:
    """扫一遍工作目录。不跟随软链接；软链接只算一个目录项。"""
    files: dict[str, FileEntry] = {}
    entries = 0
    disk_bytes = 0
    # 还没扫的目录只记路径，轮到时再从工作目录逐级打开：同时开着的 fd 不随目录个数涨
    # （一个目录下几千个子目录时，把它们的 fd 都攒着会撞上进程的 fd 上限，后面的就漏扫了）。
    pending: list[tuple[str, ...]] = [()]
    while pending:
        parts = pending.pop()
        try:
            fd = _open_path(root_fd, parts)
        except (FileNotFoundError, PathError):
            # 扫的过程中被删了，或换成了别的东西。
            continue
        except OSError:
            # 打不开又说不清为什么：用量算不准，按超限处理。
            return Usage(files, entries, disk_bytes, truncated=True)
        try:
            prefix = "".join(f"{name}/" for name in parts)
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > max_entries:
                        return Usage(files, entries, disk_bytes, truncated=True)
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    disk_bytes += info.st_blocks * 512
                    path = prefix + entry.name
                    if stat.S_ISDIR(info.st_mode):
                        if len(parts) + 1 >= MAX_SCAN_DEPTH:
                            return Usage(files, entries, disk_bytes, truncated=True)
                        pending.append((*parts, entry.name))
                    elif stat.S_ISREG(info.st_mode) and is_clean_path(path):
                        files[path] = FileEntry(path, info.st_size, info.st_mtime_ns)
        finally:
            os.close(fd)
    return Usage(files, entries, disk_bytes, truncated=False)
