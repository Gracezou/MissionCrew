# 与上游的分歧清单

本页记录 fork 中刻意保留的行为，供同步 zhaozhaozz/MissionCrew 上游时核对。代码与英文说明中的 `fork-divergence` 注释标出关键位置；本页是维护者清单，只保留中文版，不进入 Agent 的英文平台文档副本。

| 位置 | 分歧内容（一句话） | 对应测试 | 为什么不回推上游 |
|---|---|---|---|
| `missioncrew/core/seed.py`、`missioncrew/api/roles.py`、`missioncrew/api/projects.py`、`missioncrew/cli.py`、`docs/en/runtimes.md`、`docs/en/cli.md`、`README.en.md` | 全局模板保存新项目的默认 `enabled` 状态，默认主控跳过停用和 `manual_only` 模板。 | `tests/test_roles.py::test_global_role_template_enabled_state_survives_save_and_import`、`test_new_project_orchestrator_skips_disabled_and_manual_only_templates`；`tests/test_cli.py::test_project_add_uses_first_orchestrator_role_template` | 上游按模板首项选主控；这是 fork 的模板语义与现有项目兼容策略，同步时须先确认上游是否已有等价设计。 |
| `missioncrew/api/roles.py`、`missioncrew/web/js/roles.js` | 旧请求或角色文件未提供 `manual_only` 时保留既有值，主控守卫按合并后的生效值校验。 | `tests/test_roles.py::test_legacy_role_request_keeps_manual_only`、`test_role_guard_uses_effective_manual_only_before_writing`、`test_role_file_item_preserves_absent_manual_only` | 避免旧客户端或旧导出文件静默撤掉仅人工点名的权限边界；依赖 fork 的兼容承诺。 |
| `missioncrew/api/projects.py`、`missioncrew/cli.py`、`docs/en/cli.md` | 更新项目时省略主控字段保留现值且不重校验，显式选择仍完整校验。 | `tests/test_roles.py::test_project_update_without_orchestrator_keeps_unavailable_role`、`test_project_update_explicit_orchestrator_validates_role`；`tests/test_cli.py::test_project_add_orchestrator_validation_matches_explicitness` | 用量联动停用或删除旧主控时仍允许改名等无关更新；上游需先确定相同的 API/CLI 显式性规则。 |
| `missioncrew/collab/chat.py` | 主控缺失或停用时拒收未点名的人类消息，显式点名其他可用角色仍可执行。 | `tests/test_chat.py::test_human_unmentioned_message_rejects_unavailable_orchestrator`、`test_human_can_mention_enabled_role_when_orchestrator_disabled` | 防止消息显示已发送却无人处理；这是 fork 的聊天接收语义，不能在同步时退回静默行为。 |
| `missioncrew/collab/project_imports.py`、`docs/en/resources.md`、`docs/en/README.md` | 准则、Skill、自动化支持跨项目复制，逐项返回跳过、覆盖或失败结果。 | `tests/test_project_imports.py::test_cross_project_import_copies_all_resources_without_mutating_source`、`test_cross_project_import_skip_and_overwrite_conflicts` | 复制功能及其响应契约是 fork 扩展，上游目前没有对应入口。 |
| `missioncrew/collab/project_imports.py`、`docs/en/resources.md` | 复制失败对用户脱敏绝对路径，同时在本机审计保留完整异常与来源。 | `tests/test_project_imports.py::test_cross_project_import_copy_error_reason_hides_absolute_paths`、`test_cross_project_import_failure_audits_full_exception`、`test_cross_project_import_data_error_response_hides_home` | 同时满足 fork 的本机排错与对外响应安全边界；上游没有同一复制流程可直接套用。 |
| `missioncrew/api/system.py`、`missioncrew/core/store.py`、`missioncrew/web/js/runtime-status.js`、`docs/en/cli.md` | 审计记录可在 Web 运行状态页按项目过滤、搜索并查看脱敏后的完整详情。 | `tests/test_audit.py::test_audit_filters_by_project_and_searches_action_or_detail`、`tests/test_audit.py::test_audit_redacts_credentials_without_truncating_other_detail` | fork 的可观测性补充，上游无此页面。 |
| `missioncrew/collab/project_imports.py`、`missioncrew/core/store.py` | 复制自动化清空 webhook 身份、凭据和运行状态，覆盖同名定义与清理旧运行记录在 `replace_automation` 单事务内完成。 | `tests/test_project_imports.py::test_cross_project_import_automation_never_inherits_webhook_identity`、`test_cross_project_import_overwrite_automation_clears_runs_and_tokens`、`test_automation_overwrite_replaces_definition_in_one_transaction`、`test_automation_overwrite_rejects_different_id_without_mutation` | 防止复制品继承外部触发权或失败后只剩半次覆盖；这是 fork 复制入口的安全与原子性要求。 |
| `missioncrew/collab/skills.py`、`missioncrew/collab/recycle_bin.py`、`missioncrew/collab/project_imports.py` | Skill 锁先于回收站锁取得，`recycle_skill` 在检查、归档和删除期间一直持有 Skill 锁。 | `tests/test_recycle_bin.py::test_skill_restore_and_overwrite_import_do_not_deadlock`、`test_recycle_skill_archives_the_package_it_checked_not_a_concurrent_import` | 修复 fork 的覆盖导入与回收站并发路径中的死锁和错删窗口；同步时先核对上游是否仍有相同锁序。 |
| `missioncrew/runtime/clis/pi.py`、`docs/en/runtimes.md` | pi 优先使用平台 vendored 安装，缺失时回退系统 PATH，Homebrew 安装走 `brew upgrade`。 | `tests/test_pi.py::test_pi_falls_back_to_system_binary_on_path`、`test_pi_update_plan_follows_install_source` | fork 支持用户已有的系统级 pi，并按真实来源升级，避免误写全局 npm 安装。 |
| `tests/test_antigravity_usage.py` | Antigravity 假 CLI 探测超时设为 2 秒。 | `tests/test_antigravity_usage.py::test_probe_timeout_reaps_process_and_missing_binary_is_unavailable` | macOS 首次执行新脚本可能慢于 0.2 秒；这是跨系统测试稳定性的校准。 |

## 同步上游时的核对步骤

1. `git fetch` 更新上游引用，在临时分支合并上游，保留版本分支可回退。
2. 按上表逐条核对行为和 `fork-divergence` 标记仍在，运行对应测试及全量 pytest。
3. 重点查上游删除或改名的符号，尤其是 `replace_automation`、Skill/回收站锁入口、pi 定位钩子和项目主控选择函数。
4. 对照双方各自新增的模型/API 字段，检查模板复制、角色导入、自动化复制是否漏传或误继承。

备忘：上游 webhook 验签是三种凭据任一种验过即通过；最弱的 Bearer 口令决定整体强度。做 webhook 相关改动时核对这一设计，本清单不修改它。
