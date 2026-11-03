import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.workspaces import WorkspaceError, create, finish, remove
from missioncrew.core.models import Task


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


def _create(store, repo, **overrides):
    project = store.get_project("webshop")
    args = dict(repo_path=str(repo), name="task-1", base="main",
                branch="feature/task-1", channel_id="task-1",
                channel_name="work/task-1", purpose="test", task_id="")
    args.update(overrides)
    return create(store, project, "role:lead", **args)


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
    with pytest.raises(WorkspaceError) as duplicate:
        _create(seeded, repo, allow_dirty=True)
    assert duplicate.value.code == "already_exists"


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
    candidate = finish(seeded, seeded.get_project("webshop"), channel)
    assert "change.txt" in candidate["diff_stat"]
    assert candidate["commits"] == [candidate["commits"][0]]
    assert candidate["merged"] is False
    assert candidate["commits_ahead"] == 1
    assert candidate["suggested_command"] == "git merge --no-ff feature/task-1"
    with pytest.raises(WorkspaceError) as unmerged:
        remove(seeded, seeded.get_project("webshop"), channel, "human")
    assert unmerged.value.code == "not_merged"
    assert workdir.is_dir()
    _git(repo, "merge", "--no-ff", "feature/task-1", "-m", "merge task")
    finished = finish(seeded, seeded.get_project("webshop"), channel)
    assert finished["merged"] is True
    stopped = []
    result = remove(seeded, seeded.get_project("webshop"), channel, "human",
                    stop_sessions=lambda _cid: stopped.append(workdir.is_dir()))
    assert result["removed"] is True
    assert stopped == [True]
    assert channel.archived is True
    assert not workdir.exists()
    assert subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                           "refs/heads/feature/task-1"], check=False).returncode == 1


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
    assert row["managed_worktree"]["merged"] is True
