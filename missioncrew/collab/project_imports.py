"""项目间复制准则、Skill 与自动化。"""
from __future__ import annotations

import subprocess
import time
from collections.abc import Collection
from dataclasses import replace
from functools import partial

from ..core.config import projects_dir
from ..core.models import Automation, Project
from ..core.store import Store
from .agent_tools import AgentActionService
from .automations import normalize_automation_id
from .documents import guideline_library_for
from .guidelines import _project_lock as _guideline_lock
from .guidelines import save_guideline
from .skills import _project_lock as _skill_lock
from .skills import import_skill_folder, project_skill_library_dir


RESOURCE_TYPES = {"guideline", "skill", "automation"}
CONFLICT_STRATEGIES = {"skip", "overwrite"}


def _result(item_id: str, status: str, *, reason: str = "",
            target_id: str = "") -> dict:
    value = {"id": item_id, "status": status}
    if reason:
        value["reason"] = reason
    if target_id:
        value["target_id"] = target_id
    return value


def _failure_reason(exc: Exception) -> str:
    """逐条失败原因会返回前端：系统异常只保留错误类别，不带命令行与绝对路径。"""
    if isinstance(exc, subprocess.CalledProcessError):
        return "版本库写入失败"
    if isinstance(exc, OSError) and (exc.strerror or exc.filename):
        return f"文件操作失败：{exc.strerror or type(exc).__name__}"
    return str(exc)


def _copy_guideline(store: Store, source: Project, target: Project,
                    item_id: str, strategy: str, actor: str) -> dict:
    guideline = next(
        (item for item in source.guidelines if item.name == item_id), None)
    if guideline is None:
        return _result(item_id, "failed", reason="源项目准则不存在")
    with _guideline_lock(target.id):
        current = store.get_project(target.id) or target
        # save_guideline 内部会先同步准则库；这里不整库同步，只补看工作树里
        # 尚未进索引的同名文件，保证 skip 不会静默覆盖。
        try:
            guideline_library_for(target.id).read(f"{item_id}.md")
            file_exists = True
        except FileNotFoundError:
            file_exists = False
        exists = file_exists or any(
            item.name == item_id for item in current.guidelines)
        if exists and strategy == "skip":
            return _result(item_id, "skipped", reason="目标项目已有同名准则")
        save_guideline(
            store, current, guideline.render_markdown(), enabled=guideline.enabled,
            actor=actor, original_name=item_id if exists else "",
            message=f"Import guideline {item_id} from project {source.id}",
        )
    return _result(
        item_id, "overwritten" if exists else "copied", target_id=item_id)


def _copy_skill(store: Store, source: Project, target: Project,
                item_id: str, strategy: str, actor: str) -> dict:
    source_skill = next(
        (item for item in source.skills if item.id == item_id), None)
    if source_skill is None:
        return _result(item_id, "failed", reason="源项目 Skill 不存在")
    # 不调用源项目的扫描/版本 API：复制过程对源项目索引、目录与历史均只读。
    source_directory = projects_dir() / source.id / "skills" / item_id
    if not source_directory.is_dir() or source_directory.is_symlink():
        return _result(item_id, "failed", reason="源项目 Skill 目录不存在或不合法")

    # 冲突判断、导入与启停在同一把项目锁内完成；启停随导入后的扫描一起落定，
    # 停用的 Skill 不会先按默认启用写进上下文视图。
    with _skill_lock(target.id):
        current = store.get_project(target.id) or target
        target_directory = project_skill_library_dir(target.id) / item_id
        # 兼容尚未从旧 Project JSON 物化目录的 Skill：索引或目录任一存在即冲突。
        exists = (any(item.id == item_id for item in current.skills)
                  or target_directory.exists() or target_directory.is_symlink())
        if exists and strategy == "skip":
            return _result(item_id, "skipped", reason="目标项目已有同名 Skill")
        imported = import_skill_folder(
            store, current, str(source_directory), overwrite=exists, actor=actor,
            enabled_by_id={item_id: source_skill.enabled})
        # 目标库里其他 Skill 的扫描问题与本条无关；导入源就是该 Skill 目录，
        # 其自身问题以 "." 为相对路径上报。
        issues = [issue for issue in imported.get("issues", [])
                  if issue.startswith((f"{item_id}:", ".:"))]
        refreshed = store.get_project(target.id) or current
        if (item_id not in imported.get("imported", [])
                or not any(item.id == item_id for item in refreshed.skills)):
            return _result(item_id, "failed",
                           reason="；".join(issues) or "目标项目未能索引复制后的 Skill")
    return _result(
        item_id, "overwritten" if exists else "copied", target_id=item_id)


def _copy_automation(store: Store, source: Project, target: Project,
                     item_id: str, strategy: str, actor: str, *,
                     agent_tools: AgentActionService,
                     running_ids: Collection[str]) -> dict:
    source_id = normalize_automation_id(source.id, item_id)
    automation = store.get_automation(source_id)
    if automation is None or automation.project_id != source.id:
        return _result(item_id, "failed", reason="源项目自动化不存在")
    short_id = source_id.removeprefix(f"{source.id}:")
    target_id = normalize_automation_id(target.id, short_id)
    exists = store.get_automation(target_id) is not None
    if exists and strategy == "skip":
        return _result(item_id, "skipped", reason="目标项目已有同名自动化")
    if exists and target_id in running_ids:
        # 运行中的旧脚本结束时会回写运行状态，覆盖后无法保证状态干净。
        return _result(item_id, "failed", reason="目标自动化正在运行")

    # 复制的是定义而非执行身份/状态。replace 保留模型校验，再显式生成目标归属。
    copied = replace(
        automation, id=target_id, project_id=target.id, enabled=False,
        created_by_role_id="", last_run_at=0.0, last_status="",
    )
    # 目标项目中的复制条目是新定义，时间戳由目标存储重新维护。
    copied.created_at = copied.updated_at = time.time()
    copied = Automation.from_dict(copied.to_dict())
    if exists:
        # 与删除自动化同口径：回收旧脚本令牌并清掉旧运行记录，旧脚本不进回收站。
        agent_tools.revoke_automation_tokens(target_id)
        store.delete_automation(target_id)
    store.put_automation(copied)
    store.audit(
        actor, "automation_imported",
        detail=(f"source_project={source.id} target_project={target.id} "
                f"automation={target_id} overwrite={exists} enabled=false"),
    )
    return _result(
        item_id, "overwritten" if exists else "copied", target_id=target_id)


def import_project_resources(store: Store, *, source_project_id: str,
                             target_project_id: str, resource_type: str,
                             item_ids: list[str], agent_tools: AgentActionService,
                             running_automation_ids: Collection[str] = (),
                             conflict_strategy: str = "skip",
                             actor: str = "human") -> dict:
    """把源项目所选资源复制到目标项目，并逐条返回结果。

    ``running_automation_ids`` 由调用方从运行器取快照传入，collab 不反向依赖运行器。
    """
    if source_project_id == target_project_id:
        raise ValueError("源项目与目标项目不能相同")
    if resource_type not in RESOURCE_TYPES:
        raise ValueError("资源类型必须是 guideline、skill 或 automation")
    if conflict_strategy not in CONFLICT_STRATEGIES:
        raise ValueError("冲突策略必须是 skip 或 overwrite")
    source = store.get_project(source_project_id)
    target = store.get_project(target_project_id)
    if source is None:
        raise ValueError("源项目不存在")
    if target is None:
        raise ValueError("目标项目不存在")

    copier = {
        "guideline": _copy_guideline,
        "skill": _copy_skill,
        "automation": partial(
            _copy_automation, agent_tools=agent_tools,
            running_ids=frozenset(running_automation_ids)),
    }[resource_type]
    results = []
    seen: set[str] = set()
    for raw_id in item_ids:
        item_id = str(raw_id or "").strip()
        if not item_id:
            results.append(_result("", "failed", reason="条目标识不能为空"))
            continue
        if item_id in seen:
            results.append(_result(item_id, "skipped", reason="请求中重复选择"))
            continue
        seen.add(item_id)
        try:
            results.append(copier(
                store, source, target, item_id, conflict_strategy, actor))
        except (OSError, UnicodeError, ValueError,
                subprocess.CalledProcessError) as exc:
            results.append(_result(item_id, "failed", reason=_failure_reason(exc)))

    return {
        "source_project_id": source_project_id,
        "target_project_id": target_project_id,
        "resource_type": resource_type,
        "conflict_strategy": conflict_strategy,
        "results": results,
    }
