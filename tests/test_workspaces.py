import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.agent_tools import AgentToolError
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.channels import ChannelChangeError
from missioncrew.collab.workspaces import WorkspaceError, create, finish, remove, status
from missioncrew.core.models import Channel, ProjectResource, Task


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                         text=True, capture_output=True)


def _repo(path):
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "MissionCrew Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base")
    return path


def _empty_template_repo(path):
    path.mkdir()
    _git(path, "init", "--template=", "-b", "main")
    _git(path, "config", "user.name", "MissionCrew Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base")
    return path


def _create(store, repo, **overrides):
    project = store.get_project("webshop")
    project.repos = [ProjectResource(
        id="fixture-repo", name="Fixture Repo", path=str(repo))]
    store.put_project(project)
    args = dict(repo_path=str(repo), name="task-1", base="main",
                branch="feature/task-1", channel_id="task-1",
                channel_name="work/task-1", purpose="test", task_id="")
    args.update(overrides)
    return create(store, project, "role:lead", **args)


def _agent_tool_identity(store, role_id):
    chat = ChatEngine(store)
    channel = store.get_channel("general")
    root_id = store.add_message(channel.id, "human", "human", f"@{role_id} run", [role_id])
    run_id = store.add_chat_run(channel.id, role_id, root_id, root_id, 0)
    store.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, store.get_role("webshop", role_id),
                            store.get_backend("std-1"), root_id, run_id=run_id)
    token = Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip()
    return chat, chat.agent_tools.authenticate(token), run_id


def test_create_rejects_missing_dirty_duplicate_and_disabled(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    with pytest.raises(WorkspaceError, match="不存在") as missing:
        _create(seeded, tmp_path / "absent")
    assert missing.value.code == "repo_missing"
    (repo / "dirty.txt").write_text("dirty")
    with pytest.raises(WorkspaceError) as dirty:
        _create(seeded, repo)
    assert dirty.value.code == "dirty_repository"
    project = seeded.get_project("webshop")
    project.managed_worktrees_enabled = False
    seeded.put_project(project)
    with pytest.raises(WorkspaceError) as disabled:
        _create(seeded, repo, allow_dirty=True)
    assert disabled.value.code == "disabled"
    project = seeded.get_project("webshop")
    project.managed_worktrees_enabled = True
    seeded.put_project(project)
    result = _create(seeded, repo, allow_dirty=True)
    assert result["channel"]["workdir"] == str(repo / ".worktrees" / "task-1")
    assert ".worktrees" not in _git(repo, "status", "--porcelain").stdout
    with pytest.raises(WorkspaceError) as duplicate:
        _create(seeded, repo, allow_dirty=True)
    assert duplicate.value.code == "already_exists"


def test_create_succeeds_with_empty_git_template(seeded, tmp_path):
    repo = _empty_template_repo(tmp_path / "empty-template-repo")
    assert not (repo / ".git" / "info").exists()
    result = _create(seeded, repo)
    assert result["channel"]["workdir"] == str(repo / ".worktrees" / "task-1")
    assert (repo / ".git" / "info" / "exclude").read_text().splitlines().count(
        "/.worktrees/") == 1


def test_create_rejects_duplicate_task(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    task = Task(id="task-1", project_id="webshop", title="Work")
    seeded.put_task(task)
    _create(seeded, repo, task_id=task.id)
    with pytest.raises(WorkspaceError) as duplicate:
        _create(seeded, repo, name="task-2", branch="feature/task-2",
                channel_id="task-2", channel_name="work/task-2", task_id=task.id)
    assert duplicate.value.code == "task_already_bound"
    assert seeded.get_task(task.id).channel_ids == ["webshop:task-1"]


@pytest.mark.parametrize("field,value", [
    ("name", ".."), ("name", "/"), ("name", "\\"), ("name", ""),
    ("channel_id", ".."), ("channel_id", "/"), ("channel_id", "\\"),
    ("channel_id", ""),
])
def test_create_rejects_unsafe_name_and_channel_ids(seeded, tmp_path, field, value):
    repo = _repo(tmp_path / "repo")
    with pytest.raises(WorkspaceError) as error:
        _create(seeded, repo, **{field: value})
    assert error.value.code == "invalid_arguments"


def test_agent_tool_management_permission_and_registered_repo_guard(seeded, tmp_path):
    chat, dev, dev_run = _agent_tool_identity(seeded, "dev")
    dev_actions = chat.agent_tools.capabilities(dev)["actions"]
    assert all(action not in dev_actions for action in (
        "workspace.create", "workspace.finish", "workspace.remove"))
    with pytest.raises(AgentToolError) as denied:
        chat.agent_tools.execute(dev, "workspace.finish", {"channel": "somewhere"},
                                 dev_run, "dev-workspace-finish")
    assert denied.value.code == "permission_denied"

    registered = _repo(tmp_path / "registered")
    project = seeded.get_project("webshop")
    project.repos = [ProjectResource(
        id="registered", name="Registered Repo", path=str(registered))]
    seeded.put_project(project)
    chat, lead, lead_run = _agent_tool_identity(seeded, "lead")
    unregistered = tmp_path / "unregistered"
    unregistered.mkdir()
    with pytest.raises(AgentToolError) as rejected:
        chat.agent_tools.execute(lead, "workspace.create", {
            "repo_path": str(unregistered), "name": "task-a", "base": "main",
            "branch": "feature/task-a", "channel_id": "task-a",
            "channel_name": "work/task-a",
        }, lead_run, "unregistered-workspace-repo")
    assert rejected.value.status_code == 403
    assert rejected.value.code == "permission_denied"


def test_concurrent_create_has_one_winner(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    def attempt(_):
        try:
            return _create(seeded, repo)
        except WorkspaceError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sorted(item if isinstance(item, str) else "created" for item in outcomes) == [
        "already_exists", "created"]
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_creation_failure_rolls_back_new_worktree_and_branch(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    original = seeded.put_channel
    def fail_channel(_channel):
        raise RuntimeError("database write failed")
    seeded.put_channel = fail_channel
    try:
        with pytest.raises(WorkspaceError) as error:
            _create(seeded, repo)
    finally:
        seeded.put_channel = original
    assert error.value.code == "create_failed"
    assert "已回滚" in str(error.value)
    assert not (repo / ".worktrees" / "task-1").exists()
    assert subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                           "refs/heads/feature/task-1"], check=False).returncode == 1


def test_finish_candidate_and_remove_requires_merge(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    _create(seeded, repo)
    channel = seeded.get_channel("webshop:task-1")
    workdir = repo / ".worktrees" / "task-1"
    (workdir / "change.txt").write_text("change\n")
    _git(workdir, "add", "change.txt")
    _git(workdir, "commit", "-m", "implement feature")
    # Compatibility with managed channels created by c2a9fe5.
    metadata = channel.managed_worktree
    metadata["repo"] = str(repo)
    metadata.pop("repo_name")
    metadata.pop("base_sha")
    seeded.put_channel(channel)
    candidate = finish(seeded, seeded.get_project("webshop"), channel)
    assert "change.txt" in candidate["diff_stat"]
    assert len(candidate["commits"]) == 1
    assert "implement feature" in candidate["commits"][0]
    assert candidate["merged"] is False
    assert candidate["commits_ahead"] == 1
    assert candidate["suggested_command"] == "git merge --no-ff feature/task-1"
    with pytest.raises(WorkspaceError) as unmerged:
        remove(seeded, seeded.get_project("webshop"), channel, "human")
    assert unmerged.value.code == "not_merged"
    assert workdir.is_dir()
    _git(repo, "merge", "--no-ff", "feature/task-1", "-m", "merge task")
    project = seeded.get_project("webshop")
    project.managed_worktrees_enabled = False
    seeded.put_project(project)
    assert status(channel, project)["merged"] is True
    finished = finish(seeded, seeded.get_project("webshop"), channel)
    assert finished["merged"] is True
    assert finished["dirty"] is False
    stopped = []
    result = remove(seeded, seeded.get_project("webshop"), channel, "human",
                    stop_sessions=lambda _cid: stopped.append(workdir.is_dir()))
    assert result["removed"] is True
    assert stopped == [True]
    assert channel.archived is True
    assert not workdir.exists()
    assert subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                           "refs/heads/feature/task-1"], check=False).returncode == 1


def test_empty_branch_with_dirty_worktree_is_not_merged_or_removable(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    _create(seeded, repo)
    channel = seeded.get_channel("webshop:task-1")
    workdir = Path(channel.workdir)
    (workdir / "uncommitted.txt").write_text("not committed\n")
    project = seeded.get_project("webshop")
    current = status(channel, project)
    assert current["commits_ahead"] == 0
    assert current["dirty"] is True
    assert current["merged"] is False
    candidate = finish(seeded, project, channel)
    assert candidate["dirty"] is True
    assert candidate["merged"] is False
    assert "未提交改动" in candidate["summary"]
    with pytest.raises(WorkspaceError) as error:
        remove(seeded, project, channel, "human")
    assert error.value.code == "not_merged"
    assert workdir.is_dir()


def test_remove_rejects_active_channel_run(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    _create(seeded, repo)
    channel = seeded.get_channel("webshop:task-1")
    root = seeded.add_message(channel.id, "human", "human", "active", [])
    run_id = seeded.add_chat_run(channel.id, "dev", root, root, 0)
    seeded.update_chat_run(run_id, "running", backend_id="std-1")
    with pytest.raises(ChannelChangeError) as active:
        remove(seeded, seeded.get_project("webshop"), channel, "human")
    assert active.value.code == "in_use"
    chat, lead, lead_run = _agent_tool_identity(seeded, "lead")
    with pytest.raises(AgentToolError) as mapped:
        chat.agent_tools.execute(lead, "workspace.remove", {"channel": "task-1"},
                                 lead_run, "remove-active-workspace")
    assert mapped.value.code == "channel_in_use"
    assert mapped.value.status_code == 409
    assert Path(channel.workdir).is_dir()


def test_finish_and_remove_reject_missing_worktree(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    _create(seeded, repo)
    channel = seeded.get_channel("webshop:task-1")
    (repo / ".worktrees" / "task-1").rename(repo / ".worktrees" / "lost")
    with pytest.raises(WorkspaceError) as finish_error:
        finish(seeded, seeded.get_project("webshop"), channel)
    assert finish_error.value.code == "worktree_missing"
    with pytest.raises(WorkspaceError) as remove_error:
        remove(seeded, seeded.get_project("webshop"), channel, "human")
    assert remove_error.value.code == "worktree_missing"


def test_remove_maps_broken_worktree_git_state_to_missing(seeded, tmp_path):
    repo = _repo(tmp_path / "repo")
    _create(seeded, repo)
    channel = seeded.get_channel("webshop:task-1")
    Path(channel.workdir, ".git").write_text("gitdir: /missing\n")
    with pytest.raises(WorkspaceError) as error:
        remove(seeded, seeded.get_project("webshop"), channel, "human")
    assert error.value.code == "worktree_missing"


def test_project_switch_persists_and_channel_api_exposes_workspace(seeded, tmp_path):
    client = TestClient(create_app())
    saved = client.post("/api/projects", json={
        "id": "webshop", "name": "网店", "managed_worktrees_enabled": False})
    assert saved.status_code == 200
    assert saved.json()["managed_worktrees_enabled"] is False
    assert client.post("/api/projects", json={
        "id": "webshop", "name": "网店"}).json()["managed_worktrees_enabled"] is False
    project = seeded.get_project("webshop")
    project.managed_worktrees_enabled = True
    seeded.put_project(project)
    repo = _repo(tmp_path / "api-repo")
    _create(seeded, repo)
    response = client.get("/api/projects/webshop/channels?scope=all")
    row = next(item for item in response.json()["channels"]
               if item["id"] == "webshop:task-1")
    assert row["managed_worktree"]["branch"] == "feature/task-1"
    assert row["managed_worktree"]["commits_ahead"] == 0
    assert row["managed_worktree"]["merged"] is False
    seeded.put_channel(Channel(id="orphan", project_id=None))
    legacy = next(item for item in client.get("/api/chat/channels").json()
                  if item["id"] == "orphan")
    assert "resource_url" not in legacy
