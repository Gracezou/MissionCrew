"""HTTP 与 Agent Tool 共用的角色配置校验和保存。"""
from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from ..core.models import MENTION_ID_RE, ROLE_ABILITIES, Role
from ..runtime import runtime_manager


class RoleTemplateInput(BaseModel):
    id: str
    # 复制到新项目时的初始启用状态;项目角色实时启停走独立端点。
    enabled: bool = True
    runtime_id: str
    model: str = ""
    effort: str = ""
    usage_linkage_enabled: bool = False
    manual_only: bool = False
    name: str = ""
    description: str = ""
    capabilities: list[str] = []
    preference: str = ""
    color: str = ""
    sort_order: int | None = None


class RoleInput(RoleTemplateInput):
    project_id: str


class RoleConfigError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def validate_role_config(store, body, discovered_catalog: Callable) -> None:
    if not body.id.strip() or not MENTION_ID_RE.fullmatch(body.id):
        raise RoleConfigError(400, "角色 id 只能包含字母、数字、下划线、连字符")
    bad = [c for c in body.capabilities if c not in ROLE_ABILITIES]
    if bad:
        raise RoleConfigError(
            400, f"未知能力选项: {bad}(可用: {', '.join(sorted(ROLE_ABILITIES))})")
    if not body.runtime_id.strip():
        raise RoleConfigError(400, "角色必须选择 runtime(定义角色时固定执行组合)")
    backend = store.get_backend(body.runtime_id)
    if backend is None:
        raise RoleConfigError(400, f"runtime 不存在: {body.runtime_id}")
    if not backend.enabled:
        raise RoleConfigError(400, f"runtime {body.runtime_id} 已停用,请先在 Runtime 设置页启用")
    known_models = set(backend.models)
    catalog = None
    if body.model and known_models and body.model not in known_models:
        catalog = discovered_catalog(backend)
        if body.model not in set(catalog[0]):
            raise RoleConfigError(
                400, f"模型 {body.model or '(CLI 默认)'} 不属于 runtime {body.runtime_id}")
    if body.effort:
        catalog = catalog if catalog is not None else discovered_catalog(backend)
        allowed = runtime_manager.effort_options(
            backend, body.model, catalog[1])
        if not allowed:
            raise RoleConfigError(400, f"runtime {body.runtime_id} 不支持 effort(推理力度)配置")
        if body.effort not in allowed:
            raise RoleConfigError(
                400, f"模型 {body.model or '(CLI 默认)'} 的 effort "
                     f"必须是 {'/'.join(allowed)} 之一")


def reject_manual_only_orchestrator(project, role: Role) -> None:
    if role.manual_only and project.orchestrator_role_id == role.id:
        raise RoleConfigError(
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
            raise RoleConfigError(400, f"项目不存在: {body.project_id}")
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
