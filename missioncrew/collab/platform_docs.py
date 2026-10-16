"""把 MissionCrew 自身的说明文档提供给 Agent 按需检索。

事实源就是仓库 `docs/` 下给人读的那份 Markdown,不另写 Agent 版本;入口是
`docs/README.md`,逐篇列出文档并说明适用场景。装配工作区时把这些文件镜像到
数据目录的共享副本,各角色工作区以 `platform-docs/` 链接过去。

用副本而不是直接链接源码目录:工作区内的目录对 Runtime 是可写授权,副本即使被
改动也会在下一次装配时恢复,源码仓不会被 Agent 写入。从不含 `docs/` 的安装方式
运行时副本为空目录。
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

from ..core.config import mc_home

SOURCE_DIR = Path(__file__).resolve().parents[2] / "docs"
INDEX_NAME = "README.md"
_SYNC_LOCK = threading.Lock()


def platform_docs_dir() -> Path:
    return mc_home() / "platform-docs"


def _write_bytes(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_platform_docs() -> Path:
    """让共享副本与 `docs/*.md` 一致:内容不同才重写,多出的条目删除。"""
    target = platform_docs_dir()
    sources = ({path.name: path for path in SOURCE_DIR.glob("*.md") if path.is_file()}
               if SOURCE_DIR.is_dir() else {})
    # 多个角色并发装配共用这一个目录,串行化以免清理时删掉别人的临时文件
    with _SYNC_LOCK:
        target.mkdir(parents=True, exist_ok=True)
        for name, source in sources.items():
            content = source.read_bytes()
            destination = target / name
            if destination.is_file() and not destination.is_symlink():
                if destination.read_bytes() == content:
                    continue
            elif destination.exists() or destination.is_symlink():
                _remove(destination)
            _write_bytes(destination, content)
        for entry in target.iterdir():
            if entry.name not in sources:
                _remove(entry)
    return target


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
