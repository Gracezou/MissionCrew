"""项目间资源复制端点。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..collab.project_imports import import_project_resources
from .context import ApiContext
from .schemas import ProjectResourceImport


def register(app: FastAPI, ctx: ApiContext) -> None:
    @app.post("/api/projects/{target_project_id}/import")
    def import_resources(target_project_id: str, body: ProjectResourceImport):
        # 路由层只确认资源存在；复制、冲突与安全默认值均由 collab 统一处理。
        ctx.must_project(target_project_id)
        ctx.must_project(body.source_project_id)
        try:
            result = import_project_resources(
                ctx.store,
                source_project_id=body.source_project_id,
                target_project_id=target_project_id,
                resource_type=body.resource_type,
                item_ids=body.item_ids,
                agent_tools=ctx.chat.agent_tools,
                running_automation_ids=ctx.automations.running_ids(),
                conflict_strategy=body.conflict_strategy,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if body.resource_type == "automation":
            ctx.automation_scheduler.poke()
        return result
