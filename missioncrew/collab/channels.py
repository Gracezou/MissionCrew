"""频道的修改、归档与恢复:Web API 与 Agent Tool 共用同一套校验口径。

工作目录变更不需要停 Runtime:各 provider 复用会话前都比较解析后的
workdir,不一致就换新会话,所以新目录从下一轮执行起生效。归档要先停掉
频道的持久 Runtime 实例(原生 session 保留,恢复后可续接)。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from ..core.models import Channel
from ..core.store import Store

_UNSET = object()


class ChannelChangeError(ValueError):
    """频道当前状态不允许该修改;code 供 Agent Tool 返回结构化错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _ensure_editable(channel: Channel) -> None:
    if channel.content_kind:
        raise ChannelChangeError(
            "content_channel", "页面对话频道由平台按内容页维护，不能修改")
    if channel.archived:
        raise ChannelChangeError(
            "channel_archived", "频道已归档、只读，请先恢复再修改")


def ensure_channel_archivable(store: Store, channel: Channel) -> None:
    """general 不能归档或删除;仍有活动运行时先停止或等待本轮结束。"""
    if channel.is_general:
        raise ChannelChangeError(
            "general_channel", "general 是项目默认频道，不能归档或删除")
    if store.active_chat_runs(channel.id):
        raise ChannelChangeError(
            "in_use", "频道仍有 Agent 正在运行，请先停止或等待本轮结束")


def update_channel(store: Store, channel: Channel, *, actor: str,
                   name: Optional[str] = None, purpose: Optional[str] = None,
                   workdir=_UNSET) -> list[str]:
    """按给出的字段修改频道,返回实际变化的字段名。

    workdir 不传 = 保留;传空值 = 改回平台内置工作区。调用方负责目录本身的
    合法性(Agent 只能选项目代码仓,人类可填任意路径)。
    """
    _ensure_editable(channel)
    changed = []
    if name is not None:
        value = name.strip() or channel.id.rsplit(":", 1)[-1]
        if value != channel.name:
            channel.name = value
            changed.append("name")
    if purpose is not None and purpose != channel.purpose:
        channel.purpose = purpose
        changed.append("purpose")
    if workdir is not _UNSET:
        value = str(workdir or "").strip() or None
        if value != channel.workdir:
            channel.workdir = value
            changed.append("workdir")
    if changed:
        store.put_channel(channel)
        store.audit(actor, "channel_updated",
                    detail=f"channel={channel.id} fields={','.join(changed)}")
    return changed


def archive_channel(store: Store, channel: Channel, *, actor: str,
                    stop_sessions: Callable[[str], int]) -> int:
    """归档并停掉频道的持久 Runtime;已归档时直接返回 0。"""
    ensure_channel_archivable(store, channel)
    if channel.archived:
        return 0
    stopped = stop_sessions(channel.id)
    channel.archived = True
    channel.archived_at = time.time()
    store.put_channel(channel)
    store.audit(actor, "channel_archived",
                detail=f"channel={channel.id} stopped_runtimes={stopped}")
    return stopped


def restore_channel(store: Store, channel: Channel, *, actor: str) -> bool:
    """取消归档;返回是否发生了变化。"""
    if not channel.archived:
        return False
    channel.archived = False
    channel.archived_at = 0.0
    store.put_channel(channel)
    store.audit(actor, "channel_restored", detail=f"channel={channel.id}")
    return True
