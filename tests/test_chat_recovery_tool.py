import pytest

from missioncrew.collab.agent_tools import AgentToolError
from missioncrew.collab.chat import ChatEngine
from missioncrew.core.models import Automation, Channel


def _run(store, channel, role, status):
    trigger = store.add_message(channel, "human", "human", f"@{role} 工作", [role])
    run_id = store.add_chat_run(channel, role, trigger, trigger, 0)
    store.update_chat_run(run_id, status, backend_id="std-1")
    return run_id


def _identity(chat, role, tmp_path):
    store = chat.store
    run_id = _run(store, "general", role, "running")
    token_file, _ = chat.agent_tools.ensure_token_file(
        store.get_project("webshop"), store.get_channel("general"), role, tmp_path / role, run_id)
    return chat.agent_tools.authenticate(token_file.read_text().strip())


def test_project_orchestrator_discovers_and_recovers_a_run_once(seeded, tmp_path, monkeypatch):
    chat = ChatEngine(seeded)
    identity = _identity(chat, "lead", tmp_path)
    seeded.put_channel(Channel(id="webshop:work", name="工作", project_id="webshop"))
    target = _run(seeded, "webshop:work", "dev", "interrupted")
    assert "channel.run.recover" in chat.agent_tools.capabilities(identity)["actions"]
    arguments = {"channel": "work", "status": "interrupted"}
    snapshot = chat.agent_tools.execute(identity, "channel.runs.list", arguments, None, "list")
    assert [run["run_id"] for run in snapshot["runs"]] == [target]
    assert snapshot["runs"][0]["stoppable"] is False
    assert snapshot["runs"][0]["recovery_run_id"] is None

    submitted = []
    monkeypatch.setattr(chat, "_schedule", lambda channel, role_id, run_id: submitted.append(run_id))
    try:
        first = chat.agent_tools.execute(
            identity, "channel.run.recover", {"run_id": target}, None, "initial")
        repeated = chat.agent_tools.execute(
            identity, "channel.run.recover", {"run_id": target}, None, "repeat")
        chat.wait_idle()
        child = first["run_id"]
        assert submitted == [child]
        assert repeated["run_id"] == child
        assert first["created"] is True and repeated["created"] is False
        assert first["recovered_from_run_id"] == target
        assert seeded.get_chat_run(target)["status"] == "interrupted"
        assert seeded.get_chat_run(child)["channel"] == "webshop:work"
        receipts = [msg for msg in seeded.list_messages("general") if msg["kind"] == "agent_tool"]
        assert len(receipts) == 1 and "channel.run.recover" in receipts[0]["content"]
        snapshot = chat.agent_tools.execute(identity, "channel.runs.list", arguments, None, "check")
        assert snapshot["runs"][0]["recovery_run_id"] == child
    finally:
        chat.shutdown()


def test_agent_recovery_hides_cross_project_and_manual_only_runs(seeded, tmp_path):
    chat = ChatEngine(seeded)
    identity = _identity(chat, "lead", tmp_path)
    hidden = seeded.get_role("webshop", "dev")
    hidden.manual_only = True
    seeded.put_role(hidden)
    manual = _run(seeded, "general", "dev", "interrupted")
    seeded.put_channel(Channel(id="elsewhere:work", name="工作", project_id="elsewhere"))
    outside = _run(seeded, "elsewhere:work", "dev", "interrupted")
    snapshot = chat.agent_tools.execute(
        identity, "channel.runs.list", {"status": "interrupted"}, None, "list")
    assert snapshot["runs"] == []
    for target in (outside, manual):
        with pytest.raises(AgentToolError) as denied:
            chat.agent_tools.execute(
                identity, "channel.run.recover", {"run_id": target}, None, "invisible")
        assert denied.value.code == "run_not_found"
    with pytest.raises(AgentToolError) as denied_channel:
        chat.agent_tools.execute(identity, "channel.runs.list", {
            "status": "interrupted", "channel": "elsewhere:work"}, None, "another-project")
    assert denied_channel.value.code == "channel_not_found"


def test_agent_recovery_enforces_current_dispatch_authority(seeded, tmp_path):
    chat = ChatEngine(seeded)
    lead = _identity(chat, "lead", tmp_path)
    dev = _identity(chat, "dev", tmp_path)
    project = seeded.get_project("webshop")
    project.orchestrator_role_id = ""
    seeded.put_project(project)
    target = _run(seeded, "general", "expert", "interrupted")
    for identity in (lead, dev):
        assert "channel.run.recover" not in chat.agent_tools.capabilities(identity)["actions"]
        with pytest.raises(AgentToolError) as denied:
            chat.agent_tools.execute(identity, "channel.run.recover", {"run_id": target},
                                     None, "no-orchestrator")
        assert denied.value.code == "permission_denied"

    project.orchestrator_role_id = "lead"
    seeded.put_project(project)
    automation = Automation(id="webshop:script", project_id=project.id,
                            actions=["channel.run.recover"])
    seeded.put_automation(automation)
    token = chat.agent_tools.issue_automation_token(automation, 60)
    script = chat.agent_tools.authenticate(token)
    for identity in (dev, script):
        assert "channel.run.recover" not in chat.agent_tools.capabilities(identity)["actions"]
        with pytest.raises(AgentToolError) as denied:
            chat.agent_tools.execute(identity, "channel.run.recover", {"run_id": target},
                                     None, "no-dispatch")
        assert denied.value.code == "permission_denied"
