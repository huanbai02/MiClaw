"""集中管理当前单一 Markdown 用户画像的 filesystem IO。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

from .memory import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryScopeKind,
    MemorySource,
)


USER_PROFILE_FILENAME = "user_profile.md"
USER_PROFILE_MEMORY_ID = "user-profile"


class UserProfilePersistenceError(RuntimeError):
    """表示 profile 持久化失败，避免向调用方暴露路径或内容。"""


@dataclass(frozen=True)
class UserProfileStore:
    """读写固定 profile 文件，不缓存内容也不定义通用 Memory semantics。"""

    profile_path: Path | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_path", Path(self.profile_path))

    def read_profile(self) -> str | None:
        """读取当前完整 profile；缺失或空文件返回 None。

        Returns:
            去除首尾空白后的 profile；不存在或为空时返回 None。
        """
        record = self.read_record()
        return record.content if record else None

    def read_record(self) -> MemoryRecord | None:
        """读取当前 profile 并映射为固定语义的全局 MemoryRecord。

        Returns:
            存在且非空时返回用户画像 record；否则返回 None。
        """
        if not self.profile_path.exists():
            return None
        content = self.profile_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            return None
        return MemoryRecord(
            memory_id=USER_PROFILE_MEMORY_ID,
            kind=MemoryKind.USER_PROFILE,
            scope=MemoryScope(MemoryScopeKind.GLOBAL),
            source=MemorySource.USER_PROFILE_STORE,
            content=content,
        )

    def write_profile(self, content: str) -> None:
        """以同目录临时文件和原子 replace 覆盖写入 profile。

        Args:
            content: 要保存的完整 Markdown profile 文本。

        Raises:
            UserProfilePersistenceError: 创建、写入或替换 profile 失败时抛出。
        """
        temp_path: Path | None = None
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.profile_path.parent,
                prefix=f".{self.profile_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(content)
                temp_file.flush()

            os.replace(temp_path, self.profile_path)
            temp_path = None
        except OSError:
            raise UserProfilePersistenceError("user_profile_write_failed") from None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def get_user_profile_store(memory_dir: Path | str) -> UserProfileStore:
    """根据当前配置的 memory directory 创建 profile store。

    Args:
        memory_dir: 当前 runtime 配置使用的 Memory root。
    """
    return UserProfileStore(Path(memory_dir) / USER_PROFILE_FILENAME)
