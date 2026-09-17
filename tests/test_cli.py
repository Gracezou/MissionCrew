import json

import yaml

from typer.testing import CliRunner

from missioncrew import cli


def test_chat_send_human_bracket_mention_dispatches(seeded):
    """人类 CLI 的 @[角色] 仍是显式派发语法;普通 @角色 只是正文。"""
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["chat", "send", "@[dev] 检查购物车,@reviewer 只是引用。"])
    assert result.exit_code == 0, result.output
    runs = [row["role_id"] for row in
            seeded._query("SELECT role_id FROM chat_runs")]
    assert runs == ["dev"]           # reviewer 未被普通 @ 触发
    stored = seeded.list_messages("general")[0]
    assert stored["content"].startswith("@dev ")   # 方括号归一化为可见提及
    assert [span["role_id"] for span in
            json.loads(stored["mention_spans"])] == ["dev"]


def test_project_add_uses_first_enabled_role_template(seeded, tmp_path):
    lead = seeded.get_role_template("lead")
    lead.enabled = False
    seeded.put_role_template(lead)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump({"id": "cli-project", "name": "CLI Project"}))

    result = CliRunner().invoke(
        cli.app, ["project", "add", "--file", str(project_file)])

    assert result.exit_code == 0, result.output
    assert seeded.get_project("cli-project").orchestrator_role_id == "dev"
    assert seeded.get_role("cli-project", "lead").enabled is False


def test_project_add_rejects_when_all_role_templates_are_disabled(seeded, tmp_path):
    for template in seeded.list_role_templates():
        template.enabled = False
        seeded.put_role_template(template)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(yaml.safe_dump({"id": "cli-none", "name": "CLI None"}))

    result = CliRunner().invoke(
        cli.app, ["project", "add", "--file", str(project_file)])

    assert result.exit_code != 0
    assert "全部为默认停用" in result.output
    assert seeded.get_project("cli-none") is None
    assert seeded.list_roles("cli-none") == []


def test_serve_listens_on_loopback_by_default(monkeypatch):
    called = {}

    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: called.update(kwargs))
    monkeypatch.delenv("MISSIONCREW_HOST", raising=False)

    cli.serve()

    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8321


def test_serve_host_env_overrides_default_but_not_flag(monkeypatch):
    called = {}

    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: called.update(kwargs))
    monkeypatch.setenv("MISSIONCREW_HOST", "0.0.0.0")

    cli.serve()
    assert called["host"] == "0.0.0.0"

    cli.serve(host="127.0.0.1")
    assert called["host"] == "127.0.0.1"


def test_serve_chat_workers_flag_sets_env(monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    monkeypatch.delenv("MISSIONCREW_CHAT_MAX_WORKERS", raising=False)

    cli.serve(chat_workers=9)
    assert cli.os.environ["MISSIONCREW_CHAT_MAX_WORKERS"] == "9"
    monkeypatch.delenv("MISSIONCREW_CHAT_MAX_WORKERS")

    cli.serve()
    assert "MISSIONCREW_CHAT_MAX_WORKERS" not in cli.os.environ


def test_mc_home_defaults_to_user_home(tmp_path, monkeypatch):
    """默认数据目录固定在 ~/.missioncrew,不随启动目录漂移;显式变量优先且支持 ~。"""
    from missioncrew.core.config import mc_home

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MISSIONCREW_HOME", raising=False)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert mc_home() == tmp_path / ".missioncrew"
    assert not (tmp_path / "elsewhere" / ".missioncrew").exists()

    monkeypatch.setenv("MISSIONCREW_HOME", "~/data-root")
    assert mc_home() == tmp_path / "data-root"
