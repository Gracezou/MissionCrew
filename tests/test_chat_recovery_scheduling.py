import threading
from multiprocessing import get_context

from missioncrew.collab.chat import ChatEngine
from missioncrew.core.models import RunResult
from missioncrew.core.store import Store
from missioncrew.runtime import runtime_manager


def test_queued_recovery_is_a_separate_turn_between_ordinary_batches(seeded, monkeypatch):
    old_brief = "请重新发布旧版本"
    original = seeded.add_message("general", "human", "human", old_brief, ["dev"])
    source = seeded.add_chat_run("general", "dev", original, original, 0)
    seeded.update_chat_run(source, "interrupted")
    started, release = threading.Event(), threading.Event()
    turns = []

    def start(config):
        turns.append(config)
        if len(turns) == 1:
            started.set()
            assert release.wait(10)
        return RunResult(True, "ok", output="完成")

    monkeypatch.setattr(runtime_manager, "start", start)
    chat = ChatEngine(seeded, max_workers=1)
    try:
        first = chat.post("general", "human", "@[dev] 占用执行位")
        assert started.wait(10)
        before = [chat.post("general", "human", f"@[dev] 续跑前消息 {i}") for i in (1, 2)]
        child = chat.recover_run(source)
        duplicate = chat.recover_run(source)
        assert child["created"] and not duplicate["created"]
        assert child["run_id"] == duplicate["run_id"]
        marker = seeded.get_chat_run(child["run_id"])["trigger_message_id"]
        after = [chat.post("general", "human", f"@[dev] 续跑后消息 {i}") for i in (1, 2)]
    finally:
        release.set()
        chat.shutdown()

    assert [turn.trigger_message_id for turn in turns] == [first, before[1], marker, after[1]]
    assert f"Explicit recovery of interrupted Run #{source}" in turns[2].turn_prompt
    assert "Do not replay" in turns[2].turn_prompt and old_brief not in turns[2].turn_prompt
    assert seeded.get_chat_run(child["run_id"])["status"] == "done"
    assert seeded.get_chat_run(source)["recovery_run_id"] == child["run_id"]
    for batch in (before, after):
        absorbed, carrier = [seeded.chat_runs_for_trigger(msg)[0] for msg in batch]
        assert absorbed["status"] == "merged" and absorbed["merged_into"] == carrier["id"]
        assert carrier["status"] == "done"


def _queue_in_another_process(path, ready, release):
    peer = Store(path)
    msg = peer.add_message("general", "human", "human", "另一个服务的任务", ["dev"])
    run = peer.add_chat_run("general", "dev", msg, msg, 0)
    ready.put(run)
    try:
        release.wait(30)
    finally:
        peer.stop_chat_run(run)


def test_scheduler_leaves_another_live_process_queued_run_alone(seeded, monkeypatch):
    context = get_context("spawn")
    ready, release_peer = context.Queue(), context.Event()
    peer = context.Process(target=_queue_in_another_process, args=(seeded.path, ready, release_peer))
    peer.start()
    started, release_local = threading.Event(), threading.Event()
    turns = []

    def start(config):
        turns.append(config)
        started.set()
        assert release_local.wait(10)
        return RunResult(True, "ok", output="完成")

    monkeypatch.setattr(runtime_manager, "start", start)
    chat = ChatEngine(seeded, max_workers=1)
    try:
        foreign = ready.get(timeout=15)
        local = chat.post("general", "human", "@[dev] 本服务的任务")
        assert started.wait(10)
        assert [turn.trigger_message_id for turn in turns] == [local]
        assert seeded.get_chat_run(foreign)["status"] == "queued"
        assert not seeded.run_events(foreign)
    finally:
        release_peer.set()
        peer.join(5)
        if peer.is_alive():
            peer.terminate()
            peer.join()
        release_local.set()
        chat.shutdown()
    assert peer.exitcode == 0
    assert seeded.chat_runs_for_trigger(local)[0]["status"] == "done"
