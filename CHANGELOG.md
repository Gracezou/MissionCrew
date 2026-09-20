# 更新日志

本文件记录 MissionCrew 各版本对用户可感知的变化,格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

版本号遵循 SemVer(`MAJOR.MINOR.PATCH`,PEP 440 兼容):1.0 之前,含新功能或不兼容变更升 MINOR,只有修复升 PATCH,不兼容变更会在下方的「不兼容」小节写明迁移方式;分支与发布流程见 [CONTRIBUTING.md](CONTRIBUTING.md#版本与发布)。

## [Unreleased]

### 新增

- 准则、Skill 与自动化支持批量复制到其他项目，可选择跳过或覆盖同名条目；复制后的自动化默认停用并清空运行状态

## [0.5.0] - 2026-09-16

首个正式记录的版本。以下按主题归纳自初始化提交以来(`79fc57c..HEAD`)对用户可感知的变化。

### 新增

- claude、codex 从打印模式升级为原生双向协议 provider(stream-json / app-server),支持后台命令与自唤醒汇报
- pi 从打印模式升级为 RPC 原生 provider,支持裸 OpenAI / Anthropic / Google 兼容 API 的自定义模型接入
- pi 找不到平台内置安装时回退到 PATH 上的系统级安装,Homebrew 安装走 `brew upgrade` 更新
- 新增内置 ACP stdio 客户端(长驻会话,空闲 30 分钟自动回收,自动应答权限请求):kimi、kiro、qoder、trae 由此接入,grok 与 copilot 从打印模式迁移过来
- 展示 OpenCode 执行阶段进度,捕获会话 id 支持续接
- 项目可选「无主控」模式,所有角色同权、可互相派发
- Task 重构为标签驱动的 Issue:自定义看板列(标签表达式/属性分组)、外部数据源同步(如 GitCode Issue)、追加式状态简报、可恢复删除
- 定时自动化:crontab 触发脚本调用 Agent Tool API,新任务命中标签规则后自动派发进频道
- 文档库、准则、Skill 升级为结构化管理界面,由平台 Git 记录版本历史,支持比较与恢复;支持完整 Skill 包版本化导入
- 项目回收站统一管理已删除的文档/准则/Skill,以及统一的资源 URL 体系(频道/任务/文档/面板/准则/Skill)
- 面板支持对话式创建、自定义数据源绑定与多种展示形式
- Agent Tool API:认证令牌绑定 Run,提供 `document.publish/rename`、`task.create/update/brief` 等动作
- 角色支持推理力度(effort)按模型动态发现与配置
- 「运行状态」页展示各账号(Codex/Claude/Kimi/Grok)用量与限额窗口,角色可联动限额自动停用/恢复
- 聊天支持文件/图片拖拽上传、粘贴长文本自动转附件、图片悬浮预览
- 聊天内联展示实时运行过程,含 Tool 回执折叠分组、用量卡片与执行时长
- Markdown 支持 Mermaid 图表渲染,代码块与行内代码带复制按钮
- 移动端布局适配
- 支持 pm2 常驻部署,统一 `scripts/serve.sh` 入口
- 平台数据目录默认统一为 `~/.missioncrew`,支持整体搬迁(内部链接改相对路径)并可用 `MISSIONCREW_HOME` 覆盖
- Web 界面改为 History API 路由,刷新后保持当前视图

### 变更

- 前端从单文件拆分为骨架 + CSS + 按页面分 JS;后端按 `api/core/runtime/collab` 分层拆包,分层规则有测试钉住
- Runtime 侧工具静态声明下沉到 `runtime/clis/` 按工具拆分,新增工具无需改动执行器
- 各 Runtime 的用量事件在源头统一归一为 `usage/v1` 结构,前端不再依赖工具私有字段名
- 派发机制改为显式 `message.publish` 命令,Agent 正文里的 `@` 一律视为普通文字,不再触发派发
- 总览接口按层拆分(全局/项目/频道任务),启用 ETag 与增量获取,降低轮询体积
- 角色能力收敛为固定的模态/工具事实,偏好改为自由文本描述
- 侧边栏重构为并列可折叠分区,支持拖拽宽度与手工排序
- 聊天并发执行上限从 4 提升到 16,可配置
- Claude 沙箱网络隔离默认关闭,可按需用环境变量启用
- 服务改由 pm2 管理,派发子进程前统一剥离宿主终端环境变量
- `mc serve` 默认只监听 `127.0.0.1`,局域网访问需显式指定 host
- 修复 macOS(APFS 不区分大小写文件系统)上 Skill 包导入测试的误判断言(内部改动)
- 版本号统一到 `missioncrew/__init__.py::__version__` 单一来源,清理其余硬编码版本字面量(内部改动)

### 修复

- pi 自定义模型接入里,用分号或逗号分隔的模型 id 不再原样落库导致调用报 `unknown provider` 400;前端按换行/中英文逗号分号拆分,后端拒绝含空白、逗号、分号的 id
- 应用停机时等待聊天执行池收尾,避免中途中断的 Agent 运行状态残留
- 多项运行时会话恢复、后台任务追踪与多 Agent 并行时的时序/竞态问题
- 多项界面显示问题:实时输出框内容截断、任务看板筛选与分组冲突、弹窗遮罩层级、窄屏布局遮挡等
- Markdown 列表按缩进递归解析、表格内行内代码解析等渲染问题修复

## [0.4.0] - 2026-07-17

初始化提交,项目基线版本,此前未做正式版本记录。
