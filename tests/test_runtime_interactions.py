"""聊天中的原生 Runtime 用户交互桥。"""
from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.runtime import runtime_manager


def test_pending_runtime_question_is_resolved_without_persisting_answer(store):
    store._execute("INSERT INTO channels(id,data) VALUES(?,?)",
                   ("channel", '{"id":"channel","name":"channel"}'))
    trigger = store.add_message("channel", "human", "human", "question", [])
    run_id = store.add_chat_run("channel", "lead", trigger, trigger, 0)
    chat = ChatEngine(store, max_workers=1)
    result = {}

    def ask():
        result.update(chat._request_runtime_interaction(
            run_id, "codex", "user_input_request", {
                "runtime": "codex", "questions": [{
                    "id": "secret", "header": "Token", "question": "Enter token",
                    "isSecret": True,
                }],
            }, None))

    worker = threading.Thread(target=ask)
    worker.start()
    for _ in range(50):
        events = store.run_events(run_id)
        if events:
            break
        time.sleep(0.02)
    payload = json.loads(events[0]["content"])
    chat.respond_interaction(run_id, payload["request_id"], {
        "decision": "submit", "answers": {"secret": ["do-not-persist"]},
    })
    worker.join(timeout=5)
    assert result["answers"] == {"secret": ["do-not-persist"]}
    stored = store.run_events(run_id)[0]["content"]
    assert "do-not-persist" not in stored
    assert json.loads(stored)["status"] == "resolved"


@pytest.mark.parametrize("backend,kind,result_type", [
    ("claude", "permission_request", "approval_waiting"),
    ("codex", "permission_request", "approval_waiting"),
    ("claude", "user_input_request", "question_waiting"),
    ("codex", "user_input_request", "question_waiting"),
])
def test_interaction_waiting_classification_reminds_once_and_resumes(
        seeded, backend, kind, result_type):
    trigger = seeded.add_message("general", "human", "human", "question", [])
    run_id = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    chat = ChatEngine(seeded, max_workers=1)
    payload = ({"questions": [{"question": "Which option?"}]}
               if kind == "user_input_request" else {"tool": "Bash"})

    for index in range(2):
        result = {}
        worker = threading.Thread(target=lambda: result.update(
            chat._request_runtime_interaction(
                run_id, backend, kind, payload, 5)))
        worker.start()
        for _ in range(100):
            events = seeded.run_events(run_id)
            if len(events) > index:
                break
            time.sleep(0.01)
        event = events[index]
        item = json.loads(event["content"])
        assert item["result_type"] == result_type
        assert item["notification_sent"] is (index == 0)
        assert seeded.get_chat_run(run_id)["status"] == "waiting_user"
        messages = seeded.list_messages("general")
        notices = [m for m in messages if m["author_type"] == "platform"
                   and "正在等待处理" in m["content"]]
        assert len(notices) == 1
        assert f"#run-card-{run_id}" in notices[0]["content"]
        assert "项目主控：@lead" in notices[0]["content"]
        decision = "submit" if kind == "user_input_request" else "approve"
        chat.respond_interaction(run_id, item["request_id"], {
            "decision": decision, "answers": {},
        })
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert json.loads(seeded.run_events(run_id)[index]["content"])["status"] == "resolved"
        assert seeded.get_chat_run(run_id)["status"] == "running"


def test_interaction_completion_does_not_overwrite_finished_run(store):
    store._execute("INSERT INTO channels(id,data) VALUES(?,?)",
                   ("channel", '{"id":"channel","name":"channel"}'))
    trigger = store.add_message("channel", "human", "human", "question", [])
    run_id = store.add_chat_run("channel", "lead", trigger, trigger, 0)
    chat = ChatEngine(store, max_workers=1)

    worker = threading.Thread(target=lambda: chat._request_runtime_interaction(
        run_id, "claude", "permission_request", {"tool": "Bash"}, 5))
    worker.start()
    for _ in range(50):
        events = store.run_events(run_id)
        if events:
            break
        time.sleep(0.02)
    payload = json.loads(events[0]["content"])
    store.update_chat_run(run_id, "failed", backend_id="claude", error="timeout")
    chat.respond_interaction(run_id, payload["request_id"], {
        "decision": "deny", "answers": {},
    })
    worker.join(timeout=5)
    row = store._query("SELECT status,error FROM chat_runs WHERE id=?", (run_id,))[0]
    assert row["status"] == "failed"
    assert row["error"] == "timeout"


def test_respond_interaction_waits_for_pending_registration(store):
    """事件已落库但 pending 尚未注册时,应答方必须等到注册完成而不是报请求不存在。"""
    store._execute("INSERT INTO channels(id,data) VALUES(?,?)",
                   ("channel", '{"id":"channel","name":"channel"}'))
    trigger = store.add_message("channel", "human", "human", "question", [])
    run_id = store.add_chat_run("channel", "lead", trigger, trigger, 0)
    chat = ChatEngine(store, max_workers=1)
    real_append = store.append_interaction_event

    def preempted_append(*args, **kwargs):
        event_id = real_append(*args, **kwargs)
        time.sleep(0.05)   # 模拟 worker 在事件 commit 后、注册 pending 前被调度走
        return event_id

    store.append_interaction_event = preempted_append
    worker = threading.Thread(target=lambda: chat._request_runtime_interaction(
        run_id, "claude", "permission_request", {"tool": "Bash"}, 5))
    worker.start()
    for _ in range(200):
        events = store.run_events(run_id)
        if events:
            break
        time.sleep(0.005)
    payload = json.loads(events[0]["content"])
    chat.respond_interaction(run_id, payload["request_id"], {
        "decision": "deny", "answers": {},
    })
    worker.join(timeout=5)
    assert json.loads(store.run_events(run_id)[0]["content"])["status"] == "resolved"


def test_channel_stop_cancels_pending_interaction_without_reopening_run(
        seeded, monkeypatch):
    trigger = seeded.add_message("general", "human", "human", "question", [])
    run_id = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    seeded.update_chat_run(run_id, "running", backend_id="std-1")
    chat = ChatEngine(seeded, max_workers=1)
    result = {}

    worker = threading.Thread(target=lambda: result.update(
        chat._request_runtime_interaction(
            run_id, "std-1", "permission_request", {"tool": "Bash"}, None)))
    worker.start()
    for _ in range(50):
        if any(event["kind"] == "permission_request"
               for event in seeded.run_events(run_id)):
            break
        time.sleep(0.02)
    assert any(event["kind"] == "permission_request"
               for event in seeded.run_events(run_id))

    monkeypatch.setattr(runtime_manager, "stop", lambda *_args, **_kwargs: 1)
    stopped = chat.stop_channel_agents("general")
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert stopped["stopped_runs"] == 1
    assert result["decision"] == "cancel"
    assert "用户已停止" in result["reason"]
    row = seeded._query(
        "SELECT status FROM chat_runs WHERE id=?", (run_id,))[0]
    assert row["status"] == "stopped"
    interaction = next(event for event in seeded.run_events(run_id)
                       if event["kind"] == "permission_request")
    payload = json.loads(interaction["content"])
    assert payload["status"] == "stopped" and payload["decision"] == "cancel"
    assert payload["result_type"] == "approval_waiting"
    audit = next(row for row in seeded.list_audit(limit=20)
                 if row["action"] == "runtime_interaction_resolved")
    assert audit["actor"] == "platform"


def test_runtime_interaction_timeout_marks_event_and_resumes_run(seeded):
    trigger = seeded.add_message("general", "human", "human", "question", [])
    run_id = seeded.add_chat_run("general", "dev", trigger, trigger, 0)
    chat = ChatEngine(seeded, max_workers=1)
    result = chat._request_runtime_interaction(
        run_id, "claude", "permission_request", {"tool": "Bash"}, 0.01)
    event = next(item for item in seeded.run_events(run_id)
                 if item["kind"] == "permission_request")
    payload = json.loads(event["content"])
    assert result["decision"] == "cancel"
    assert payload["status"] == "timeout" and payload["decision"] == "cancel"
    assert payload["result_type"] == "approval_waiting"
    assert seeded.get_chat_run(run_id)["status"] == "running"
    assert sum(m["author_type"] == "platform" and "正在等待处理" in m["content"]
               for m in seeded.list_messages("general")) == 1


def test_chat_assets_include_runtime_interaction_controls(seeded):
    client = TestClient(create_app())
    js = client.get("/assets/js/run-events.js").text
    assert "submitRuntimeAnswers" in js
    assert "sendRuntimeInteraction" in js
    assert "waiting_user" in js
    assert "permission_request" in js and "user_input_request" in js
    assert "提问待答" in js and "权限待审" in js and "策略决定" in js
    assert 'el.id = `run-card-${run.id}`' in js


def test_live_output_box_shows_pending_questions(seeded):
    """Agent 中途提问/请求权限时,待处理请求也显示在实时输出框里,不只藏在过程卡片中。"""
    client = TestClient(create_app())
    js = client.get("/assets/js/run-events.js").text
    css = client.get("/assets/css/app.css").text
    assert "function pendingInteractions(events)" in js
    assert "function renderLiveInteraction(run, { event, payload })" in js
    # 没有正文只有提问也要建框;表单只在请求集合变化时重建,轮询不清掉已填回答
    assert "if (!text.trim() && !asks.length) { existing?.remove(); return; }" in js
    assert 'class="live-text markdown-body"' in js and 'class="live-ask"' in js
    assert "askEl.dataset.askKey !== askKey" in js
    # 同一提问在过程卡片与实时框各有一份表单,单选/多选的 name 按渲染面命名空间
    assert 'name="ri-${esc(scope)}-${esc(payload.request_id)}-${esc(qid)}"' in js
    assert 'renderUserInputRequest(run, event, payload, "live")' in js
    assert ".run-live-output .live-ask .re-interaction" in css and ".live-ask .re-k" in css
