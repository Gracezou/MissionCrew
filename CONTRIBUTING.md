# 参与贡献

感谢你对 MissionCrew 的兴趣。提 Issue、改文档、修 bug、接入新的 Agent CLI 都欢迎。

## 开发环境

```bash
git clone https://github.com/zhaozhaozz/MissionCrew.git
cd MissionCrew
uv sync                  # 创建 .venv 并安装含 dev 组的全部依赖
uv run pytest -q         # 完整测试(约 2 分钟;markdown 渲染测试需要本机有 node,没有会自动跳过)
uv run mc serve          # 本地起服务,访问 http://127.0.0.1:8321
```

要求 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。CI 会在 3.10–3.14 上跑测试并构建 wheel，提 PR 前请至少在本机跑通完整测试。

## 代码约定

- 代码结构与分层规则见 [AGENTS.md](AGENTS.md)，其中的分层规则有测试钉住（`tests/test_runtime_abstraction.py`），违反会直接失败。
- 接入新的 Agent CLI：在 `missioncrew/runtime/clis/` 加一个声明模块并追加进 `clis.SPECS`，不要改执行器；步骤见 [docs/runtimes.md](docs/runtimes.md) 的「接入新工具」。
- 改动 `core/models.py` 等序列化字段时注意向后兼容：旧数据库里的记录经 `from_dict` 读入，未知字段会抛 `TypeError`。
- 行为变化要有测试；修 bug 请附回归测试。
- 注释和文档用中文，与现有代码保持一致；提交信息一行说明做了什么，正文按需补充原因。

## 提交 PR

1. 有进行中的版本分支(`release/vX.Y.Z`)时从该分支拉，没有进行中的版本分支才从 `main` 拉；保持一个 PR 只做一件事。
2. 跑通 `uv run pytest -q`；涉及前端改动请在浏览器里实际验证。
3. 涉及对外行为（CLI 参数、API、Agent Tool 动作、目录布局）的改动，同步更新 `README.md` 或 `docs/` 中对应的文档。
4. PR 描述写清动机、做法和验证方式。

## 版本与发布

- 版本号遵循 SemVer(`MAJOR.MINOR.PATCH`,PEP 440 兼容)。1.0 之前:含新功能或不兼容变更 → MINOR+1(不兼容变更必须在 CHANGELOG 的「不兼容」小节写清迁移方式);只有修复 → PATCH+1;预发布版本写 `X.Y.ZrcN`。Agent Tool API、数据目录结构、CLI 稳定后再升级到 1.0。
- 单一版本来源是 `missioncrew/__init__.py::__version__`,`pyproject.toml`、`missioncrew/runtime/acp.py` 里的 clientInfo 等其他位置一律引用它,不得再硬编码版本号。
- 每个版本一个分支 `release/vX.Y.Z`(不直接叫 `vX.Y.Z`,避免与同名 tag 冲突)。第一个版本分支从 `main` 创建，之后的版本分支从上一个版本分支创建；日常开发分支从当前进行中的版本分支拉出，完成后以 `--no-ff` 合并回该版本分支。版本分支何时合入 `main` 由人类决定。
- 发布流程:在版本分支上把 `CHANGELOG.md` 的 `[Unreleased]` 段落改成对应版本段(附日期)→ 修改 `__version__` → 提交 `chore: 发布 vX.Y.Z`。默认不打 tag；需要时由人类决定，格式为 `vX.Y.Z`。
- 用户可感知的 feat/fix 提交,要同时在 `CHANGELOG.md` 的 `[Unreleased]` 里补一行(按「新增/变更/修复/不兼容」分类),不要留到发布前才补。

## 安全问题

请不要在公开 Issue 中披露漏洞，报告方式见 [SECURITY.md](SECURITY.md)。
