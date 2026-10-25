"""角色端点:定义时固定 runtime/model 的角色 CRUD。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.recycle_bin import recycle_role
from ..collab.role_config import (reject_manual_only_orchestrator,
                                  save_role_config, validate_role_config)
from ..core.models import Role
from .context import ApiContext
from .schemas import (RoleEnabledInput, RoleImport, RoleInput, RoleReorder,
                      RoleTemplateImport, RoleTemplateInput, RoleTemplateReorder)


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    def validate_import_roles(
            roles: list[RoleTemplateInput], existing_ids: set[str],
            overwrite_ids: list[str]) -> set[str]:
        """先完整校验批次，再写库；覆盖名单必须与当前数据一致。"""
        ids = [role.id for role in roles]
        if len(ids) != len(set(ids)):
            raise HTTPException(400, "导入列表包含重复角色 id")
        acknowledged = set(overwrite_ids)
        if not acknowledged <= set(ids):
            raise HTTPException(400, "overwrite_ids 只能包含本次导入的角色 id")
        conflicts = set(ids) & existing_ids
        unconfirmed = conflicts - acknowledged
        if unconfirmed:
            names = ", ".join(f"@{role_id}" for role_id in sorted(unconfirmed))
            raise HTTPException(409, f"以下角色会覆盖现有配置，请确认后重试: {names}")
        for role in roles:
            validate_role_config(store, role, ctx.discovered_catalog)
        return conflicts

    @app.get("/api/roles")
    def roles():
        return [r.to_dict() for r in store.list_roles()]

    @app.post("/api/roles")
    def save_role(body: RoleInput):
        return save_role_config(store, body, "project", ctx.discovered_catalog,
                                "human", role_updated=ctx.role_usage_linkage.role_updated)

    @app.post("/api/roles/{role_id}/enabled")
    def set_role_enabled(role_id: str, body: RoleEnabledInput):
        project = ctx.must_project(body.project_id)
        role = store.get_role(project.id, role_id)
        if role is None:
            raise HTTPException(404, "角色不存在")
        if not body.enabled and project.orchestrator_role_id == role.id:
            raise HTTPException(409, "不能停用项目主控角色；请先为项目选择其他已启用主控")
        role = store.set_role_enabled_manually(
            project.id, role.id, body.enabled)
        store.audit(
            "human", "role_enabled_changed",
            detail=f"project={project.id} role={role.id} enabled={str(role.enabled).lower()}",
        )
        return role.to_dict()

    @app.post("/api/roles/reorder")
    def reorder_roles(body: RoleReorder):
        ctx.must_project(body.project_id)
        current = {r.id: r for r in store.list_roles(body.project_id)}
        if set(body.ids) != set(current) or len(body.ids) != len(current):
            raise HTTPException(400, "ids 必须恰好包含该项目的全部角色,不重不漏")
        for i, rid in enumerate(body.ids):
            current[rid].sort_order = (i + 1) * 10
            store.put_role(current[rid])
        store.audit("human", "roles_reordered",
                    detail=f"project={body.project_id} order={','.join(body.ids)}")
        return {"ok": True, "ids": body.ids}

    @app.post("/api/roles/import")
    def import_roles(body: RoleImport):
        project = ctx.must_project(body.project_id)
        current = {role.id: role for role in store.list_roles(project.id)}
        overwritten = validate_import_roles(
            body.roles, set(current), body.overwrite_ids)
        next_order = 10 + max((role.sort_order for role in current.values()), default=0)
        imported: list[str] = []
        prepared: list[tuple[Role, Role | None]] = []
        for item in body.roles:
            previous = current.get(item.id)
            data = item.model_dump(exclude={"sort_order", "enabled"})
            # fork-divergence: 旧文件未携带时不撤掉已有角色的仅人工限制。
            if "manual_only" not in item.model_fields_set and previous:
                data["manual_only"] = previous.manual_only
            role = Role(
                **data,
                project_id=project.id,
                # 启停是项目的实时运行状态，不随配置文件迁移。
                enabled=previous.enabled if previous else True,
                sort_order=previous.sort_order if previous else next_order,
            )
            reject_manual_only_orchestrator(project, role)
            prepared.append((role, previous))
            if previous is None:
                next_order += 10
        # 全部按生效配置校验通过后再写入，拒绝后续条目时不留下半批数据。
        for role, previous in prepared:
            store.put_role(role)
            ctx.role_usage_linkage.role_updated(role, previous=previous)
            imported.append(role.id)
        store.audit(
            "human", "roles_imported",
            detail=(f"project={project.id} imported={','.join(imported)} "
                    f"overwritten={','.join(sorted(overwritten))}"),
        )
        return {
            "ok": True,
            "imported_ids": imported,
            "overwritten_ids": sorted(overwritten),
        }

    @app.delete("/api/roles/{role_id}")
    def delete_role(role_id: str, project_id: str):
        if store.get_role(project_id, role_id) is None:
            raise HTTPException(404, "角色不存在")
        project = ctx.must_project(project_id)
        if project.orchestrator_role_id == role_id:
            raise HTTPException(409, "不能删除项目主控角色；请先为项目选择其他主控")
        role = store.get_role(project_id, role_id)
        item = recycle_role(store, project, role, actor="human")
        return {"ok": True, "recycle_item": item}

    @app.get("/api/role-templates")
    def role_templates():
        return [role.to_dict() for role in store.list_role_templates()]

    @app.post("/api/role-templates")
    def save_role_template(body: RoleTemplateInput):
        return save_role_config(store, body, "template", ctx.discovered_catalog, "human")

    @app.post("/api/role-templates/reorder")
    def reorder_role_templates(body: RoleTemplateReorder):
        current = {role.id: role for role in store.list_role_templates()}
        if set(body.ids) != set(current) or len(body.ids) != len(current):
            raise HTTPException(400, "ids 必须恰好包含全部全局角色模板,不重不漏")
        for i, role_id in enumerate(body.ids):
            current[role_id].sort_order = (i + 1) * 10
            store.put_role_template(current[role_id])
        store.audit("human", "role_templates_reordered",
                    detail=f"order={','.join(body.ids)}")
        return {"ok": True, "ids": body.ids}

    @app.post("/api/role-templates/import")
    def import_role_templates(body: RoleTemplateImport):
        current = {role.id: role for role in store.list_role_templates()}
        overwritten = validate_import_roles(
            body.roles, set(current), body.overwrite_ids)
        next_order = 10 + max((role.sort_order for role in current.values()), default=0)
        imported: list[str] = []
        for item in body.roles:
            previous = current.get(item.id)
            data = item.model_dump(exclude={"sort_order"})
            # fork-divergence: 旧导出文件不能静默清除仅人工限制。
            if "manual_only" not in item.model_fields_set and previous:
                data["manual_only"] = previous.manual_only
            if "enabled" not in item.model_fields_set and previous:
                data["enabled"] = previous.enabled   # 文件未携带时不改已有模板
            role = Role(
                **data,
                project_id="",
                sort_order=previous.sort_order if previous else next_order,
            )
            if previous is None:
                next_order += 10
            store.put_role_template(role)
            imported.append(role.id)
        store.audit(
            "human", "role_templates_imported",
            detail=(f"imported={','.join(imported)} "
                    f"overwritten={','.join(sorted(overwritten))}"),
        )
        return {
            "ok": True,
            "imported_ids": imported,
            "overwritten_ids": sorted(overwritten),
        }

    @app.delete("/api/role-templates/{role_id}")
    def delete_role_template(role_id: str):
        if store.get_role_template(role_id) is None:
            raise HTTPException(404, "全局角色模板不存在")
        if len(store.list_role_templates()) <= 1:
            raise HTTPException(409, "至少保留一个全局角色模板,用于新项目主控")
        store.delete_role_template(role_id)
        store.audit("human", "role_template_deleted", detail=f"role={role_id}")
        return {"ok": True}
