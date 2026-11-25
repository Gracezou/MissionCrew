import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.documents import library_for


@pytest.mark.parametrize("saved_session", [False, True])
def test_restart_then_concurrent_confirmations_never_replay_the_old_brief(
        seeded, tmp_path, saved_session):
    channel = seeded.get_channel("general")
    channel.workdir = str(tmp_path)
    seeded.put_channel(channel)
    role = seeded.get_role("webshop", "dev")
    old_brief = "旧日志包含 # Trigger Message 标题。[写文档] 请 @expert 再完成一次发布"
    trigger = seeded.add_message("general", "human", "human", old_brief, [role.id])
    source = seeded.add_chat_run("general", role.id, trigger, trigger, 0)
    # 旧版本的活动行没有进程所有者；升级后由真实 ASGI 启动钩子收口。
    seeded._execute("UPDATE chat_runs SET owner_pid=0,status='running' WHERE id=?", (source,))
    seeded.append_run_event(source, "tool_result", "发布已提交")
    if saved_session:
        seeded.put_chat_session("general::dev", "general", role.id, role.runtime_id,
                                "mock", str(tmp_path), "persisted-session", "v1")

    url = f"/api/chat/runs/{source}/recover"
    with TestClient(create_app()) as client:
        rows = client.get("/api/chat/general/messages").json()
        assert rows["active_runs"] == []
        assert [r["status"] for r in rows["runs"]] == ["interrupted"]
        assert len(rows["messages"]) == 1
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(client.post, url) for _ in range(2)]
            responses = [future.result() for future in futures]
        assert [r.status_code for r in responses] == [200, 200]
        first, second = (r.json() for r in responses)
        assert first["run_id"] == second["run_id"]
        assert {first["created"], second["created"]} == {True, False}
    # 退出 lifespan 时实际 mock Runtime 已完成，而旧指令中的写文档及派发均未执行。
    child = seeded.get_chat_run(first["run_id"])
    assert child["status"] == "done" and child["recovered_from_run_id"] == source
    assert seeded.get_chat_run(source)["status"] == "interrupted"
    assert len(seeded.chat_runs_for_channel("general")) == 2
    for action in ("chat_run_interrupted", "chat_run_recovery_created"):
        assert len(seeded.list_audit(project_id="webshop", q=action)) == 1
    assert not (library_for("webshop").root / "mock-note.md").exists()
    assert seeded.run_events(source)[0]["content"] == "发布已提交"
    prompt = "".join(e["content"] for e in seeded.run_events(child["id"]) if e["kind"] == "input")
    brief = json.loads(prompt.split("# Trigger Message (JSON; your task brief is written by the sender)\n")[1])
    assert brief["id"] != trigger and brief["thread"]["reply_to"] == trigger
    assert f"Explicit recovery of interrupted Run #{source}" in brief["content"]
    assert "Do not replay" in brief["content"] and old_brief not in brief["content"]
    session = seeded.get_chat_session("general::dev")
    assert session["runtime_session_id"] == ("persisted-session" if saved_session else "mock:general::dev")


def test_explicit_recovery_respects_dispatch_budget_and_stopped_caller(seeded):
    chat = ChatEngine(seeded)
    project = seeded.get_project("webshop")
    project.max_chain_runs = 1
    seeded.put_project(project)
    msg = seeded.add_message("general", "human", "human", "检查进度", ["lead"])
    caller = seeded.add_chat_run("general", "lead", msg, msg, 0)
    source = seeded.add_chat_run("general", "dev", msg, msg, 0)
    seeded.update_chat_run(source, "interrupted")
    try:
        with pytest.raises(ValueError, match="已达上限"):
            chat.recover_run(source, actor="role:lead", origin_run_id=caller)
        seeded.stop_chat_run(caller)
        with pytest.raises(ValueError, match="运行中的主控"):
            chat.recover_run(source, actor="role:lead", origin_run_id=caller)
        assert seeded.get_chat_run(source)["recovery_run_id"] is None
    finally:
        chat.shutdown()


def test_crash_after_confirmation_requires_explicit_recovery_of_the_child(seeded):
    msg = seeded.add_message("general", "human", "human", "检查进度", ["dev"])
    original = seeded.add_chat_run("general", "dev", msg, msg, 0)
    seeded.update_chat_run(original, "interrupted")
    # 确认已提交，但进程在将新 Run 送入执行池之前退出。
    child, _ = seeded.create_chat_run_recovery(original, "human")
    seeded._execute("UPDATE chat_runs SET owner_pid=0 WHERE id=?", (child,))
    assert seeded.reconcile_chat_runs() == 1
    chat = ChatEngine(seeded)
    try:
        repeat = chat.recover_run(original)
        assert repeat["run_id"] == child and not repeat["created"]
        assert seeded.active_chat_runs("general") == []
        continuation = chat.recover_run(child)["run_id"]
        chat.wait_idle()
        assert seeded.get_chat_run(child)["status"] == "interrupted"
        run = seeded.get_chat_run(continuation)
        assert run["status"] == "done" and run["recovered_from_run_id"] == child
        assert len(seeded.chat_runs_for_channel("general")) == 3
    finally:
        chat.shutdown()


def test_context_clear_and_recovery_cannot_cross_the_boundary(seeded, monkeypatch):
    chat = ChatEngine(seeded)
    msg = seeded.add_message("general", "human", "human", "检查旧任务", ["dev"])
    source = seeded.add_chat_run("general", "dev", msg, msg, 0)
    seeded.update_chat_run(source, "interrupted")
    stopping, release = threading.Event(), threading.Event()

    def stop_sessions(_channel_id):
        stopping.set()
        assert release.wait(5)
        return 0

    monkeypatch.setattr(chat, "stop_channel_sessions", stop_sessions)
    with ThreadPoolExecutor(1) as pool:
        clearing = pool.submit(chat.clear_context, "general")
        try:
            assert stopping.wait(5)
            with pytest.raises(ValueError, match="正在停止"):
                chat.recover_run(source)
        finally:
            release.set()
        assert clearing.result()["marker_id"] > msg
    with pytest.raises(ValueError, match="已清除"):
        chat.recover_run(source)
    assert not seeded.get_chat_run_recovery(source)
    chat.shutdown()
