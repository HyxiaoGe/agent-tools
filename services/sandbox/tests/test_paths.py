"""工作目录里的路径和文件操作：读写不能顺着软链接、`..` 落到工作目录外面。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from app import paths


@pytest.fixture
def workspace(tmp_path: Path):
    directory = tmp_path / "ws"
    directory.mkdir()
    fd = paths.open_root(directory)
    yield directory, fd
    os.close(fd)


@pytest.mark.parametrize("raw", ["a.txt", "outputs/图表.png", "uploads/销售 数据(1).xlsx", "a/b/c/d/e/f/g/h"])
def test_split_path_accepts_relative_paths(raw):
    assert "/".join(paths.split_path(raw)) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/etc/passwd",
        "../x",
        "a/../../x",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "a\nb",
        "a\x00b",
        "a/b/c/d/e/f/g/h/i",
        "x" * 256,
        "/".join(["d" * 100] * 6),
    ],
)
def test_split_path_rejects_anything_else(raw):
    with pytest.raises(paths.PathError) as error:
        paths.split_path(raw)
    assert error.value.code == "invalid_path"


def test_write_then_read_creates_missing_directories(workspace):
    directory, fd = workspace
    paths.write_file(fd, ("outputs", "报告", "a.txt"), b"hello", owner=None)
    assert (directory / "outputs" / "报告" / "a.txt").read_bytes() == b"hello"
    assert paths.read_file(fd, ("outputs", "报告", "a.txt"), 100) == b"hello"
    paths.write_file(fd, ("outputs", "报告", "a.txt"), b"again", owner=None)
    assert paths.read_file(fd, ("outputs", "报告", "a.txt"), 100) == b"again"


def test_read_refuses_symlinked_file(workspace, tmp_path):
    directory, fd = workspace
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (directory / "link").symlink_to(secret)
    with pytest.raises(paths.PathError) as error:
        paths.read_file(fd, ("link",), 100)
    assert error.value.code == "not_a_file"
    assert paths.sha256_file(fd, ("link",), 100) is None


def test_read_and_write_refuse_symlinked_directory(workspace, tmp_path):
    directory, fd = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    (directory / "dir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(paths.PathError) as error:
        paths.read_file(fd, ("dir", "secret.txt"), 100)
    assert error.value.code == "not_a_directory"
    with pytest.raises(paths.PathError):
        paths.write_file(fd, ("dir", "planted.txt"), b"x", owner=None)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


def test_write_replaces_a_symlink_instead_of_following_it(workspace, tmp_path):
    directory, fd = workspace
    target = tmp_path / "target.txt"
    target.write_text("keep")
    (directory / "a.txt").symlink_to(target)
    paths.write_file(fd, ("a.txt",), b"new", owner=None)
    assert target.read_text() == "keep"
    assert not (directory / "a.txt").is_symlink()
    assert (directory / "a.txt").read_bytes() == b"new"


def test_write_refuses_to_replace_a_directory(workspace):
    directory, fd = workspace
    (directory / "uploads").mkdir()
    with pytest.raises(paths.PathError) as error:
        paths.write_file(fd, ("uploads",), b"x", owner=None)
    assert error.value.code == "is_a_directory"
    assert (directory / "uploads").is_dir()


def test_read_refuses_fifo_without_blocking(workspace):
    directory, fd = workspace
    os.mkfifo(directory / "pipe")
    with pytest.raises(paths.PathError) as error:
        paths.read_file(fd, ("pipe",), 100)
    assert error.value.code == "not_a_file"


def test_read_refuses_directories_and_oversized_files(workspace):
    directory, fd = workspace
    (directory / "sub").mkdir()
    (directory / "big.bin").write_bytes(b"x" * 11)
    with pytest.raises(paths.PathError) as error:
        paths.read_file(fd, ("sub",), 100)
    assert error.value.code == "not_a_file"
    with pytest.raises(paths.PathError) as error:
        paths.read_file(fd, ("big.bin",), 10)
    assert error.value.code == "too_large"
    with pytest.raises(paths.PathError) as error:
        paths.read_file(fd, ("missing.txt",), 10)
    assert error.value.code == "not_found"
    with pytest.raises(paths.PathError) as error:
        paths.read_file(fd, ("nope", "missing.txt"), 10)
    assert error.value.code == "not_found"


def test_sha256_matches_content(workspace):
    directory, fd = workspace
    (directory / "a.bin").write_bytes(b"abc")
    assert paths.sha256_file(fd, ("a.bin",), 100) == hashlib.sha256(b"abc").hexdigest()
    assert paths.sha256_file(fd, ("a.bin",), 2) is None
    assert paths.sha256_file(fd, ("missing",), 100) is None


def test_delete_file(workspace):
    directory, fd = workspace
    (directory / "a.txt").write_text("x")
    (directory / "sub").mkdir()
    assert paths.delete_file(fd, ("a.txt",)) is True
    assert paths.delete_file(fd, ("a.txt",)) is False
    assert paths.delete_file(fd, ("nope", "a.txt")) is False
    with pytest.raises(paths.PathError):
        paths.delete_file(fd, ("sub",))


def test_delete_removes_the_link_not_its_target(workspace, tmp_path):
    directory, fd = workspace
    target = tmp_path / "target.txt"
    target.write_text("keep")
    (directory / "link").symlink_to(target)
    assert paths.delete_file(fd, ("link",)) is True
    assert target.read_text() == "keep"


def test_scan_lists_regular_files_and_never_follows_links(workspace, tmp_path):
    directory, fd = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"x" * 100_000)
    (directory / "outputs").mkdir()
    (directory / "outputs" / "a.txt").write_bytes(b"12345")
    (directory / "b.txt").write_bytes(b"1")
    (directory / "escape").symlink_to(outside, target_is_directory=True)
    (directory / "file-link").symlink_to(outside / "huge.bin")
    (directory / "bad\nname.txt").write_bytes(b"1")
    os.mkfifo(directory / "pipe")

    usage = paths.scan(fd, max_entries=100)

    assert sorted(usage.files) == ["b.txt", "outputs/a.txt"]
    assert usage.files["outputs/a.txt"].size == 5
    assert usage.entries == 7
    assert usage.truncated is False
    # 外面那个 100KB 的文件没被算进来
    assert usage.disk_bytes < 100_000


def test_scan_stops_at_entry_limit_and_depth_limit(workspace):
    directory, fd = workspace
    for index in range(20):
        (directory / f"f{index}.txt").write_text("x")
    assert paths.scan(fd, max_entries=10).truncated is True
    assert paths.scan(fd, max_entries=20).truncated is False

    deep = directory
    for _ in range(paths.MAX_SCAN_DEPTH + 2):
        deep = deep / "d"
        deep.mkdir()
    assert paths.scan(fd, max_entries=1000).truncated is True


def test_scan_does_not_hold_a_descriptor_per_directory(workspace):
    import resource

    directory, fd = workspace
    for index in range(300):
        (directory / f"d{index}").mkdir()
        (directory / f"d{index}" / "f.bin").write_bytes(b"x" * 4096)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
    try:
        usage = paths.scan(fd, max_entries=1000)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    # 300 个子目录里的文件一个不漏
    assert usage.truncated is False
    assert len(usage.files) == 300
    assert usage.disk_bytes >= 300 * 4096
