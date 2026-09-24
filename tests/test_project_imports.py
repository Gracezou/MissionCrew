"""准则、Skill 与自动化的跨项目复制。"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import project_imports, recycle_bin, skills
from missioncrew.collab.automations import AutomationService, save_automation
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.documents import guideline_library_for
from missioncrew.collab.recycle_bin import list_recycle_items, recycle_bin_dir
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


def test_cross_project_import_overwrite_skill_removes_stale_files_and_archives_old(seeded):
    client = TestClient(create_app())
    _create_target(client)
    _save_source_skill(client)
    assert client.post("/api/projects/target/skills", json={
        "id": "release-tools",
        "markdown": "---\nname: old\ndescription: old\n---\n\ntarget skill\n",
        "enabled": True,
    }).status_code == 200
    target_directory = project_skill_library_dir("target") / "release-tools"
    (target_directory / "stale.txt").write_text("only in target\n")
    assert client.post("/api/projects/target/skills/rescan").status_code == 200

    response = _import(client, "skill", ["release-tools"], strategy="overwrite")
    assert response.json()["results"] == [{
        "id": "release-tools", "status": "overwritten", "target_id": "release-tools"}]
    assert not (target_directory / "stale.txt").exists()
    assert (target_directory / "scripts" / "check.sh").is_file()
    archived = [item for item in list_recycle_items("target")
                if item["resource_type"] == "skill"
                and item["resource_id"] == "release-tools"]
    assert len(archived) == 1
    payload = recycle_bin_dir("target") / archived[0]["id"] / "payload"
    assert (payload / "stale.txt").read_text() == "only in target\n"
    assert (payload / "SKILL.md").read_text().endswith("target skill\n")


def test_cross_project_import_disabled_skill_never_exposed_in_context(seeded, monkeypatch):
    client = TestClient(create_app())
    _create_target(client)
    _save_source_skill(client, enabled=False)
    link = lambda: skill_context_dir(seeded.get_project("target")) / "release-tools"
    exposed = []
    original_write = skills.write_skill_context

    def recording_write(project):
        directory = original_write(project)
        exposed.append((directory / "release-tools").is_symlink())
        return directory

    monkeypatch.setattr(skills, "write_skill_context", recording_write)
    copied = _import(client, "skill", ["release-tools"])
    assert copied.json()["results"][0]["status"] == "copied"
    assert exposed and not any(exposed)
    assert next(item for item in seeded.get_project("target").skills
                if item.id == "release-tools").enabled is False

    # 目标已启用的同名 Skill 被停用源覆盖：目录替换发生前旧链接已撤下。
    project = seeded.get_project("target")
    next(item for item in project.skills if item.id == "release-tools").enabled = True
    seeded.put_project(project)
    original_write(project)
    assert link().is_symlink()
    exposed.clear()
    link_during_swap = []
    original_archive = recycle_bin.archive_replaced_skill

    def recording_archive(project, skill_id, source, **kwargs):
        link_during_swap.append(link().is_symlink())
        return original_archive(project, skill_id, source, **kwargs)

    monkeypatch.setattr(recycle_bin, "archive_replaced_skill", recording_archive)
    overwritten = _import(client, "skill", ["release-tools"], strategy="overwrite")
    assert overwritten.json()["results"][0]["status"] == "overwritten"
    assert link_during_swap == [False]
    assert exposed and exposed[-1] is False and not link().exists()


def test_cross_project_import_keeps_source_guideline_history(seeded):
    client = TestClient(create_app())
    _create_target(client)
    _save_guideline(client, "webshop", "检查变更日志。")
    history_before = guideline_library_for("webshop").history()

    for strategy in ("skip", "overwrite", "skip"):
        response = _import(client, "guideline", ["release-check"], strategy=strategy)
        assert response.status_code == 200

    assert guideline_library_for("webshop").history() == history_before
    target_messages = [item["message"] for item in
                       guideline_library_for("target").history("release-check.md")]
    assert "Import guideline release-check from project webshop" in target_messages


def test_cross_project_import_guideline_skip_detects_unindexed_target_file(seeded):
    client = TestClient(create_app())
    _create_target(client)
    _save_guideline(client, "webshop", "source guideline")
    # 工作树里有同名文件但尚未同步进索引：skip 仍须识别为冲突。
    guideline_library_for("target").write(
        "release-check.md",
        "---\nname: release-check\ndescription: 目标\n---\n\ntarget file\n",
        actor="human")
    assert not any(item.name == "release-check"
                   for item in seeded.get_project("target").guidelines)

    skipped = _import(client, "guideline", ["release-check"])
    assert skipped.json()["results"][0]["status"] == "skipped"
    assert guideline_library_for("target").read("release-check.md").endswith(
        "target file\n")
    overwritten = _import(client, "guideline", ["release-check"], strategy="overwrite")
    assert overwritten.json()["results"][0]["status"] == "overwritten"
    assert next(item for item in seeded.get_project("target").guidelines
                if item.name == "release-check").content == "source guideline"


@pytest.mark.parametrize("resource_type", ["guideline", "skill", "automation"])
def test_cross_project_import_rejects_unsafe_item_ids(seeded, resource_type):
    client = TestClient(create_app())
    _create_target(client)
    unsafe = ["../x", "a/b", "..", "target:job"]
    response = _import(client, resource_type, unsafe)
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["id"] for item in results] == unsafe
    assert all(item["status"] == "failed" and item["reason"] for item in results)
    assert not (project_skill_library_dir("target") / "x").exists()
    assert not any(automation.project_id == "target"
                   for automation in seeded.list_automations())


def test_cross_project_import_accepts_source_prefixed_automation_id(seeded):
    client = TestClient(create_app())
    _create_target(client)
    save_automation(seeded, "webshop", id="job", script="echo job")
    response = _import(client, "automation", ["webshop:job"])
    assert response.json()["results"] == [{
        "id": "webshop:job", "status": "copied", "target_id": "target:job"}]
    assert seeded.get_automation("target:job").script == "echo job"


def test_cross_project_import_mixed_results_in_one_batch(seeded):
    client = TestClient(create_app())
    _create_target(client)
    save_automation(seeded, "webshop", id="daily-report", script="echo source")
    save_automation(seeded, "webshop", id="weekly-report", script="echo weekly")
    save_automation(seeded, "target", id="weekly-report", script="echo target")

    response = _import(
        client, "automation", ["daily-report", "weekly-report", "missing", "../x"])
    assert response.status_code == 200
    results = response.json()["results"]
    assert [(item["id"], item["status"]) for item in results] == [
        ("daily-report", "copied"), ("weekly-report", "skipped"),
        ("missing", "failed"), ("../x", "failed")]
    assert seeded.get_automation("target:daily-report").script == "echo source"
    assert seeded.get_automation("target:weekly-report").script == "echo target"


def test_cross_project_import_request_validation(seeded):
    client = TestClient(create_app())
    _create_target(client)
    base = {"source_project_id": "webshop", "resource_type": "guideline",
            "item_ids": ["release-check"]}
    for override in ({"resource_type": "role"},
                     {"conflict_strategy": "merge"},
                     {"source_project_id": "../webshop"},
                     {"source_project_id": ""},
                     {"item_ids": []}):
        response = client.post("/api/projects/target/import", json=base | override)
        assert response.status_code == 422, override

    missing_target = client.post("/api/projects/missing/import", json=base)
    assert missing_target.status_code == 404
    assert missing_target.json()["detail"] == "项目不存在"
    missing_source = client.post(
        "/api/projects/target/import", json=base | {"source_project_id": "missing"})
    assert missing_source.status_code == 404


def test_cross_project_import_overwrite_automation_clears_runs_and_tokens(seeded, monkeypatch):
    client = TestClient(create_app())
    _create_target(client)
    save_automation(seeded, "webshop", id="daily-report", script="echo source")
    old, _ = save_automation(
        seeded, "target", id="daily-report", script="echo target", enabled=True)
    run_id = seeded.start_automation_run(old.id, "target", "manual")
    seeded.finish_automation_run(run_id, "succeeded", exit_code=0, stdout="old")
    seeded.touch_automation_run_state(old.id, "succeeded")
    tools = ChatEngine(seeded).agent_tools
    token = tools.issue_automation_token(old, 600)

    # 目标正在运行：该条失败，定义、运行记录与令牌都保持原样。
    monkeypatch.setattr(AutomationService, "running_ids",
                        lambda self: {"target:daily-report"})
    running = _import(client, "automation", ["daily-report"], strategy="overwrite")
    assert running.json()["results"] == [{
        "id": "daily-report", "status": "failed", "reason": "目标自动化正在运行"}]
    assert seeded.get_automation(old.id).script == "echo target"
    assert len(seeded.list_automation_runs(old.id)) == 1
    tools.authenticate(token)

    monkeypatch.setattr(AutomationService, "running_ids", lambda self: set())
    overwritten = _import(client, "automation", ["daily-report"], strategy="overwrite")
    assert overwritten.json()["results"][0]["status"] == "overwritten"
    copied = seeded.get_automation(old.id)
    assert copied.script == "echo source" and copied.enabled is False
    assert copied.last_run_at == 0.0 and copied.last_status == ""
    assert seeded.list_automation_runs(old.id) == []
    assert client.get(
        "/api/projects/target/automations/daily-report/runs").json() == {"runs": []}
    rows = seeded._query(
        "SELECT revoked_at FROM agent_tokens WHERE kind='automation'")
    assert rows and all(row["revoked_at"] is not None for row in rows)


def test_cross_project_import_item_failure_keeps_other_results_without_paths(
        seeded, monkeypatch):
    client = TestClient(create_app())
    _create_target(client)
    _save_source_skill(client)
    assert client.post("/api/projects/webshop/skills", json={
        "id": "lint-tools",
        "markdown": "---\nname: lint-tools\ndescription: lint\n---\n\nlint\n",
        "enabled": True,
    }).status_code == 200
    original_import = project_imports.import_skill_folder
    home = os.environ["MISSIONCREW_HOME"]

    def flaky_import(store, project, source, **kwargs):
        if source.endswith("release-tools"):
            raise subprocess.CalledProcessError(
                128, ["git", f"--git-dir={home}/projects/target/skill-history.git"],
                stderr=f"fatal: {home}")
        return original_import(store, project, source, **kwargs)

    monkeypatch.setattr(project_imports, "import_skill_folder", flaky_import)
    response = _import(client, "skill", ["release-tools", "lint-tools"])
    assert response.status_code == 200
    results = response.json()["results"]
    assert [(item["id"], item["status"]) for item in results] == [
        ("release-tools", "failed"), ("lint-tools", "copied")]
    assert results[0]["reason"] == "版本库写入失败"
    assert home not in response.text

    def missing_file(store, project, source, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", f"{home}/x")

    monkeypatch.setattr(project_imports, "import_skill_folder", missing_file)
    failed = _import(client, "skill", ["release-tools"], strategy="overwrite")
    assert failed.json()["results"][0]["reason"] == \
        "文件操作失败：No such file or directory"
    assert home not in failed.text


def test_cross_project_import_copy_error_reason_hides_absolute_paths(
        seeded, monkeypatch):
    """shutil.Error 是 OSError 子类但 strerror/filename 为空，逐条原因不能回显
    它的 args——里面是每个失败文件的源/目标绝对路径。"""
    client = TestClient(create_app())
    _create_target(client)
    _save_source_skill(client)
    home = os.environ["MISSIONCREW_HOME"]

    def copy_error(store, project, source, **kwargs):
        raise shutil.Error([
            (f"{home}/projects/webshop/skills/release-tools/SKILL.md",
             f"{home}/projects/target/skills/release-tools/SKILL.md",
             "[Errno 13] Permission denied"),
        ])

    monkeypatch.setattr(project_imports, "import_skill_folder", copy_error)
    response = _import(client, "skill", ["release-tools"])

    assert response.status_code == 200
    assert response.json()["results"][0] == {
        "id": "release-tools", "status": "failed", "reason": "文件复制失败"}
    assert home not in response.text


def test_automation_overwrite_replaces_definition_in_one_transaction(seeded):
    """删旧 + 写新必须同一事务：写入失败时不能留下「旧的已删、新的没写」。"""
    old, _ = save_automation(
        seeded, "webshop", id="nightly", script="echo old", enabled=True)
    run_id = seeded.start_automation_run(old.id, "webshop", "manual")
    seeded.finish_automation_run(run_id, "succeeded", exit_code=0, stdout="old")

    class _Unserializable:
        id = old.id
        updated_at = 0.0

        def to_dict(self):
            return {"id": self.id, "script": {"不可序列化"}}

    with pytest.raises(TypeError):
        seeded.replace_automation(old.id, _Unserializable())
    survivor = seeded.get_automation(old.id)
    assert survivor is not None and survivor.script == "echo old"
    assert len(seeded.list_automation_runs(old.id)) == 1

    replacement = replace(old, script="echo new", enabled=False)
    seeded.replace_automation(old.id, replacement)
    assert seeded.get_automation(old.id).script == "echo new"
    assert seeded.list_automation_runs(old.id) == []


def test_cross_project_import_skill_failure_reason_only_mentions_item(seeded):
    client = TestClient(create_app())
    _create_target(client)
    _save_source_skill(client)
    # 目标库里另一个无效 Skill 的扫描问题不应混进本条原因。
    (project_skill_library_dir("target") / "broken").mkdir()
    # 源目录在索引之后被改坏，复制时才发现；API 层会先重扫源项目，这里直接走 collab。
    (project_skill_library_dir("webshop") / "release-tools" / "SKILL.md").write_text(
        "no frontmatter\n")

    response = project_imports.import_project_resources(
        seeded, source_project_id="webshop", target_project_id="target",
        resource_type="skill", item_ids=["release-tools"],
        agent_tools=ChatEngine(seeded).agent_tools)
    result = response["results"][0]
    assert result["status"] == "failed"
    assert "frontmatter" in result["reason"]
    assert "broken" not in result["reason"]


def test_cross_project_import_controls_are_exposed_on_all_three_pages(seeded):
    client = TestClient(create_app())
    html = client.get("/").text
    configs = client.get("/assets/js/project-configs.js").text
    automations = client.get("/assets/js/automations.js").text
    imports = client.get("/assets/js/project-imports.js").text

    assert "/assets/js/project-imports.js" in html
    # 三个页面的入口统一常驻页头，不随条目选中或详情页出现。
    for resource_type in ("guideline", "skill", "automation"):
        assert html.count(f"openProjectImportDialog('{resource_type}')") == 1
    assert 'id="guideline-proj-label"' in html and 'id="automation-proj-label"' in html
    assert "openProjectImportDialog" not in configs
    assert "openProjectImportDialog" not in automations
    assert "/api/projects/${encodeURIComponent(target)}/import" in imports
    for status in ("copied", "skipped", "overwritten", "failed"):
        assert status in imports
    assert 'value="skip"' in imports and 'value="overwrite"' in imports
    assert "覆盖会丢弃目标项目原有脚本，且不进回收站" in imports
    assert "button.disabled = true" in imports
