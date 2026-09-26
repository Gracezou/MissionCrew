"""pi RPC 原生 provider。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from missioncrew.core import config as core_config
from missioncrew.core.models import (Backend, ExecutionConfig, RunResult,
                                     RuntimePermissions, RuntimePolicy)
from missioncrew.runtime import pi as pi_mod
from missioncrew.runtime.base import RuntimeCapabilities, RuntimeProvider
from missioncrew.runtime.pi import (PiRuntimeProvider, read_pi_models,
                                    validate_pi_providers)

FAKE_PI = str(Path(__file__).with_name("fake_pi_rpc.py"))
SETTINGS_MODELS_JS = (Path(__file__).resolve().parents[1] / "missioncrew" /
                      "web" / "js" / "settings-models.js")


class _Fallback(RuntimeProvider):
    def start(self, config: ExecutionConfig) -> RunResult:
        return RunResult(False, "fallback")

    def stop(self, backend: Backend, session_key: str = "") -> int:
        return 0

    def capabilities(self, backend: Backend) -> RuntimeCapabilities:
        return RuntimeCapabilities()

    def list_models(self, backend: Backend, timeout: int = 25) -> list[str]:
        return []


@pytest.fixture(autouse=True)
def _pi_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONCREW_HOME", str(tmp_path / "mc-home"))
    monkeypatch.setattr(pi_mod, "_RETRY_GRACE_SECONDS", 0.25)


def _config(tmp_path, prompt: str, saved: dict,
            events: list[tuple[str, str]], *, session_key: str = "channel::pi",
            scenario: str = "", effort: str = "") -> ExecutionConfig:
    backend = Backend(id="pi", name="pi", adapter="pi",
                      model="test-openai/test-model")

    def load():
        return saved.get("id", ""), saved.get("context", ""), False

    def save(session_id, context, **stats):
        saved.update(id=session_id, context=context, **stats)

    env = {"FAKE_PI_LAUNCH_LOG": str(tmp_path / "pi.launches")}
    if scenario:
        env["FAKE_PI_SCENARIO"] = scenario
    return ExecutionConfig(
        task_id="chat", stage_name="chat", backend=backend,
        prompt=prompt, workdir=str(tmp_path), project_id="project-a",
        role_id="lead",
        runtime_policy=RuntimePolicy(
            readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)],
            permissions=RuntimePermissions()),
        env=env, timeout=15, session_key=session_key,
        session_id=saved.get("id", ""), common_prompt="COMMON",
        turn_prompt=prompt, recovery_prompt="RECENT\n" + prompt,
        context_version="v1", load_session=load, save_session=save,
        emit=lambda kind, text: events.append((kind, text)),
        effort=effort,
    )


def _launches(tmp_path) -> list[list[str]]:
    log = tmp_path / "pi.launches"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def test_pi_turn_streams_and_reuses_session(tmp_path):
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    saved: dict = {}
    events: list[tuple[str, str]] = []
    try:
        first = provider.start(_config(tmp_path, "FIRST", saved, events))
        assert first.success and first.output == "pi answer 1"
        assert first.summary == ""          # 有输出时也不用固定文案
        assert saved["id"] and Path(saved["id"]).is_file()
        assert any(kind == "tool" and "bash" in text for kind, text in events)
        assert any(kind == "thinking" for kind, _ in events)
        usage = json.loads(next(text for kind, text in events if kind == "usage"))
        assert usage["schema"] == "usage/v1"
        assert usage["turn"] == {"input": 10, "output": 5, "total": 15}
        assert usage["cost_usd"] == 0.01 and usage["raw"]["cost"] == {"total": 0.01}

        second = provider.start(_config(tmp_path, "SECOND", saved, events))
        assert second.success and second.output == "pi answer 2"
        # 进程存活时同会话续用,不重启新进程
        assert len(_launches(tmp_path)) == 1
    finally:
        provider.shutdown()

    # 模拟服务重启:新 provider 用持久化的会话文件恢复
    events.clear()
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    try:
        third = provider.start(_config(tmp_path, "THIRD", saved, events))
        assert third.success and third.output == "pi answer 1"
        launches = _launches(tmp_path)
        assert len(launches) == 2
        assert launches[1][launches[1].index("--session") + 1] == saved["id"]
    finally:
        provider.shutdown()


def test_pi_launch_loads_bash_timeout_guard_extension(tmp_path):
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(_config(tmp_path, "GUARD", {}, events))
        assert result.success
        launch = _launches(tmp_path)[0]
        assert "--no-extensions" in launch
        guard = Path(launch[launch.index("--extension") + 1])
        assert guard.name == "pi_guard.ts" and guard.is_file()
        # 守卫职责钉住:注入超时规则提示词 + 拒绝无 timeout 的 bash 调用。
        text = guard.read_text(encoding="utf-8")
        assert "before_agent_start" in text and "tool_call" in text
        assert "block: true" in text
    finally:
        provider.shutdown()


def test_pi_ephemeral_run_uses_no_session(tmp_path):
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(
            _config(tmp_path, "ONE-SHOT", {}, events, session_key=""))
        assert result.success and result.output == "pi answer 1"
        assert "--no-session" in _launches(tmp_path)[0]
        assert provider.instances(Backend(id="pi", name="pi", adapter="pi")) == []
    finally:
        provider.shutdown()


def test_pi_auto_retry_not_treated_as_turn_end(tmp_path):
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    saved: dict = {}
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(
            _config(tmp_path, "FIRST", saved, events, scenario="retry"))
        assert result.success and result.output == "pi answer 1"
        assert any(kind == "status" and "自动重试" in text
                   for kind, text in events)
    finally:
        provider.shutdown()


def test_pi_error_turn_reports_provider_error(tmp_path):
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(
            _config(tmp_path, "FIRST", {}, events, scenario="error"))
        assert not result.success
        assert "provider rejected request" in result.summary
    finally:
        provider.shutdown()


def test_pi_empty_success_turn_keeps_summary_blank(tmp_path):
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(
            _config(tmp_path, "FIRST", {}, events, scenario="empty"))
        assert result.success and result.output == ""
        assert result.summary == ""
    finally:
        provider.shutdown()


def test_pi_effort_maps_to_thinking_flag(tmp_path):
    provider = PiRuntimeProvider(_Fallback(), [sys.executable, FAKE_PI])
    events: list[tuple[str, str]] = []
    try:
        result = provider.start(
            _config(tmp_path, "FIRST", {}, events, effort="high"))
        assert result.success
        launch = _launches(tmp_path)[0]
        assert launch[launch.index("--thinking") + 1] == "high"
    finally:
        provider.shutdown()


def test_pi_models_come_from_platform_models_json(tmp_path):
    assert read_pi_models() == []
    core_config.pi_models_path().write_text(json.dumps({
        "providers": {
            "raw-openai": {
                "baseUrl": "https://api.openai.com/v1",
                "apiKey": "$OPENAI_API_KEY", "api": "openai-completions",
                "models": [{"id": "gpt-5.2"}, {"id": "gpt-5.2-mini"}],
            },
            "raw-anthropic": {
                "baseUrl": "https://api.anthropic.com",
                "apiKey": "$ANTHROPIC_API_KEY", "api": "anthropic-messages",
                "models": [{"id": "claude-sonnet-5"}],
            },
        },
    }), encoding="utf-8")
    provider = PiRuntimeProvider(_Fallback())
    assert provider.list_models(Backend(id="pi", name="pi", adapter="pi")) == [
        "raw-openai/gpt-5.2", "raw-openai/gpt-5.2-mini",
        "raw-anthropic/claude-sonnet-5",
    ]


def _model_provider_client(seeded):
    from fastapi.testclient import TestClient
    from missioncrew.api import create_app

    seeded.put_backend(Backend(id="pi", name="pi", adapter="pi"))
    return TestClient(create_app())


def test_model_providers_api_roundtrip(seeded):
    client = _model_provider_client(seeded)
    initial = client.get("/api/model-providers").json()
    assert initial["config"] == {"providers": {}}
    # 页面按这两项决定是否提示"先装/先启用执行这些模型的运行时"
    assert initial["executor"]["id"] == "pi"
    assert "openai-completions" in initial["apis"]

    config = {"providers": {"raw-openai": {
        "baseUrl": "https://api.openai.com/v1", "apiKey": "sk-x",
        "api": "openai-completions", "models": [{"id": "gpt-5.2"}]}}}
    response = client.put("/api/model-providers", json=config)
    assert response.status_code == 200
    assert response.json()["executor"]["models"] == ["raw-openai/gpt-5.2"]
    path = core_config.pi_models_path()
    assert json.loads(path.read_text(encoding="utf-8")) == config
    assert (path.stat().st_mode & 0o777) == 0o600
    assert seeded.get_backend("pi").models == ["raw-openai/gpt-5.2"]

    bad = client.put("/api/model-providers", json={"providers": {
        "p": {"baseUrl": "", "api": "openai-completions",
              "models": [{"id": "m"}]}}})
    assert bad.status_code == 400


def test_model_providers_never_return_literal_key_and_keep_it_on_edit(seeded):
    """字面量密钥不回传浏览器,编辑时留空即沿用已保存的值。"""
    client = _model_provider_client(seeded)
    client.put("/api/model-providers", json={"providers": {
        "raw-openai": {"baseUrl": "https://api.openai.com/v1", "apiKey": "sk-x",
                       "api": "openai-completions", "models": [{"id": "gpt-5.2"}]},
        "by-env": {"baseUrl": "https://x/v1", "apiKey": "$MY_KEY",
                   "api": "openai-completions", "models": [{"id": "m"}]}}})

    shown = client.get("/api/model-providers").json()["config"]["providers"]
    assert shown["raw-openai"]["apiKey"] == ""          # 字面量只说明"已保存"
    assert shown["raw-openai"]["apiKeySaved"] is True
    assert shown["by-env"]["apiKey"] == "$MY_KEY"       # 环境变量引用不是密钥

    # 前端把读到的内容原样回写(密钥字段为空),已保存的密钥不能被清掉
    shown["raw-openai"]["models"] = [{"id": "gpt-5.2"}, {"id": "gpt-5.2-mini"}]
    client.put("/api/model-providers", json={"providers": shown})
    saved = json.loads(core_config.pi_models_path().read_text(encoding="utf-8"))
    assert saved["providers"]["raw-openai"]["apiKey"] == "sk-x"
    assert saved["providers"]["by-env"]["apiKey"] == "$MY_KEY"
    assert "apiKeySaved" not in saved["providers"]["raw-openai"]
    assert seeded.get_backend("pi").models == [
        "raw-openai/gpt-5.2", "raw-openai/gpt-5.2-mini", "by-env/m"]


def test_validate_pi_providers_rejects_bad_shapes():
    assert validate_pi_providers({}) != ""
    assert validate_pi_providers({"providers": {
        "p": {"baseUrl": "https://x", "api": "grpc",
              "models": [{"id": "m"}]}}}) != ""
    assert validate_pi_providers({"providers": {
        "p": {"baseUrl": "https://x", "api": "openai-completions",
              "models": []}}}) != ""
    assert validate_pi_providers({"providers": {
        "p": {"baseUrl": "https://x", "api": "openai-completions",
              "models": [{"id": "m"}]}}}) == ""
    # 执行单元是 "provider/model",provider 名带斜杠会让模型解析歧义
    assert validate_pi_providers({"providers": {
        "a/b": {"baseUrl": "https://x", "api": "openai-completions",
                "models": [{"id": "m"}]}}}) != ""


@pytest.mark.parametrize("model_id", [
    "deepseek-flash;", "deepseek,flash", "deepseek；flash", "deepseek，flash",
    "deepseek flash", "deepseek\tflash", "deepseek\x00flash", 123,
])
def test_validate_pi_providers_rejects_invalid_model_ids(model_id):
    error = validate_pi_providers({"providers": {
        "CPA": {"baseUrl": "https://x", "api": "openai-completions",
                "models": [{"id": model_id}]}}})
    assert error
    if isinstance(model_id, str):
        assert "不能包含空白、逗号、分号或控制字符" in error
    else:
        assert "模型必须带 id" in error


def test_settings_model_ids_split_common_separators_and_validate_whitespace():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the model provider form parser")
    script = r"""
const fs = require("fs"), vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);
const parsed = vm.runInContext(`parseModelIds(${JSON.stringify(process.argv[2])})`, context);
const error = vm.runInContext(`modelIdValidationError(${JSON.stringify(process.argv[3])})`, context);
process.stdout.write(JSON.stringify({ parsed, error }));
"""
    result = subprocess.run(
        [node, "-e", script, str(SETTINGS_MODELS_JS),
         " glm-5.2; deepseek-flash，glm-5.3-flash\n deepseek-v4-pro ",
         "deepseek flash"],
        text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    assert actual["parsed"] == [
        "glm-5.2", "deepseek-flash", "glm-5.3-flash", "deepseek-v4-pro"]
    assert "不能包含空白、逗号、分号或控制字符" in actual["error"]


def test_managed_pi_binary_relocates_with_data_dir(monkeypatch):
    """vendored pi 的可执行路径由数据目录推导:库里记的旧位置按当前数据目录
    重新定位;非托管工具不动;安装缺失时清空路径。"""
    from missioncrew.runtime.clis import pi as pi_spec
    monkeypatch.setattr(pi_spec.shutil, "which", lambda name: None)
    from missioncrew.core.config import pi_vendor_bin
    from missioncrew.runtime import runtime_manager
    vendored = pi_vendor_bin()
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text("#!/bin/sh\n")
    stale = Backend(id="pi", name="pi", adapter="pi", version="0.73.1",
                    binary_path="/old-root/.missioncrew/pi/vendor/node_modules/.bin/pi")
    assert runtime_manager.relocate_managed_binary(stale) is True
    assert stale.binary_path == str(vendored) and stale.version == "0.73.1"
    assert runtime_manager.relocate_managed_binary(stale) is False
    other = Backend(id="claude", name="c", adapter="claude_code",
                    binary_path="/home/u/.local/bin/claude")
    assert runtime_manager.relocate_managed_binary(other) is False
    assert other.binary_path == "/home/u/.local/bin/claude"
    vendored.unlink()
    assert runtime_manager.relocate_managed_binary(stale) is True
    assert stale.binary_path == ""


def test_startup_relocates_stale_pi_binary_path(store):
    """服务启动时按当前数据目录重定位托管安装,数据目录搬迁后不必手工改库。"""
    from missioncrew.api import create_app
    from missioncrew.core.config import pi_vendor_bin
    vendored = pi_vendor_bin()
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text("#!/bin/sh\n")
    store.put_backend(Backend(
        id="pi", name="pi", adapter="pi",
        binary_path="/old-root/.missioncrew/pi/vendor/node_modules/.bin/pi"))
    create_app()
    assert store.get_backend("pi").binary_path == str(vendored)


def test_pi_falls_back_to_system_binary_on_path(tmp_path, monkeypatch):
    """没有 vendored 安装时回退到 PATH 上的系统级 pi;vendored 存在时优先。"""
    from missioncrew.core.config import pi_vendor_bin
    from missioncrew.runtime.clis import pi as pi_spec
    system_pi = tmp_path / "bin" / "pi"
    monkeypatch.setattr(pi_spec.shutil, "which",
                        lambda name: str(system_pi) if name == "pi" else None)
    assert pi_spec.locate_binary() == str(system_pi)
    vendored = pi_vendor_bin()
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text("#!/bin/sh\n")
    assert pi_spec.locate_binary() == str(vendored)


def test_pi_update_plan_follows_install_source(tmp_path, monkeypatch):
    """升级渠道跟随安装来源:vendored/未安装走 --prefix npm,brew 走 brew upgrade,
    其他系统级安装不代管;任何情况都不出现 npm -g。"""
    from missioncrew.core.config import pi_vendor_bin, pi_vendor_prefix
    from missioncrew.runtime.clis import pi as pi_spec

    found = {"pi": None, "brew": "/opt/homebrew/bin/brew"}
    monkeypatch.setattr(pi_spec.shutil, "which", lambda name: found.get(name))

    kind, cmd = pi_spec.update_plan()  # 未安装:引导装进 vendor
    assert kind == "npm" and str(pi_vendor_prefix()) in cmd and "-g" not in cmd

    cellar = tmp_path / "Cellar" / "pi-coding-agent" / "1.0" / "bin" / "pi"
    cellar.parent.mkdir(parents=True)
    cellar.write_text("#!/bin/sh\n")
    link = tmp_path / "homebrew-bin" / "pi"
    link.parent.mkdir()
    link.symlink_to(cellar)
    found["pi"] = str(link)
    assert pi_spec.update_plan() == (
        "brew", ["/opt/homebrew/bin/brew", "upgrade", "pi-coding-agent"])

    other = tmp_path / "npm-global" / "bin" / "pi"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    found["pi"] = str(other)
    assert pi_spec.update_plan() is None

    vendored = pi_vendor_bin()
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text("#!/bin/sh\n")
    kind, cmd = pi_spec.update_plan()
    assert kind == "npm" and "-g" not in cmd
