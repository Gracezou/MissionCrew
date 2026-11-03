"""Task/频道级 Git worktree 生命周期。"""
from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

from ..core.models import Channel
from ..core.store import Store
from . import channels as channel_ops

_LOCK = threading.Lock()  # ponytail: 单进程锁覆盖本机服务的并发入口,多进程时改用文件锁
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class WorkspaceError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise WorkspaceError("git_failed", (result.stderr or result.stdout).strip())
    return result


def _repo_root(repo: Path) -> Path:
    if not repo.is_dir():
        raise WorkspaceError("repo_missing", f"项目仓库目录不存在: {repo}", 400)
    result = _git(repo, "rev-parse", "--show-toplevel")
    root = Path(result.stdout.strip()).resolve()
    if root != repo.resolve():
        raise WorkspaceError("not_repository_root", f"代码仓路径必须是 Git 仓库根目录: {repo}", 400)
    return root


def create(store: Store, project, actor: str, *, repo_path: str, name: str,
           base: str, branch: str, channel_id: str, channel_name: str,
           purpose: str = "", task_id: str = "", allow_dirty: bool = False) -> dict:
    """原子性尽力回滚创建物;回滚失败时错误会包含需人工处理的 worktree 路径。"""
    if not project.managed_worktrees_enabled:
        raise WorkspaceError("disabled", "项目已关闭受管 worktree 创建", 409)
    if not _ID_RE.fullmatch(name) or not _ID_RE.fullmatch(channel_id):
        raise WorkspaceError("invalid_arguments", "name 和 channel_id 只能含字母、数字、下划线、连字符", 400)
    repo = _repo_root(Path(repo_path).expanduser())
    base = base.strip()
    if not base or _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}", check=False).returncode:
        raise WorkspaceError("base_not_found", f"基线分支或提交不存在: {base}", 400)
    workdir = repo / ".worktrees" / name
    if workdir.exists():
        raise WorkspaceError("already_exists", f"worktree 目录已存在: {workdir}", 409)
    cid = f"{project.id}:{channel_id}"
    if store.get_channel(cid):
        raise WorkspaceError("already_exists", f"频道已存在: {channel_id}", 409)
    if store.get_task(task_id) is None and task_id:
        raise WorkspaceError("task_not_found", f"任务不存在: {task_id}", 404)
    if task_id:
        task = store.get_task(task_id)
        if task.project_id != project.id:
            raise WorkspaceError("task_not_found", f"任务不属于当前项目: {task_id}", 404)
        if any(c.managed_worktree and c.managed_worktree.get("task_id") == task_id
               for c in store.list_channels(project.id)):
            raise WorkspaceError("task_already_bound", f"任务已绑定受管 worktree: {task_id}", 409)
    status = _git(repo, "status", "--porcelain").stdout
    if status and not allow_dirty:
        raise WorkspaceError("dirty_repository", "项目仓库有未提交修改;传 allow_dirty=true 明确继续", 409)
    if not branch.strip() or branch.startswith("-"):
        raise WorkspaceError("invalid_arguments", "branch 必须是有效分支名", 400)
    if _git(repo, "check-ref-format", "--branch", branch, check=False).returncode == 0:
        pass
    else:
        raise WorkspaceError("invalid_arguments", f"无效分支名: {branch}", 400)
    if _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise WorkspaceError("already_exists", f"分支已存在: {branch}", 409)

    with _LOCK:
        # 锁内再次检查同名路径与频道,避免同一进程同时创建时出现半套资源。
        if workdir.exists() or store.get_channel(cid):
            raise WorkspaceError("already_exists", f"worktree 或频道已存在: {name}", 409)
        if task_id and any(
                c.managed_worktree and c.managed_worktree.get("task_id") == task_id
                for c in store.list_channels(project.id)):
            raise WorkspaceError("task_already_bound", f"任务已绑定受管 worktree: {task_id}", 409)
        made_worktree = False
        channel = None
        try:
            workdir.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, "worktree", "add", "-b", branch, str(workdir), base)
            made_worktree = True
            metadata = {"repo": str(repo), "name": name, "base": base,
                        "branch": branch, "task_id": task_id, "merged": False}
            channel = Channel(
                id=cid, name=channel_name or channel_id, project_id=project.id,
                purpose=purpose, workdir=str(workdir), created_by_role_id=actor,
                managed_worktree=metadata,
            )
            store.put_channel(channel)
            if task_id:
                task = store.get_task(task_id)
                task.channel_ids = list(dict.fromkeys([*task.channel_ids, cid]))
                store.put_task(task)
            store.audit(actor, "managed_worktree_created",
                        detail=f"project={project.id} channel={cid} branch={branch} base={base}")
        except Exception as exc:
            rollback_errors = []
            if channel is not None:
                try:
                    store.delete_channel(cid)
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if made_worktree:
                result = _git(repo, "worktree", "remove", str(workdir), check=False)
                if result.returncode:
                    rollback_errors.append((result.stderr or result.stdout).strip())
                result = _git(repo, "branch", "-D", branch, check=False)
                if result.returncode:
                    rollback_errors.append((result.stderr or result.stdout).strip())
            detail = f"创建失败，已回滚本次新建资源: {exc}"
            if rollback_errors:
                detail += f"；回滚未完成，请检查 {workdir}: {'; '.join(rollback_errors)}"
            raise WorkspaceError("create_failed", detail, 500) from exc
    return {"summary": f"已创建受管 worktree 频道 #{channel.name}",
            "channel": channel.to_dict(), "resource_url": f"/resources/{project.id}/channels/{channel_id}"}


def finish(store: Store, project, channel: Channel) -> dict:
    metadata = channel.managed_worktree
    if not metadata:
        raise WorkspaceError("not_managed", "频道未绑定受管 worktree", 409)
    repo = Path(metadata["repo"])
    branch, base = metadata["branch"], metadata["base"]
    if not repo.is_dir() or not channel.workdir or not Path(channel.workdir).is_dir():
        raise WorkspaceError("worktree_missing", "受管 worktree 或原仓库目录缺失", 409)
    if _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}", check=False).returncode:
        raise WorkspaceError("base_not_found", f"基线已不存在: {base}", 409)
    stats = _git(repo, "diff", "--stat", f"{base}...{branch}").stdout.strip()
    commits = _git(repo, "log", "--oneline", "--reverse", f"{base}..{branch}").stdout.splitlines()
    ahead = int(_git(repo, "rev-list", "--count", f"{base}..{branch}").stdout.strip() or 0)
    merged = _git(repo, "merge-base", "--is-ancestor", branch, base, check=False).returncode == 0
    metadata["merged"] = merged
    store.put_channel(channel)
    return {"summary": "已生成合并候选" if not merged else "分支已并入基线",
            "channel": channel.to_dict(), "diff_stat": stats,
            "commits": commits, "commits_ahead": ahead, "merged": merged,
            "suggested_command": "" if merged else f"git merge --no-ff {branch}"}


def status(channel: Channel) -> dict | None:
    metadata = channel.managed_worktree
    if not metadata:
        return None
    repo, base, branch = Path(metadata["repo"]), metadata["base"], metadata["branch"]
    if not repo.is_dir() or _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}", check=False).returncode:
        return {**metadata, "missing": True}
    ahead = _git(repo, "rev-list", "--count", f"{base}..{branch}", check=False)
    merged = _git(repo, "merge-base", "--is-ancestor", branch, base, check=False).returncode == 0
    return {**metadata, "commits_ahead": int(ahead.stdout.strip() or 0),
            "merged": merged, "missing": not bool(channel.workdir and Path(channel.workdir).is_dir())}


def remove(store: Store, project, channel: Channel, actor: str,
           stop_sessions=lambda _channel_id: 0) -> dict:
    metadata = channel.managed_worktree
    if not metadata:
        raise WorkspaceError("not_managed", "频道未绑定受管 worktree", 409)
    repo, branch, base = Path(metadata["repo"]), metadata["branch"], metadata["base"]
    if not repo.is_dir() or not channel.workdir or not Path(channel.workdir).is_dir():
        raise WorkspaceError("worktree_missing", "受管 worktree 或原仓库目录缺失，拒绝自动清理", 409)
    if store.active_chat_runs(channel.id):
        raise WorkspaceError("channel_in_use", "频道仍有活动运行，拒绝清理", 409)
    if _git(repo, "merge-base", "--is-ancestor", branch, base, check=False).returncode:
        raise WorkspaceError("not_merged", f"分支 {branch} 尚未并入 {base}，拒绝删除 worktree 和分支", 409)
    # 即使聊天 Run 已结束,频道仍可能持有可复用 Runtime;先停 Runtime 再删它的 cwd。
    stop_sessions(channel.id)
    _git(repo, "worktree", "remove", str(channel.workdir))
    _git(repo, "branch", "-d", branch)
    channel_ops.archive_channel(store, channel, actor=actor,
                                stop_sessions=lambda _channel_id: 0)
    store.audit(actor, "managed_worktree_removed",
                detail=f"project={project.id} channel={channel.id} branch={branch}")
    return {"summary": f"已清理 worktree、分支并归档频道 #{channel.name}",
            "channel": channel.to_dict(), "removed": True}
