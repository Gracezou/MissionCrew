"""中断 Run 的只读审阅、显式确认与浏览器运行卡。"""
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.chat import ChatEngine
from missioncrew.core.models import Backend


def interrupted(store, backend_id=""):
    message = store.add_message("general", "human", "human", "检查构建产物", ["dev"])
    run = store.add_chat_run("general", "dev", message, message, 0)
    store.append_run_event(run, "tool_result", "构建文件已写入")
    store.update_chat_run(run, "interrupted", backend_id, "原执行进程已结束")
    return message, run


def test_human_reviews_and_explicitly_recovers_once(seeded, tmp_path, monkeypatch):
    client = TestClient(create_app())
    backend = Backend(id="codex", name="Codex", adapter="codex")
    seeded.put_backend(backend)
    role = seeded.get_role("webshop", "dev")
    role.runtime_id = backend.id
    seeded.put_role(role)
    channel = seeded.get_channel("general")
    channel.workdir = str(tmp_path)
    seeded.put_channel(channel)
    msg_id, source = interrupted(seeded, backend.id)
    seeded.put_chat_session("general::dev", "general", "dev", backend.id,
                            backend.adapter, str(tmp_path), "native-thread", "v1")
    submitted = []
    monkeypatch.setattr(ChatEngine, "_submit_run",
                        lambda self, run_id, *args: submitted.append(run_id))
    url = f"/api/chat/runs/{source}"
    info = client.get(f"{url}/recovery").json()
    assert info["trigger"] == {"id": msg_id, "author": "human", "content": "检查构建产物"}
    assert info["last_event"]["content"] == "构建文件已写入"
    assert info["session_available"] and info["recovery_run"] is None
    assert "native-thread" not in str(info)
    assert not submitted and seeded.get_chat_run_recovery(source) is None

    # GET 预览和读取频道都不会派发，首次 POST 才建立关联和新触发消息。
    assert client.get("/api/chat/general/messages").status_code == 200
    result = client.post(f"{url}/recover").json()
    child = result["run_id"]
    assert result == {"run_id": child, "created": True, "recovered_from_run_id": source}
    assert client.post(f"{url}/recover").json() == {**result, "created": False}
    assert submitted == [child]
    recovery = seeded.get_chat_run(child)
    trigger = seeded.get_message(recovery["trigger_message_id"])
    assert trigger["id"] != msg_id and trigger["author_type"] == "human"
    assert trigger["kind"] == "run_recovery"

    seeded.update_chat_run(child, "failed", backend.id, "原生会话恢复失败")
    assert client.get(f"{url}/recovery").json()["recovery_run"] == {
        "id": child, "status": "failed", "error": "原生会话恢复失败"}
    assert seeded.get_chat_run(source)["status"] == "interrupted"


def test_interrupted_run_remains_reachable_outside_recent_window(seeded):
    client = TestClient(create_app())
    original = [interrupted(seeded) for _ in range(35)]
    message, source = original[0]
    for _ in range(31):
        run_id = seeded.add_chat_run("general", "dev", message, message, 0)
        seeded.update_chat_run(run_id, "done")
    result = client.get("/api/chat/general/messages", params={"tail": True}).json()
    assert len(result["runs"]) == 30
    url = "/api/chat/general/interrupted-runs"
    page = client.get(url, params={"before": result["earlier_interrupted_before"]}).json()
    last_page = client.get(url, params={"before": page["next_before"]}).json()
    assert len(page["runs"]) == 30 and last_page["next_before"] is None
    assert [r["id"] for r in page["runs"] + last_page["runs"]] == [
        run for _, run in reversed(original)]
    assert not result["active_runs"]
    assert client.get(f"/api/chat/runs/{run_id}/recovery").status_code == 409
    assert client.post(f"/api/chat/runs/{run_id}/recover").status_code == 409
    assert client.post("/api/chat/runs/999999/recover").status_code == 404

    assert client.post("/api/chat/general/clear-context").status_code == 200
    info = client.get(f"/api/chat/runs/{source}/recovery").json()
    assert not info["session_available"] and "已清除" in info["unavailable_reason"]
    assert client.post(f"/api/chat/runs/{source}/recover").status_code == 409
    assert seeded.get_chat_run_recovery(source) is None
    result = client.get("/api/chat/general/messages").json()
    assert result["earlier_interrupted_before"] is None


def test_long_previews_bound_event_reads_and_keep_source_intact(seeded, monkeypatch):
    client = TestClient(create_app())
    body = "<b>消息</b>\n" + "x" * 10_000
    message = seeded.add_message("general", "human", "human", body, ["dev"])
    source = seeded.add_chat_run("general", "dev", message, message, 0)
    seeded.append_run_event(source, "tool_result", body)
    seeded.update_chat_run(source, "interrupted")
    info = client.get(f"/api/chat/runs/{source}/recovery").json()
    assert info["trigger"]["content"] == body[:600] + "…"
    assert len(info["last_event"]["content"]) <= 601
    assert seeded.get_message(message)["content"] == body

    def unexpected_live_output(*args):
        pytest.fail("interrupted runs do not have a live output box")
    monkeypatch.setattr(type(seeded), "run_live_output", unexpected_live_output)
    events = client.get(f"/api/chat/runs/{source}/events?include_live_output=false").json()
    assert events["events"][0]["content"] == body and events["live_output"] is None


@pytest.mark.parametrize("route,method", [("stop", "stop_channel_agents"),
                                          ("clear-context", "clear_context")])
def test_overlapping_channel_operations_are_conflicts(seeded, monkeypatch, route, method):
    def busy(*args):
        raise ValueError("频道正在停止，请稍后再试")
    monkeypatch.setattr(ChatEngine, method, busy)
    client = TestClient(create_app())
    assert client.post(f"/api/chat/general/{route}").status_code == 409
    assert client.post(f"/api/chat/missing/{route}").status_code == 404


def test_run_card_requires_explicit_confirmation_and_shows_failed_recovery():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the browser recovery flow")
    script = Path(__file__).parents[1] / "missioncrew/web/js/run-events.js"
    js = r"""
const assert = require("node:assert/strict");
global.setInterval = () => {};
global.esc = String;
global.roleColor = {};
require("node:vm").runInThisContext(require("node:fs").readFileSync(process.argv[1], "utf8"));
const calls = [];
const info = {role_id: "dev", reason: "服务重启", trigger: {id: 2, author: "human", content: "原请求"},
  last_event: {kind: "tool_result", content: "已完成一步"}, session_available: true};
global.api = async (method, url) => {
  calls.push([method, url]);
  return method === "GET" ? info : {run_id: 4, created: true};
};
global.toast = () => {};
global.pollMessages = async () => {};
let confirmed = false;
global.uiConfirm = async text => {
  for (const s of ["原请求", "已完成一步", "尝试续接", "关联的新运行"]) assert.ok(text.includes(s));
  return confirmed;
};
const event = {preventDefault() {}, stopPropagation() {}};
const button = {dataset: {runId: "3"}};
(async () => {
  const html = runSummary({id: 3, role_id: "dev", status: "interrupted"});
  assert.ok(html.includes("已中断") && html.includes("查看恢复"));
  await reviewChatRunRecovery(event, button);
  assert.deepEqual(calls, [["GET", "/api/chat/runs/3/recovery"]]);
  confirmed = true;
  await reviewChatRunRecovery(event, button);
  assert.deepEqual(calls.slice(1), [["GET", "/api/chat/runs/3/recovery"], ["POST", "/api/chat/runs/3/recover"]]);
  info.recovery_run = {id: 4, status: "failed", error: "原生会话恢复失败"};
  let alert;
  global.uiAlert = async text => { alert = text; };
  await reviewChatRunRecovery(event, button);
  assert.ok(alert.includes("续跑 #4 · 失败") && alert.includes("原生会话恢复失败"));
  assert.equal(calls.filter(([method]) => method === "POST").length, 1);
  assert.equal(button.disabled, false);

})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", js, str(script)], check=True)
