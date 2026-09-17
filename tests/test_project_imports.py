"""准则、Skill 与自动化的跨项目复制。"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.automations import save_automation
from missioncrew.collab.skill_versions import skill_version_library
from missioncrew.collab.skills import (project_skill_library_dir,
                                       skill_context_dir)


def _create_target(client: TestClient, project_id: str = "target") -> None:
    response = client.post("/api/projects", json={"id": project_id, "name": "Target"})
    assert response.status_code == 200


def _save_guideline(client: TestClient, project_id: str, body: str) -> None:
    response = client.post(f"/api/projects/{project_id}/guidelines", json={
        "markdown": (
            "---\nname: release-check\ndescription: 发布前检查\n---\n\n"
            f"{body}\n"),
        "enabled": False,
    })
    assert response.status_code == 200


def _save_source_skill(client: TestClient, *, enabled: bool = False) -> None:
    response = client.post("/api/projects/webshop/skills", json={
        "id": "release-tools",
        "markdown": (
            "---\nname: release-tools\ndescription: 发布辅助\n"
            "allowed-tools:\n  - Bash\n---\n\n运行 scripts/check.sh。\n"),
        "enabled": enabled,
    })
    assert response.status_code == 200
    source = project_skill_library_dir("webshop") / "release-tools"
    (source / "scripts").mkdir()
    script = source / "scripts" / "check.sh"
    script.write_text("#!/bin/sh\necho source\n")
    script.chmod(0o755)
    assert client.post("/api/projects/webshop/skills/rescan").status_code == 200


def _import(client: TestClient, resource_type: str, item_ids: list[str],
            *, target: str = "target", strategy: str | None = None):
    payload = {
        "source_project_id": "webshop",
        "resource_type": resource_type,
        "item_ids": item_ids,
    }
    if strategy is not None:
        payload["conflict_strategy"] = strategy
    return client.post(f"/api/projects/{target}/import", json=payload)


def test_cross_project_import_copies_all_resources_without_mutating_source(seeded):
    client = TestClient(create_app())
    _create_target(client)
    _save_guideline(client, "webshop", "检查变更日志。")
    _save_source_skill(client, enabled=True)
    automation, _ = save_automation(
        seeded, "webshop", id="daily-report", name="日报",
        description="生成日报", script="echo source", cron="0 9 * * *",
        created_by_role_id="lead")
    automation.last_run_at = 123.0
    automation.last_status = "succeeded"
    seeded.put_automation(automation)

    source_project_before = seeded.get_project("webshop").to_dict()
    source_automation_before = seeded.get_automation(
        "webshop:daily-report").to_dict()
    source_directory = project_skill_library_dir("webshop") / "release-tools"
    source_files_before = {
        path.relative_to(source_directory).as_posix(): path.read_bytes()
        for path in source_directory.rglob("*") if path.is_file()
    }
    source_history_before = skill_version_library("webshop").head()

    guideline = _import(client, "guideline", ["release-check"])
    skill = _import(client, "skill", ["release-tools"])
    automation_result = _import(client, "automation", ["daily-report"])
    assert guideline.status_code == skill.status_code == automation_result.status_code == 200
    assert guideline.json()["results"] == [{
        "id": "release-check", "status": "copied", "target_id": "release-check"}]
    assert skill.json()["results"] == [{
        "id": "release-tools", "status": "copied", "target_id": "release-tools"}]
    assert automation_result.json()["results"] == [{
        "id": "daily-report", "status": "copied",
        "target_id": "target:daily-report"}]

    target = seeded.get_project("target")
    copied_guideline = next(item for item in target.guidelines
                            if item.name == "release-check")
    assert copied_guideline.content == "检查变更日志。"
    assert copied_guideline.enabled is False
    copied_skill = next(item for item in target.skills
                        if item.id == "release-tools")
    assert copied_skill.enabled is True
    target_directory = project_skill_library_dir("target") / "release-tools"
    assert (target_directory / "scripts" / "check.sh").read_text() \
        == "#!/bin/sh\necho source\n"
    assert (target_directory / "scripts" / "check.sh").stat().st_mode & 0o111
    assert (target_directory / "SKILL.md").read_bytes() \
        == source_files_before["SKILL.md"]
    assert all(not path.is_symlink() for path in target_directory.rglob("*"))
    context_link = skill_context_dir(target) / "release-tools"
    assert context_link.is_symlink()
    assert context_link.resolve() == target_directory.resolve()
    assert not os.path.isabs(os.readlink(context_link))
    assert str(source_directory.resolve()) not in os.readlink(context_link)

    copied_automation = seeded.get_automation("target:daily-report")
    assert copied_automation.project_id == "target"
    assert copied_automation.script == "echo source"
    assert copied_automation.enabled is False
    assert copied_automation.last_run_at == 0.0
    assert copied_automation.last_status == ""
    assert copied_automation.created_by_role_id == ""

    # 源项目索引、文件、版本历史与自动化执行状态均保持不变。
    assert seeded.get_project("webshop").to_dict() == source_project_before
    assert seeded.get_automation("webshop:daily-report").to_dict() \
        == source_automation_before
    assert {
        path.relative_to(source_directory).as_posix(): path.read_bytes()
        for path in source_directory.rglob("*") if path.is_file()
    } == source_files_before
    assert skill_version_library("webshop").head() == source_history_before
    target_history = skill_version_library("target").history("release-tools")
    assert target_history and target_history[0]["message"] == "Import Skills release-tools"


def test_cross_project_import_skip_and_overwrite_conflicts(seeded):
    client = TestClient(create_app())
    _create_target(client)
    _save_guideline(client, "webshop", "source guideline")
    _save_guideline(client, "target", "target guideline")
    _save_source_skill(client)
    target_skill = client.post("/api/projects/target/skills", json={
        "id": "release-tools",
        "markdown": "---\nname: old\ndescription: old\n---\n\ntarget skill\n",
        "enabled": True,
    })
    assert target_skill.status_code == 200
    save_automation(seeded, "webshop", id="daily-report", script="echo source")
    old_automation, _ = save_automation(
        seeded, "target", id="daily-report", script="echo target", enabled=True)
    old_automation.last_run_at = 99.0
    old_automation.last_status = "failed"
    seeded.put_automation(old_automation)

    for resource_type, item_id in (
            ("guideline", "release-check"), ("skill", "release-tools"),
            ("automation", "daily-report")):
        skipped = _import(client, resource_type, [item_id])  # 默认 skip
        assert skipped.status_code == 200
        assert skipped.json()["results"][0]["status"] == "skipped"

    assert next(item for item in seeded.get_project("target").guidelines
                if item.name == "release-check").content == "target guideline"
    assert (project_skill_library_dir("target") / "release-tools" /
            "SKILL.md").read_text().endswith("target skill\n")
    assert seeded.get_automation("target:daily-report").script == "echo target"

    for resource_type, item_id in (
            ("guideline", "release-check"), ("skill", "release-tools"),
            ("automation", "daily-report")):
        overwritten = _import(
            client, resource_type, [item_id], strategy="overwrite")
        assert overwritten.status_code == 200
        assert overwritten.json()["results"][0]["status"] == "overwritten"

    assert next(item for item in seeded.get_project("target").guidelines
                if item.name == "release-check").content == "source guideline"
    target_directory = project_skill_library_dir("target") / "release-tools"
    assert (target_directory / "scripts" / "check.sh").is_file()
    context_link = skill_context_dir(seeded.get_project("target")) / "release-tools"
    assert not context_link.exists()  # overwrite 后沿用源 Skill 的停用状态
    copied_automation = seeded.get_automation("target:daily-report")
    assert copied_automation.script == "echo source"
    assert not copied_automation.enabled
    assert copied_automation.last_run_at == 0.0 and copied_automation.last_status == ""


def test_cross_project_import_rejects_same_project_and_reports_item_failure(seeded):
    client = TestClient(create_app())
    same = client.post("/api/projects/webshop/import", json={
        "source_project_id": "webshop",
        "resource_type": "guideline",
        "item_ids": ["missing"],
    })
    assert same.status_code == 400
    assert same.json()["detail"] == "源项目与目标项目不能相同"

    _create_target(client)
    missing = _import(client, "skill", ["missing", "missing"])
    assert missing.status_code == 200
    assert missing.json()["results"] == [
        {"id": "missing", "status": "failed", "reason": "源项目 Skill 不存在"},
        {"id": "missing", "status": "skipped", "reason": "请求中重复选择"},
    ]


def test_cross_project_import_controls_are_exposed_on_all_three_pages(seeded):
    client = TestClient(create_app())
    html = client.get("/").text
    configs = client.get("/assets/js/project-configs.js").text
    automations = client.get("/assets/js/automations.js").text
    imports = client.get("/assets/js/project-imports.js").text

    assert "/assets/js/project-imports.js" in html
    assert "openProjectImportDialog('skill'" in html
    assert "openProjectImportDialog('guideline'" in configs
    assert "openProjectImportDialog('automation'" in automations
    assert "/api/projects/${encodeURIComponent(target)}/import" in imports
    for status in ("copied", "skipped", "overwritten", "failed"):
        assert status in imports
    assert 'value="skip"' in imports and 'value="overwrite"' in imports
