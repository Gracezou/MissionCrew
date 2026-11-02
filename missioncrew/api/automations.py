"""项目自动化脚本端点:定义维护、手动触发、webhook 配置与运行记录。"""
from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException

from ..collab.automations import (delete_automation, normalize_automation_id,
                                  rotate_webhook_secret, save_automation)
from ..collab.resource_urls import automation_resource_url
from ..collab.webhooks import webhook_path
from ..core.cron import next_cron_time
from ..core.models import Automation
from .context import ApiContext
from .schemas import AutomationInput
from .webhooks import webhook_listener_status


def register(app: FastAPI, ctx: ApiContext) -> None:
    store = ctx.store

    def _must_automation(project_id: str, automation_id: str) -> Automation:
        ctx.must_project(project_id)
        try:
            full_id = normalize_automation_id(project_id, automation_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        automation = store.get_automation(full_id)
        if automation is None or automation.project_id != project_id:
            raise HTTPException(404, "自动化脚本不存在")
        return automation

    def _automation_data(automation: Automation) -> dict:
        # 密钥只经 /webhook 端点按需读取,列表与轮询数据里不带
        data = automation.public_dict()
        data["resource_url"] = automation_resource_url(
            automation.project_id, automation.id)
        data["webhook_path"] = webhook_path(automation)
        data["running"] = automation.id in ctx.automations.running_ids()
        data["next_run_at"] = None
        if automation.enabled and automation.cron.strip():
            try:
                data["next_run_at"] = next_cron_time(
                    automation.cron, time.time())
            except ValueError:
                pass
        return data

    @app.get("/api/projects/{project_id}/automations")
    def list_automations(project_id: str):
        ctx.must_project(project_id)
        return {"automations": [
            _automation_data(item) for item in store.list_automations(project_id)],
            "webhook_listener": webhook_listener_status(ctx)}

    @app.post("/api/projects/{project_id}/automations")
    def save(project_id: str, body: AutomationInput):
        ctx.must_project(project_id)
        try:
            automation, _created = save_automation(
                store, project_id, id=body.id, name=body.name,
                description=body.description, script=body.script,
                cron=body.cron, enabled=body.enabled, webhook=body.webhook,
                actions=body.actions, timeout_seconds=body.timeout_seconds,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        ctx.automation_scheduler.poke()
        return _automation_data(automation)

    @app.delete("/api/projects/{project_id}/automations/{automation_id}")
    def delete(project_id: str, automation_id: str):
        _must_automation(project_id, automation_id)
        try:
            delete_automation(
                store, ctx.chat.agent_tools, project_id, automation_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        ctx.automation_scheduler.poke()
        return {"deleted": True}

    @app.post("/api/projects/{project_id}/automations/{automation_id}/run")
    def run(project_id: str, automation_id: str):
        automation = _must_automation(project_id, automation_id)
        try:
            run_id = ctx.automations.trigger(automation, trigger="manual")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @app.get("/api/projects/{project_id}/automations/{automation_id}/webhook")
    def webhook(project_id: str, automation_id: str):
        """webhook 配置:投递路径、密钥与端口监听状态(密钥只从这里给人看)。"""
        automation = _must_automation(project_id, automation_id)
        return {"enabled": automation.webhook, "path": webhook_path(automation),
                "secret": automation.webhook_secret,
                "listener": webhook_listener_status(ctx)}

    @app.post("/api/projects/{project_id}/automations/{automation_id}"
              "/webhook/secret")
    def rotate_secret(project_id: str, automation_id: str):
        automation = _must_automation(project_id, automation_id)
        if not automation.webhook:
            raise HTTPException(409, "该脚本未开启 webhook 触发")
        automation = rotate_webhook_secret(store, automation)
        return {"secret": automation.webhook_secret}

    @app.get("/api/projects/{project_id}/automations/{automation_id}/runs")
    def runs(project_id: str, automation_id: str, limit: int = 20):
        automation = _must_automation(project_id, automation_id)
        return {"runs": store.list_automation_runs(automation.id, limit)}

    @app.get("/api/projects/{project_id}/automations/{automation_id}"
             "/runs/{run_id}")
    def run_detail(project_id: str, automation_id: str, run_id: int):
        automation = _must_automation(project_id, automation_id)
        row = store.get_automation_run(run_id)
        if row is None or row["automation_id"] != automation.id:
            raise HTTPException(404, "运行记录不存在")
        return row
