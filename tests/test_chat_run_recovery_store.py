import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from subprocess import CompletedProcess
from typing import Optional

import pytest

from missioncrew.core.models import Channel
from missioncrew.core.store import Store


def _run_in_other_process(path, ready, release):
    service = Store(path)
    trigger = service.add_message("general", "human", "human", "require approval", [])
    run_id = service.add_chat_run("general", "dev", trigger, trigger, 0)
    service.wait_chat_run_for_interaction(run_id)
    service.append_interaction_event(run_id, "permission_request", {"status": "pending"})
    ready.put(run_id)
    release.wait(30)


def test_process_exit_reconciles_once_without_touching_a_live_owner(store):
    # spawn 使用独立解释器；两个 Store 连接并不等于服务重启。
    context = get_context("spawn")
    ready, release = context.Queue(), context.Event()
    worker = context.Process(target=_run_in_other_process, args=(store.path, ready, release))
    worker.start()
    try:
        abandoned = ready.get(timeout=15)
        peer = Store(store.path)
        assert peer.reconcile_chat_runs() == 0
        assert peer.get_chat_run(abandoned)["status"] == "waiting_user"
    finally:
        release.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join()
    assert worker.exitcode == 0
    local = store.add_chat_run("general", "dev", 1, 1, 0)
    assert Store(store.path).get_chat_run(abandoned)["status"] == "waiting_user"
    assert peer.reconcile_chat_runs() == 1
    assert peer.reconcile_chat_runs() == 0
    assert peer.get_chat_run(local)["status"] == "queued"
    row = peer.get_chat_run(abandoned)
    assert row["status"] == "interrupted" and row["finished_at"] and row["error"]
    assert json.loads(peer.run_events(abandoned)[0]["content"])["status"] == "interrupted"
    assert not store.update_chat_run(abandoned, "running")
    assert len(peer.list_audit(q="chat_run_interrupted")) == 1


def test_legacy_active_runs_migrate_without_replaying_or_opening_pending_approvals(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE chat_runs (
            id INTEGER PRIMARY KEY, channel TEXT, role_id TEXT, backend_id TEXT,
            trigger_message_id INTEGER, root_id INTEGER, depth INTEGER, status TEXT,
            error TEXT, created_at REAL, finished_at REAL)""")
        conn.executemany(
            "INSERT INTO chat_runs VALUES(?, 'work', 'dev', '', 12, 12, 0, ?, '', 1, NULL)",
            enumerate(["queued", "running", "waiting_user", "done", "failed", "stopped"], 1))
    service = Store(path)
    service.append_interaction_event(3, "user_input_request", {"status": "pending"})
    service.append_interaction_event(3, "permission_request", {"status": "resolved"})
    service.append_run_event(3, "permission_request", "legacy log")
    service.put_agent_token(token_hash="key", token_id="token", project_id="proj", channel="work",
                            role_id="dev", run_id=3, scopes=[], expires_at=9999999999)
    assert service.reconcile_chat_runs() == 3
    assert service.active_chat_run_counts() == {}
    assert [r["status"] for r in service.chat_runs_for_channel("work")] == [
        "interrupted", "interrupted", "interrupted", "done", "failed", "stopped"]
    events = service.run_events(3)
    assert json.loads(events[0]["content"])["status"] == "interrupted"
    assert json.loads(events[1]["content"])["status"] == "resolved"
    assert events[2]["content"] == "legacy log"
    assert service.get_agent_token("key")["revoked_at"] is not None
    assert service.count_messages("work") == 0


def test_reused_owner_pid_does_not_hide_interrupted_run(store, monkeypatch):
    run = store.add_chat_run("general", "dev", 10, 10, 0)
    store._execute("UPDATE chat_runs SET owner_instance='old date|old instance' WHERE id=?", (run,))
    assert store.reconcile_chat_runs() == 1
    other = store.add_chat_run("general", "dev", 20, 20, 0)
    foreign_pid = os.getpid() + 1
    store._execute("UPDATE chat_runs SET owner_pid=?,owner_instance='old date|old instance' "
                   "WHERE id=?", (foreign_pid, other))
    monkeypatch.setattr("missioncrew.core.store.os.kill", lambda _pid, _signal: None)
    monkeypatch.setattr("missioncrew.core.store._process_state", lambda _pid: ("R", "new date"))
    assert store.reconcile_chat_runs() == 1
    assert store.get_chat_run(other)["status"] == "interrupted"


def test_zombie_owner_is_interrupted_before_its_parent_reaps_it(store, monkeypatch):
    run = store.add_chat_run("work", "dev", 1, 1, 0)
    start = "Thu Nov 19 10:00:00 2026"
    pid = os.getpid() + 1
    store._execute("UPDATE chat_runs SET owner_pid=?,owner_instance=? WHERE id=?",
                   (pid, f"{start}|same process", run))
    monkeypatch.setattr("missioncrew.core.store.os.kill", lambda _pid, _signal: None)

    def zombie(args, **_kwargs):
        assert args == ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="]
        return CompletedProcess(args, 0, stdout=f" Z+    {start}\n")

    monkeypatch.setattr("missioncrew.core.store.subprocess.run", zombie)
    assert store.reconcile_chat_runs() == 1
    assert store.get_chat_run(run)["status"] == "interrupted"


def test_simultaneous_service_startups_reconcile_once(store):
    trigger = store.add_message("general", "human", "human", "check progress", [])
    run = store.add_chat_run("general", "dev", trigger, trigger, 0)
    store._execute("UPDATE chat_runs SET owner_pid=0,owner_instance='' WHERE id=?", (run,))
    gate = threading.Barrier(2)

    def startup():
        service = Store(store.path)
        gate.wait(timeout=5)
        return service.reconcile_chat_runs()

    with ThreadPoolExecutor(2) as pool:
        starts = [pool.submit(startup) for _ in range(2)]
        assert sorted(call.result() for call in starts) == [0, 1]
    assert len(store.list_audit(q="chat_run_interrupted")) == 1
    assert store.count_messages("general") == 1


@pytest.mark.parametrize("actor,root_id,depth", [("human", None, 0), ("role:lead", 20, 2)])
def test_concurrent_recovery_creates_one_confirmation_and_one_linked_run(
        store, actor: str, root_id: Optional[int], depth: int):
    store.put_channel(Channel(id="work"))
    original = store.add_message("work", "human", "human", "an old request", ["dev"])
    source = store.add_chat_run("work", "dev", original, original, 0)
    store.update_chat_run(source, "interrupted")
    peer = Store(store.path)
    gate = threading.Barrier(2)

    def recover(service):
        gate.wait(timeout=5)
        return service.create_chat_run_recovery(source, actor, root_id=root_id, depth=depth)

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(recover, (store, peer))
    assert first[0] == second[0] and {first[1], second[1]} == {True, False}
    run = store.get_chat_run_recovery(source)
    assert run["recovered_from_run_id"] == source and run["status"] == "queued"
    assert store.get_chat_run(source)["recovery_run_id"] == run["id"]
    assert run["trigger_message_id"] != original
    msg = store.get_message(run["trigger_message_id"])
    assert msg["kind"] == "run_recovery" and msg["reply_to"] == original
    assert msg["author_type"] == ("agent" if actor.startswith("role:") else "human")
    assert msg["author"] == actor.removeprefix("role:")
    assert msg["root_id"] == run["root_id"] == (root_id or msg["id"])
    assert msg["depth"] == run["depth"] == depth
    assert json.loads(msg["mentions"]) == ["dev"] and f"Run #{source}" in msg["content"]
    assert "owner_instance" not in run and "owner_pid" not in run
    assert [r["id"] for r in store.interrupted_chat_runs("work")] == [source]
    assert store.interrupted_chat_runs("work", pending_only=True) == []
    assert store.interrupted_chat_runs("work", after_message_id=original) == []
    assert store.count_messages("work") == 2
    assert len(store.list_audit(q="chat_run_recovery_created")) == 1
    with pytest.raises(ValueError):
        store.create_chat_run_recovery(run["id"], "human")
    with pytest.raises(KeyError):
        store.create_chat_run_recovery(-1, "human")


def test_failed_recovery_rolls_back_confirmation_message(store):
    store.put_channel(Channel(id="work"))
    msg_id = store.add_message("work", "human", "human", "continue", [])
    run_id = store.add_chat_run("work", "dev", msg_id, msg_id, 0)
    store.update_chat_run(run_id, "interrupted")
    store._execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON chat_runs "
                   "BEGIN SELECT RAISE(ABORT,'retry later'); END")
    with pytest.raises(sqlite3.IntegrityError, match="retry later"):
        store.create_chat_run_recovery(run_id, "human")
    assert store.get_chat_run_recovery(run_id) is None
    assert store.count_messages("work") == 1
    assert store.list_audit(q="chat_run_recovery_created") == []
    store._execute("DROP TRIGGER fail_recovery")
    assert store.create_chat_run_recovery(run_id, "human")[1]


def test_recovery_and_context_clear_serialize_across_connections(store):
    store.put_channel(Channel(id="work", purpose="保留频道配置"))
    original = store.add_message("work", "human", "human", "request", ["dev"])
    source = store.add_chat_run("work", "dev", original, original, 0)
    store.update_chat_run(source, "interrupted")
    store.put_chat_session("work::dev", "work", "dev", "mock", "mock", ".", "native", "v1")
    peer = Store(store.path)
    gate = threading.Barrier(2)

    def attempt(operation):
        gate.wait(timeout=5)
        try:
            return operation()
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        clear, recovery = pool.map(attempt, (
            lambda: store.clear_channel_chat_context("work"),
            lambda: peer.create_chat_run_recovery(source, "human")))
    assert (clear is None) != (recovery is None)
    current = store.get_channel("work")
    assert current.purpose == "保留频道配置"
    if clear is not None:
        channel, marker, cleared_sessions = clear
        assert channel.context_start_message_id == current.context_start_message_id == marker
        assert cleared_sessions == 1 and store.get_chat_session("work::dev") is None
        assert store.get_message(marker)["root_id"] == marker
        assert store.get_chat_run_recovery(source) is None
        with pytest.raises(ValueError, match="清除"):
            peer.create_chat_run_recovery(source, "human")
    else:
        assert current.context_start_message_id == 0 and store.get_chat_session("work::dev")
        assert store.get_chat_run_recovery(source)["id"] == recovery[0]
        with pytest.raises(ValueError, match="正在运行"):
            store.clear_channel_chat_context("work")


def test_budget_and_duplicate_confirmation_in_one_transaction(store):
    store.put_channel(Channel(id="work"))
    sources = []
    for _ in range(2):
        msg = store.add_message("work", "human", "human", "request", ["dev"])
        run_id = store.add_chat_run("work", "dev", msg, msg, 0)
        store.update_chat_run(run_id, "interrupted")
        sources.append(run_id)
    root = store.add_message("work", "human", "human", "coordinate", ["lead"])
    store.add_chat_run("work", "lead", root, root, 0)
    peer = Store(store.path)
    gate = threading.Barrier(2)

    def confirm(service, source):
        gate.wait(timeout=5)
        try:
            return service.create_chat_run_recovery(
                source, "role:lead", root_id=root, depth=1, max_chain_runs=2)
        except ValueError as exc:
            assert "上限" in str(exc)
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(confirm, (store, peer), sources))
    assert results.count(None) == 1
    winner = next(i for i, result in enumerate(results) if result is not None)
    assert store.create_chat_run_recovery(
        sources[winner], "role:lead", root_id=root, depth=1, max_chain_runs=2
    ) == (results[winner][0], False)
    assert store.count_chain_runs(root) == 2


def test_interrupted_pages_and_archived_channel_guard(store):
    channel = Channel(id="work")
    store.put_channel(channel)
    runs = []
    for _ in range(4):
        msg = store.add_message("work", "human", "human", "request", ["dev"])
        run_id = store.add_chat_run("work", "dev", msg, msg, 0)
        store.update_chat_run(run_id, "interrupted")
        runs.append(run_id)
    store.create_chat_run_recovery(runs[2], "human")
    page = store.interrupted_chat_runs("work", pending_only=True, limit=2)
    assert [row["id"] for row in page] == [runs[3], runs[1]]
    rest = store.interrupted_chat_runs(
        "work", pending_only=True, before_run_id=page[-1]["id"], limit=2)
    assert [row["id"] for row in rest] == [runs[0]]
    channel.archived = True
    store.put_channel(channel)
    with pytest.raises(ValueError, match="归档"):
        store.create_chat_run_recovery(runs[0], "human")
