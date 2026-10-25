"""HTTP 与 Agent Tool 共用的角色配置校验和保存。"""
from __future__ import annotations

import re
from collections.abc import Callable

from fastapi import HTTPException

from ..core.models import ROLE_ABILITIES, Role
from ..runtime import runtime_manager


def validate_role_config(store, body, discovered_catalog: Callable) -> None:
    if not body.id.strip() or not re.fullmatch(r"[\w-]+", body.id):
        raise HTTPException(400, "角色 id 只能包含字母、数字、下划线、连字符")
    bad = [c for c in body.capabilities if c not in ROLE_ABILITIES]
    if bad:
        raise HTTPException(
            400, f"未知能力选项: {bad}(可用: {', '.join(sorted(ROLE_ABILITIES))})")
    if not body.runtime_id.strip():
        raise HTTPException(400, "角色必须选择 runtime(定义角色时固定执行组合)")
    backend = store.get_backend(body.runtime_id)
    if backend is None:
        raise HTTPException(400, f"runtime 不存在: {body.runtime_id}")
    if not backend.enabled:
        raise HTTPException(400, f"runtime {body.runtime_id} 已停用,请先在 Runtime 设置页启用")
    known_models = set(backend.models)
    if body.model and known_models and body.model not in known_models:
        if body.model not in set(discovered_catalog(backend)[0]):
            raise HTTPException(
                400, f"模型 {body.model or '(CLI 默认)'} 不属于 runtime {body.runtime_id}")
    if body.effort:
        allowed = runtime_manager.effort_options(
            backend, body.model, discovered_catalog(backend)[1])
        if not allowed:
            raise HTTPException(400, f"runtime {body.runtime_id} 不支持 effort(推理力度)配置")
        if body.effort not in allowed:
            raise HTTPException(
                400, f"模型 {body.model or '(CLI 默认)'} 的 effort "
                     f"必须是 {'/'.join(allowed)} 之一")


def reject_manual_only_orchestrator(project, role: Role) -> None:
    if role.manual_only and project.orchestrator_role_id == role.id:
        raise HTTPException(
            409, f"主控角色 @{role.id} 不能设为仅人工点名；请先为项目选择其他主控")


def save_role_config(store, body, scope: str, discovered_catalog: Callable,
                     actor: str, *, role_updated: Callable | None = None,
                     audit_suffix: str = "") -> dict:
    """与 HTTP 保存语义一致：项目 enabled 只由独立启停端点修改。"""
    validate_role_config(store, body, discovered_catalog)
    data = body.model_dump()
    if scope == "project":
        project = store.get_project(body.project_id)
        if project is None:
            raise HTTPException(400, f"项目不存在: {body.project_id}")
        existing = store.get_role(body.project_id, body.id)
        data["enabled"] = existing.enabled if existing else True
        siblings = store.list_roles(body.project_id)
    elif scope == "template":
        data["project_id"] = ""
        existing = store.get_role_template(body.id)
        if "enabled" not in body.model_fields_set and existing:
            data["enabled"] = existing.enabled
        siblings = store.list_role_templates()
    else:
        raise ValueError(f"未知角色范围: {scope}")
    # fork-divergence: 旧请求省略限制字段时保留现值。
    if "manual_only" not in body.model_fields_set and existing:
        data["manual_only"] = existing.manual_only
    if data["sort_order"] is None:
        data["sort_order"] = existing.sort_order if existing else 10 + max(
            (role.sort_order for role in siblings), default=0)
    role = Role(**data)
    if scope == "project":
        reject_manual_only_orchestrator(project, role)
        store.put_role(role)
        if role_updated:
            role_updated(role, previous=existing)
        detail = f"project={role.project_id} role={role.id}"
        action = "role_saved"
        result = store.get_role(role.project_id, role.id).to_dict()
    else:
        store.put_role_template(role)
        detail = f"role={role.id}"
        action = "role_template_saved"
        result = role.to_dict()
    store.audit(actor, action, detail=f"{detail}{audit_suffix}")
    return result
