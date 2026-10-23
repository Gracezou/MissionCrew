"""只读审计 API 与 Web 入口。"""
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import project_imports


def test_audit_filters_by_project_and_searches_action_or_detail(seeded):
    client = TestClient(create_app())
    seeded.audit("human", "project_saved", detail="project=webshop note=launch")
    seeded.audit("human", "project_resource_import_failed",
                 detail="source_project=webshop target_project=target item=guide")
    seeded.audit("platform", "chat_wake", detail="channel=webshop:general role=lead")
    seeded.audit("platform", "backend_updated", detail="backend=codex")
    seeded.audit("human", "project_saved", detail="project=webshop2 note=other")
    added = {row["id"] for row in seeded.list_audit(limit=5)}

    rows = client.get("/api/audit", params={"project_id": "webshop"}).json()["entries"]
    assert [row["action"] for row in rows if row["id"] in added] == [
        "chat_wake", "project_resource_import_failed", "project_saved"]
    assert [row["action"] for row in client.get(
        "/api/audit", params={"project_id": "target"}).json()["entries"]
            if row["id"] in added] == ["project_resource_import_failed"]
    assert len([row for row in client.get("/api/audit").json()["entries"]
                if row["id"] in added]) == 5
    assert [row["action"] for row in client.get(
        "/api/audit", params={"q": "IMPORT_FAILED"}).json()["entries"]] \
        == ["project_resource_import_failed"]
    assert [row["action"] for row in client.get(
        "/api/audit", params={"q": "launch"}).json()["entries"]] \
        == ["project_saved"]


def test_audit_limit_and_before_id_keep_equal_timestamps(seeded):
    client = TestClient(create_app())
    for i in range(5):
        seeded.audit("human", f"event_{i}")
    seeded._execute("UPDATE audit SET ts=? WHERE action LIKE 'event_%'", (1234.0,))

    first = client.get("/api/audit", params={"limit": 2, "q": "event_"}).json()
    assert [row["action"] for row in first["entries"]] == ["event_4", "event_3"]
    assert first["has_more"] is True
    second = client.get("/api/audit", params={
        "limit": 2, "q": "event_", "before_id": first["entries"][-1]["id"]}).json()
    assert [row["action"] for row in second["entries"]] == ["event_2", "event_1"]
    assert second["has_more"] is True
    third = client.get("/api/audit", params={
        "limit": 2, "q": "event_", "before_id": second["entries"][-1]["id"]}).json()
    assert [row["action"] for row in third["entries"]] == ["event_0"]
    assert third["has_more"] is False
    assert client.get("/api/audit", params={"limit": 0}).status_code == 422
    assert client.get("/api/audit", params={"limit": 501}).status_code == 422


def test_audit_redacts_credentials_without_truncating_other_detail(seeded):
    client = TestClient(create_app())
    detail = ("project=webshop token=token-value api_key=key-value "
              "Authorization: Bearer bearer-value password=pass-value "
              'webhook_secret="json-value" '
              "Authorization: Basic basic-value "
              "https://user:url-pass@example.test/path " + "原因详情" * 100)
    seeded.audit("human", "resource_failed", detail=detail)
    row = client.get("/api/audit").json()["entries"][0]
    for secret in ("token-value", "key-value", "bearer-value", "pass-value",
                   "json-value", "basic-value", "url-pass"):
        assert secret not in row["detail"]
    assert "project=webshop" in row["detail"]
    assert "原因详情" * 100 in row["detail"]
    assert "[已隐藏]" in row["detail"]
    assert set(row) == {"id", "ts", "actor", "action", "task_id", "detail"}


def test_task_detail_redacts_audit_but_cli_store_keeps_original(seeded):
    from missioncrew.core.models import Task

    client = TestClient(create_app())
    task = Task(id="audit-task", project_id="webshop", title="审计测试")
    seeded.put_task(task)
    seeded.audit("human", "task_updated", task.id,
                 "project=webshop token=task-secret")

    detail = client.get(f"/api/tasks/{task.id}").json()["audit"]
    assert "task-secret" not in str(detail)
    assert "[已隐藏]" in detail[0]["detail"]
    assert "task-secret" in seeded.list_audit(task.id)[0]["detail"]


def test_audit_widget_source_redacts_detail(seeded):
    client = TestClient(create_app())
    seeded.audit("human", "resource_failed",
                 detail="project=webshop api_key=board-secret")
    response = client.post("/api/projects/webshop/widget_data", json={"widgets": [
        {"id": "audit-widget", "type": "list",
         "content": {"source": {"from": "audit", "actions": ["resource_failed"]}}},
    ]})
    assert response.status_code == 200
    text = response.json()["audit-widget"]["items"][0]["text"]
    assert "board-secret" not in text
    assert "[已隐藏]" in text


def test_import_failure_full_reason_is_visible_in_audit(seeded, monkeypatch):
    client = TestClient(create_app())
    assert client.post("/api/projects", json={"id": "target", "name": "Target"}) \
        .status_code == 200
    reason = "复制文件时发现格式错误：" + "来源文件第 42 行" * 30

    def fail(*args, **kwargs):
        raise ValueError(reason)

    monkeypatch.setattr(project_imports, "_copy_guideline", fail)
    result = client.post("/api/projects/target/import", json={
        "source_project_id": "webshop", "resource_type": "guideline",
        "item_ids": ["release-check"]})
    assert result.status_code == 200
    assert result.json()["results"][0]["status"] == "failed"
    rows = client.get("/api/audit", params={
        "project_id": "target", "q": "project_resource_import_failed"}).json()["entries"]
    assert len(rows) == 1
    assert rows[0]["detail"].endswith(reason)
    assert "source_project=webshop target_project=target" in rows[0]["detail"]
    assert "resource_type=guideline item=release-check" in rows[0]["detail"]


def test_runtime_status_exposes_read_only_audit_tab(seeded):
    client = TestClient(create_app())
    html = client.get("/").text
    js = client.get("/assets/js/runtime-status.js").text
    assert 'id="runtime-tab-audit"' in html
    assert 'id="audit-project"' in html
    assert 'id="audit-search"' in html
    assert 'id="audit-more"' in html
    assert "showRuntimePanel('audit')" in html
    assert "`/api/audit?${params}`" in js
    assert 'params.set("before_id", auditRows[auditRows.length - 1].id)' in js
    assert 'esc(row.detail || "")' in js
    assert '<details><summary>查看完整详情</summary>' in js
