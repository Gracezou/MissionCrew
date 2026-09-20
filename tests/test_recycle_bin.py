"""项目统一回收站：多资源删除、恢复、冲突与 Web 页面。"""
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import recycle_bin
from missioncrew.collab.documents import library_for
from missioncrew.collab.recycle_bin import (list_recycle_items,
                                             recycle_skill,
                                             restore_recycle_item)
from missioncrew.collab.skills import (import_skill_folder,
                                       project_skill_library_dir,
                                       skill_context_dir)


def _client():
    return TestClient(create_app())


def test_project_resources_share_one_recycle_bin_and_restore(seeded, tmp_path):
    client = _client()
    project_id = "webshop"

    library_for(project_id).write_bytes(
        "recycle/note.bin", b"\x00recover me\xff", overwrite=True)
    client.post(f"/api/projects/{project_id}/guidelines", json={
        "markdown": "---\nname: recycle-guide\ndescription: 回收测试\n---\n\n正文\n",
        "actor_role_id": "lead",
    })
    client.post(f"/api/projects/{project_id}/skills", json={
        "id": "recycle-skill",
        "markdown": "---\nname: recycle-skill\ndescription: 回收测试\n---\n\n正文\n",
        "enabled": False,
        "actor_role_id": "lead",
    })
    client.post(f"/api/projects/{project_id}/boards", json={
        "id": "recycle-board", "name": "回收面板", "actor_role_id": "lead",
    })
    channel = client.post("/api/chat/channels", json={
        "id": "recycle-channel", "name": "回收频道", "project_id": project_id,
        "actor_role_id": "lead",
    }).json()
    resource_dir = tmp_path / "recycle-resource"
    resource_dir.mkdir()
    resource = client.post(f"/api/projects/{project_id}/resources", json={
        "target": str(resource_dir), "name": "回收资源",
    }).json()
    role = seeded.get_role(project_id, "reviewer")
    assert role is not None

    content_channels = {}
    for kind, key in (
            ("docs", "recycle/note.bin"),
            ("guidelines", "recycle-guide"),
            ("skills", "recycle-skill")):
        content = client.post(
            f"/api/projects/{project_id}/content-channel", json={
                "content_kind": kind, "content_key": key, "label": key,
            }).json()
        content_channels[(kind, key)] = content["id"]
        seeded.add_message(
            content["id"], "human", "human", f"{kind} private conversation", [])

    responses = [
        client.delete(f"/api/projects/{project_id}/documents/file/recycle/note.bin"),
        client.delete(f"/api/projects/{project_id}/guidelines/recycle-guide",
                      params={"actor_role_id": "lead"}),
        client.delete(f"/api/projects/{project_id}/skills/recycle-skill",
                      params={"actor_role_id": "lead"}),
        client.delete(f"/api/projects/{project_id}/boards/recycle-board",
                      params={"actor_role_id": "lead"}),
        client.delete(f"/api/chat/channels/{channel['id']}"),
        client.delete("/api/roles/reviewer", params={"project_id": project_id}),
        client.delete(f"/api/projects/{project_id}/resources/{resource['id']}"),
    ]
    assert all(response.status_code == 200 for response in responses)
    for response in responses[:3]:
        assert response.json()["conversation"]["deleted"] is True
    assert len(responses[2].json()["revision"]) == 40
    for channel_id in content_channels.values():
        assert seeded.get_channel(channel_id) is None
        assert seeded.all_messages(channel_id) == []

    listing = client.get(f"/api/projects/{project_id}/recycle-bin").json()
    assert listing["resource_url"] == "/resources/webshop/recycle-bin"
    assert {item["resource_type"] for item in listing["items"]} == {
        "document", "guideline", "skill", "dashboard", "channel", "role",
        "project_resource",
    }
    assert all(item["resource_url"] == listing["resource_url"]
               and not any(key in item for key in ("metadata", "payload", "path"))
               for item in listing["items"])

    for item in listing["items"]:
        restored = client.post(
            f"/api/projects/{project_id}/recycle-bin/{item['id']}/restore",
            params={"actor_role_id": "lead"})
        assert restored.status_code == 200, restored.text

    assert client.get(f"/api/projects/{project_id}/recycle-bin").json()["items"] == []
    assert library_for(project_id).read_bytes("recycle/note.bin") == b"\x00recover me\xff"
    project = seeded.get_project(project_id)
    assert any(item.name == "recycle-guide" for item in project.guidelines)
    assert not next(item for item in project.skills
                    if item.id == "recycle-skill").enabled
    assert (project_skill_library_dir(project_id) / "recycle-skill" / "SKILL.md").is_file()
    skill_history = client.get(
        f"/api/projects/{project_id}/skills/recycle-skill/history").json()
    assert [row["message"].split()[0] for row in skill_history[:3]] == [
        "Restore", "Delete", "Save"]
    assert seeded.get_board(f"{project_id}:recycle-board") is not None
    assert seeded.get_channel(channel["id"]) is not None
    assert seeded.get_role(project_id, "reviewer") is not None
    assert any(item.id == resource["id"] for item in seeded.get_project(project_id).repos)
    for (kind, key), old_channel_id in content_channels.items():
        missing = client.get(
            f"/api/projects/{project_id}/content-channel", params={
                "content_kind": kind, "content_key": key,
            })
        assert missing.status_code == 404
        fresh = client.post(
            f"/api/projects/{project_id}/content-channel", json={
                "content_kind": kind, "content_key": key, "label": key,
            })
        assert fresh.status_code == 200
        assert fresh.json()["id"] == old_channel_id
        assert seeded.all_messages(old_channel_id) == []


def test_recycle_restore_conflict_keeps_item_and_purge_is_irreversible(seeded):
    client = _client()
    path = "recycle/conflict.md"
    url = f"/api/projects/webshop/documents/file/{path}"
    client.put(url, json={"content": "first"})
    deleted = client.delete(url).json()["recycle_item"]
    client.put(url, json={"content": "replacement"})

    forbidden = client.post(
        f"/api/projects/webshop/recycle-bin/{deleted['id']}/restore",
        params={"actor_role_id": "dev"})
    assert forbidden.status_code == 403
    conflict = client.post(
        f"/api/projects/webshop/recycle-bin/{deleted['id']}/restore",
        params={"actor_role_id": "lead"})
    assert conflict.status_code == 409
    assert library_for("webshop").read(path) == "replacement"
    assert [item["id"] for item in client.get(
        "/api/projects/webshop/recycle-bin").json()["items"]] == [deleted["id"]]

    purged = client.delete(
        f"/api/projects/webshop/recycle-bin/{deleted['id']}",
        params={"actor_role_id": "lead"})
    assert purged.status_code == 200
    assert client.post(
        f"/api/projects/webshop/recycle-bin/{deleted['id']}/restore",
        params={"actor_role_id": "lead"}).status_code == 404

    client.put("/api/projects/webshop/documents/file/recycle/empty.md",
               json={"content": "empty"})
    client.delete("/api/projects/webshop/documents/file/recycle/empty.md")
    assert client.delete("/api/projects/webshop/recycle-bin",
                         params={"actor_role_id": "dev"}).status_code == 403
    emptied = client.delete("/api/projects/webshop/recycle-bin",
                            params={"actor_role_id": "lead"})
    assert emptied.status_code == 200 and emptied.json()["purged"] == 1


def test_legacy_skill_trash_is_migrated_into_unified_bin(seeded):
    project_root = project_skill_library_dir("webshop").parent
    legacy = project_root / ".skill-trash" / "123" / "legacy-skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        "---\nname: legacy-skill\ndescription: 旧回收目录\n---\n\n正文\n",
        encoding="utf-8")

    items = _client().get("/api/projects/webshop/recycle-bin").json()["items"]
    assert any(item["resource_type"] == "skill"
               and item["resource_id"] == "legacy-skill" for item in items)
    assert not (project_root / ".skill-trash").exists()


def test_skill_delete_failure_rolls_back_package_index_and_live_view(
        seeded, monkeypatch):
    client = _client()
    response = client.post("/api/projects/webshop/skills", json={
        "id": "rollback-skill",
        "markdown": ("---\nname: rollback-skill\ndescription: 回滚测试\n"
                     "---\n\n正文\n"),
        "enabled": True,
        "actor_role_id": "lead",
    })
    assert response.status_code == 200
    project = seeded.get_project("webshop")
    original_put_project = seeded.put_project
    calls = 0

    def fail_once(value):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected project index failure")
        return original_put_project(value)

    monkeypatch.setattr(seeded, "put_project", fail_once)
    try:
        recycle_skill(seeded, project, "rollback-skill", actor="test")
    except RuntimeError as exc:
        assert str(exc) == "injected project index failure"
    else:
        raise AssertionError("故障注入应使删除失败")

    restored_project = seeded.get_project("webshop")
    assert any(item.id == "rollback-skill" and item.enabled
               for item in restored_project.skills)
    assert (project_skill_library_dir("webshop")
            / "rollback-skill" / "SKILL.md").is_file()
    assert (skill_context_dir(restored_project) / "rollback-skill").is_symlink()
    assert list_recycle_items("webshop") == []


def test_skill_restore_failure_keeps_recycle_item_and_deleted_state(
        seeded, monkeypatch):
    client = _client()
    response = client.post("/api/projects/webshop/skills", json={
        "id": "restore-rollback-skill",
        "markdown": ("---\nname: restore-rollback-skill\n"
                     "description: 恢复回滚测试\n---\n\n正文\n"),
        "actor_role_id": "lead",
    })
    assert response.status_code == 200
    project = seeded.get_project("webshop")
    item = recycle_skill(
        seeded, project, "restore-rollback-skill", actor="test")
    original_put_project = seeded.put_project
    calls = 0

    def fail_once(value):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected restore index failure")
        return original_put_project(value)

    monkeypatch.setattr(seeded, "put_project", fail_once)
    try:
        restore_recycle_item(
            seeded, seeded.get_project("webshop"), item["id"], actor="test")
    except RuntimeError as exc:
        assert str(exc) == "injected restore index failure"
    else:
        raise AssertionError("故障注入应使恢复失败")

    current = seeded.get_project("webshop")
    assert all(skill.id != "restore-rollback-skill" for skill in current.skills)
    assert not (project_skill_library_dir("webshop")
                / "restore-rollback-skill").exists()
    context_entry = skill_context_dir(current) / "restore-rollback-skill"
    assert not context_entry.exists() and not context_entry.is_symlink()
    assert [entry["id"] for entry in list_recycle_items("webshop")] == [item["id"]]


def test_recycle_bin_page_is_one_project_level_view(seeded):
    html = _client().get("/resources/webshop/recycle-bin").text
    router = Path("missioncrew/web/js/router.js").read_text(encoding="utf-8")
    page = Path("missioncrew/web/js/recycle-bin.js").read_text(encoding="utf-8")
    markdown = Path("missioncrew/web/js/markdown.js").read_text(encoding="utf-8")

    assert 'id="nav-recycle-bin"' in html
    assert 'id="recycle-bin-view"' in html
    assert 'id="recycle-bin-table"' in html
    assert 'src="/assets/js/recycle-bin.js"' in html
    assert 'route.tab = "recycle-bin"' in router
    assert 'missionCrewResourceUrl(currentProject, "recycle-bin")' in router
    assert '"recycle-bin",' in markdown
    for function in ("renderRecycleBin", "restoreRecycleItem",
                     "purgeRecycleItem", "emptyRecycleBin"):
        assert f"function {function}" in page


def test_skill_restore_and_overwrite_import_do_not_deadlock(seeded, tmp_path, monkeypatch):
    """回收站锁与 Skill 锁必须同序获取。

    恢复走「回收站锁 → Skill 锁」、覆盖导入走「Skill 锁 → 回收站锁」时两者互等。
    这里用事件把两条路径卡在各自已持一把锁、正要去拿另一把的位置，交错是确定
    的：事件成对握手，wait 的超时只是上限，修好后靠锁顺序保证不会互等，不靠它。
    """
    client = _client()
    project_id = "lock-probe"          # 用独立项目，避免把共享项目的锁留在死锁态
    assert client.post("/api/projects", json={
        "id": project_id, "name": "锁顺序探针"}).status_code == 200
    for skill_id in ("deadlock-restore", "deadlock-overwrite"):
        assert client.post(f"/api/projects/{project_id}/skills", json={
            "id": skill_id,
            "markdown": f"---\nname: {skill_id}\ndescription: 锁顺序测试\n---\n\n正文\n",
            "actor_role_id": "lead",
        }).status_code == 200
    item = recycle_skill(
        seeded, seeded.get_project(project_id), "deadlock-restore", actor="test")
    source = tmp_path / "import-source" / "deadlock-overwrite"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: deadlock-overwrite\ndescription: 覆盖导入\n---\n\n新正文\n",
        encoding="utf-8")

    restore_holds_recycle = threading.Event()
    import_holds_skill = threading.Event()
    handshake = 5.0
    original_sync = recycle_bin.sync_project_skill_library
    original_archive = recycle_bin.archive_replaced_skill

    def sync_after_handshake(*args, **kwargs):
        # 恢复路径：此刻已持回收站锁，等导入方拿到 Skill 锁后再去抢 Skill 锁
        restore_holds_recycle.set()
        import_holds_skill.wait(handshake)
        return original_sync(*args, **kwargs)

    def archive_after_handshake(*args, **kwargs):
        # 覆盖导入路径：此刻已持 Skill 锁，等恢复方拿到回收站锁后再去抢回收站锁
        import_holds_skill.set()
        restore_holds_recycle.wait(handshake)
        return original_archive(*args, **kwargs)

    monkeypatch.setattr(recycle_bin, "sync_project_skill_library", sync_after_handshake)
    monkeypatch.setattr(recycle_bin, "archive_replaced_skill", archive_after_handshake)

    failures: dict[str, BaseException] = {}

    def restore():
        try:
            restore_recycle_item(
                seeded, seeded.get_project(project_id), item["id"], actor="test")
        except BaseException as exc:            # noqa: BLE001 - 线程内异常带回主线程
            failures["restore"] = exc

    def overwrite_import():
        try:
            import_skill_folder(
                seeded, seeded.get_project(project_id), str(source),
                overwrite=True, actor="test")
        except BaseException as exc:            # noqa: BLE001
            failures["import"] = exc

    # 守护线程：万一仍然死锁，失败的是断言而不是整个 pytest 进程挂住
    threads = [threading.Thread(target=restore, name="restore", daemon=True),
               threading.Thread(target=overwrite_import, name="import", daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    stuck = [thread.name for thread in threads if thread.is_alive()]
    assert not stuck, f"回收站锁与 Skill 锁顺序相反导致死锁，线程未结束: {stuck}"
    assert not failures, failures
    library = project_skill_library_dir(project_id)
    assert (library / "deadlock-restore" / "SKILL.md").is_file()
    assert (library / "deadlock-overwrite" / "SKILL.md").read_text(
        encoding="utf-8").endswith("新正文\n")
    # 覆盖导入把旧包归档进同一回收站；被恢复的条目已从回收站移除
    archived = [entry for entry in list_recycle_items(project_id)
                if entry["resource_id"] == "deadlock-overwrite"]
    assert len(archived) == 1
    assert all(entry["id"] != item["id"] for entry in list_recycle_items(project_id))
