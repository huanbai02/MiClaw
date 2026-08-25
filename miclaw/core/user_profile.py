"""集中管理当前单一 Markdown 用户画像的 filesystem IO。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


USER_PROFILE_FILENAME = "user_profile.md"


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
        if not self.profile_path.exists():
            return None
        content = self.profile_path.read_text(encoding="utf-8", errors="ignore").strip()
        return content or None

    def write_profile(self, content: str) -> None:
        """以 UTF-8 整文件覆盖写入 profile，并在需要时创建父目录。

        Args:
            content: 要保存的完整 Markdown profile 文本。
        """
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.profile_path.write_text(content, encoding="utf-8")


def get_user_profile_store(memory_dir: Path | str) -> UserProfileStore:
    """根据当前配置的 memory directory 创建 profile store。

    Args:
        memory_dir: 当前 runtime 配置使用的 Memory root。
    """
    return UserProfileStore(Path(memory_dir) / USER_PROFILE_FILENAME)
