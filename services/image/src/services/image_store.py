"""/v2/mcp 出的图：文件放在私有目录（不在静态目录下），每张在 sqlite 里记一行，归出图的产品。

- 只有所属产品能取、能删；别人的和不存在的一样当成没有，不透露有没有这张图。
- 不自动清理：产品删对话时按 id 删（DELETE /v1/images/{id}）。
- 每张图只属于一次生成：缓存命中时复制一份、给新 id（见 generator），删哪张都不影响别的。
- 文件先写临时名再改名，读写都放到线程里，不卡事件循环。
- id 和旧的静态图一样是 uuid4；文件名是 <id>.<扩展名>。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from anyio import to_thread

from src.models.response import StoredImage
from src.services.references import _sniff_mime, image_size

logger = structlog.get_logger()

_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class UnsupportedImage(ValueError):
    """上游给的不是认得出的 PNG / JPEG / WebP，或者读不出宽高。"""


@dataclass(frozen=True)
class ImageRecord:
    id: str
    app_id: str
    file: str
    mime_type: str
    width: int
    height: int
    bytes: int
    model: str
    edited_from: str | None
    created_at: str

    def public(self) -> StoredImage:
        return StoredImage(**{k: v for k, v in asdict(self).items() if k not in {"app_id", "file"}})


def valid_id(image_id: str) -> bool:
    return isinstance(image_id, str) and bool(_ID.fullmatch(image_id))


class ImageStore:
    def __init__(self, directory: str | Path, db_path: str | Path) -> None:
        self.directory = Path(directory)
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._initialized = False

    # ---- sqlite（同进程串行；单个 worker） ----

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        if not self._initialized:
            self.directory.mkdir(parents=True, exist_ok=True)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, app_id TEXT NOT NULL, file TEXT NOT NULL, mime_type TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL, bytes INTEGER NOT NULL, model TEXT NOT NULL,
                    edited_from TEXT, created_at TEXT NOT NULL
                )
            """)
            db.commit()
            self._initialized = True
        return db

    def _run(self, fn):  # noqa: ANN001, ANN202
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            db = self._connect()
            try:
                return fn(db)
            finally:
                db.close()

    def _get(self, app_id: str, image_id: str) -> ImageRecord | None:
        if not valid_id(image_id):
            return None
        row = self._run(
            lambda db: db.execute("SELECT * FROM images WHERE id = ? AND app_id = ?", (image_id, app_id)).fetchone()
        )
        return ImageRecord(**dict(row)) if row else None

    def _save(self, app_id: str, data: bytes, model: str, edited_from: str | None) -> ImageRecord:
        mime = _sniff_mime(data)
        size = image_size(data) if mime else None
        if mime is None or size is None or min(size) <= 0:
            raise UnsupportedImage("not a PNG, JPEG or WebP image with a readable size")
        image_id = str(uuid.uuid4())
        record = ImageRecord(
            id=image_id,
            app_id=app_id,
            file=f"{image_id}.{EXTENSIONS[mime]}",
            mime_type=mime,
            width=size[0],
            height=size[1],
            bytes=len(data),
            model=model,
            edited_from=edited_from,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.directory / record.file
        tmp = self.directory / f".{record.file}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, final)
        try:
            self._run(
                lambda db: (
                    db.execute(
                        "INSERT INTO images VALUES (:id, :app_id, :file, :mime_type, :width, :height, :bytes, :model,"
                        " :edited_from, :created_at)",
                        asdict(record),
                    ),
                    db.commit(),
                )
            )
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        logger.info("owned_image_saved", id=record.id, app_id=app_id, size=record.bytes)
        return record

    def _read(self, record: ImageRecord) -> bytes | None:
        try:
            return (self.directory / record.file).read_bytes()
        except FileNotFoundError:
            return None

    def _delete(self, app_id: str, image_id: str) -> bool:
        record = self._get(app_id, image_id)
        if record is None:
            return False
        # 先删记录：删到一半时宁可留个没人指着的文件，也不留指着空文件的记录
        self._run(
            lambda db: (db.execute("DELETE FROM images WHERE id = ? AND app_id = ?", (image_id, app_id)), db.commit())
        )
        try:
            (self.directory / record.file).unlink(missing_ok=True)
        except OSError:
            logger.warning("owned_image_unlink_failed", id=image_id)
        logger.info("owned_image_deleted", id=image_id, app_id=app_id)
        return True

    # ---- 对外：都在线程里跑 ----

    def path(self, record: ImageRecord) -> Path:
        return self.directory / record.file

    async def get(self, app_id: str, image_id: str) -> ImageRecord | None:
        """本产品的这张图；别的产品的、不存在的、id 格式不对的都是 None。"""
        return await to_thread.run_sync(self._get, app_id, image_id)

    async def save(self, *, app_id: str, data: bytes, model: str, edited_from: str | None = None) -> ImageRecord:
        return await to_thread.run_sync(self._save, app_id, data, model, edited_from)

    async def read(self, record: ImageRecord) -> bytes | None:
        """文件内容；记录还在但文件没了是 None。"""
        return await to_thread.run_sync(self._read, record)

    async def copy(self, image_id: str, *, app_id: str, edited_from: str | None) -> ImageRecord | None:
        """缓存命中：把本产品的那张复制一份、给新 id。原图（或文件）已经删了是 None，按没命中处理。"""
        original = await self.get(app_id, image_id)
        data = await self.read(original) if original else None
        if original is None or data is None:
            return None
        return await self.save(app_id=app_id, data=data, model=original.model, edited_from=edited_from)

    async def delete(self, app_id: str, image_id: str) -> bool:
        """删本产品的这张图；不是本产品的、已经删了的什么都不做，返回 False。"""
        return await to_thread.run_sync(self._delete, app_id, image_id)
