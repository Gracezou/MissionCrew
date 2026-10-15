# MissionCrew

[中文](README.md) | English

MissionCrew is a local multi-agent collaboration platform. It turns the Agent CLIs already installed on your machine (Claude Code, Codex, Grok, …) into a set of roles that work together like a team inside chat channels. You state what you need in a chat box and either address a specific role directly or let the orchestrator coordinate several roles to get it done.

The point is to combine several local Agent CLIs so you can play to the strengths of different tools and models — like a group chat where different tasks go to different models. Add your own guidelines and skills to build the agent team you want.

![One channel per issue or feature; a one-line request from a human, dispatched by the orchestrator to dev and tester and summarised back](docs/images/chat.jpg)

## Get started

### Install and run

Requirements: Python 3.10+, [uv](https://docs.astral.sh/uv/), Node.js 18+ (npm is needed for pm2 and pi), and at least one logged-in Agent CLI on this machine (such as `claude` or `codex`).

```bash
uv sync            # create .venv and install dependencies
uv run mc serve    # start the web service on 127.0.0.1:8321
```

Open `http://127.0.0.1:8321`, go to **Global settings** and click **Re-detect** in the runtime list. The platform scans the installed Agent CLIs, registers them, and creates a `default` project with a set of default roles (`@lead` orchestrator, `@dev`, `@reviewer`, …) and a `general` channel. Each role's runtime/model and persona can be adjusted in **Project settings**. Everything else happens in the browser; the three scenarios below go from simple to fully automated. (The screenshots show the Chinese UI.)

### Scenario 1: one task, several agents in the same conversation

Getting several agents to cooperate used to mean asking one tool for a plan, copying the answer into another tool for review, and pasting the feedback back. In MissionCrew a channel is one topic and every agent reads the same channel history, so whoever replies next already has the context — no relaying prompts by hand.

1. Create a channel for the task with the ＋ next to **Channels** in the sidebar; it can be bound to a local repository as its working directory.
2. Type `@` in the composer to pick a role. Pick a single role and it executes directly, with the result staying in the channel.
3. To have several agents answer the same question at once, set the project's orchestrator to **No orchestrator** in **Project settings**: mentioning several roles then starts each of them independently (in a project with an orchestrator, mentioning several roles hands the list to the orchestrator).
4. Mention another role in the next message and it can read every answer above. "Fix both, taking codex's finding into account" needs no copy-paste.

![No-orchestrator project: @claude and @codex investigate the same problem, Claude fixes it using Codex's finding, Codex reviews and Kimi adds tests](docs/images/get-started/multi-agent.jpg)

Here the human mentions @claude and @codex together to investigate a slow order endpoint; they find an N+1 query and a missing index respectively. Then @claude fixes both using Codex's finding, @codex reviews the change and @kimi adds regression tests — all without copying anything between tools.

**Continue on your phone**: the service only listens on loopback by default. To follow progress or send a quick instruction from your phone, start it with `uv run mc serve --host 0.0.0.0` (or `MISSIONCREW_HOST=0.0.0.0 scripts/serve.sh start` under pm2), join the same LAN and open `http://<your computer's LAN IP>:8321`; the page switches to a mobile layout. There is no authentication, so only do this on a trusted network — see [SECURITY.md](SECURITY.md).

<img src="docs/images/get-started/mobile.jpg" width="300" alt="The same channel on a phone">

### Scenario 2: your own workflow, driven end to end by the orchestrator

Write down how your team works as guidelines, package recurring procedures as skills, and pick an orchestrator. From then on a one-line request is enough: the orchestrator hands the work to different roles in turn — analysis → development → review → testing → merge — and you only state the request at the start and accept the result at the end.

1. **Write the workflow as a guideline.** Use the ＋ next to **Guidelines** to create a Markdown file stating who owns each stage and what "done" means. The guideline list is injected into every role's context, and the orchestrator dispatches accordingly.

   ![Guideline dev-workflow: which role owns analysis, development, review, testing and merge, and what each stage must deliver](docs/images/get-started/guideline.jpg)

2. **Add your own skills.** On the Skills page upload a ZIP, import a local directory, or simply copy a directory containing `SKILL.md` into the drop directory shown on the page; the platform discovers it and versions it with Git (see [Project skills](docs/skills.md)). The `local-ci`, `code-review` and `git-commit` skills in the screenshot back the self-test, review checklist and commit conventions of the workflow.

   ![Skills page: the full local-ci directory and its SKILL.md preview](docs/images/get-started/skills.jpg)

3. **Pick an orchestrator.** In **Project settings → Project info** choose the project's orchestrator role, and give each role the runtime and model that suits it — for example Claude Opus for the orchestrator and analyst, Codex for the developer, Claude Sonnet for the reviewer and Kimi for the tester.

   ![Project settings: @lead selected as the orchestrator](docs/images/get-started/orchestrator.jpg)

4. **Start with one sentence.** Describe the request in a channel without mentioning anyone; it goes to the orchestrator by default. The orchestrator dispatches stage by stage following the guideline, every role's result flows back to it automatically, and when review finds a problem the orchestrator sends the work back to the developer until every stage passes.

   ![The orchestrator follows dev-workflow: analysis, development, review (one round of fixes), testing, merge, then a summary](docs/images/get-started/workflow.jpg)

Guidelines, skills and channels can also be maintained by the orchestrator straight from the chat, e.g. "put these review requirements into the guideline"; it calls the matching action and posts the link back to the channel.

### Scenario 3: automation — pull GitHub issues on a schedule and handle them automatically

A scheduled script syncs an external list into tasks, and task auto-processing rules dispatch matching new tasks to roles; together they form an unattended pipeline. See [Scheduled automation and task auto-processing](docs/automation.md) (Chinese) for how it works.

1. **Write the sync script.** Use the ＋ next to **Automation** in the sidebar and set the cron to `*/30 * * * *`. The script fetches issues with `gh issue list`, turns them into cards and calls the platform action `board_source.save`. Every run gets a one-time token that only allows the actions on the script's allow-list; the run history shows each run's exit code and output.

   ![Automation script syncing GitHub issues every 30 minutes, with the run history below](docs/images/get-started/automation.jpg)

   `board_source.save` overwrites each card by issue number on every sync, so derive the status from GitHub instead of hard-coding it — otherwise progress written back by agents is wiped out by the next sync. The script below maps closed issues to done and the `处理中` / `待验证` (in progress / to verify) issue labels to the matching status:

   <details>
   <summary>Example sync script</summary>

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   gh issue list --repo acme/webshop --state all --limit 100 \
     --json number,title,body,state,labels,url > issues.json

   # GitHub is the source of truth: closed → done, labelled 处理中/待验证 → that status, otherwise to do
   jq '[.[] | [.labels[].name] as $l | {
         id: ("#" + (.number | tostring)),
         title: ("#" + (.number | tostring) + " " + .title),
         summary: (.body // "" | .[0:300]),
         labels: ($l - ["处理中", "待验证"]),
         url: .url,
         status: (if .state == "CLOSED" then "已完成"
                  elif ($l | index("待验证")) then "待验证"
                  elif ($l | index("处理中")) then "处理中"
                  else "待处理" end)
       }]' issues.json > cards.json

   "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call board_source.save \
     --arguments "$(jq -c '{id: "github", name: "GitHub Issues",
       columns: [{value: "待处理"}, {value: "处理中"}, {value: "待验证"}, {value: "已完成"}],
       cards: .}' cards.json)"
   ```

   </details>

2. **Create a board and a rule.** Create a task board bound to the `github` data source under **Dashboards** in the sidebar (or let the orchestrator create it with `dashboard.save`). Synced issues are ordinary tasks, shown in columns by status. Click the ⚡ on the to-do column header, write the handling instructions and mention the responsible role: from then on every newly synced task that matches the column's label expression is dispatched automatically. Rules only apply to newly created tasks, so run the script once by hand to import existing issues before enabling the rule; otherwise all existing issues would be dispatched at once.

   ![GitHub Issues board with to do / in progress / to verify / done columns](docs/images/get-started/issue-board.jpg)

   <img src="docs/images/get-started/task-rule.jpg" width="640" alt="Auto-processing rule on the to-do column: @lead decides the issue type and fixes bugs following dev-workflow">

3. **Done automatically.** When a new issue matches the rule, the platform posts the task details to the channel as *Task auto rule*. The orchestrator runs analysis and the fix following `dev-workflow`, replies on the issue, adds the `待验证` label and writes the status back with `task.brief`; the card on the board moves to "to verify". You just follow the channel and step in when needed.

   ![Issue #231 auto-dispatched to the orchestrator: analysis, fix, PR, and the task status written back as to verify](docs/images/get-started/auto-dispatch.jpg)

## Core concepts

MissionCrew exposes a set of tool commands to agents, so an agent operates platform resources by running commands. The main concepts are:

- **Project**: configuration and resources are isolated per project, so several efforts can run side by side.
- **Runtime**: an Agent CLI installed on this machine (Claude Code, Codex, …). Auto-detected and shared by all projects.
- **Role**: a runtime configuration plus a persona — for example one model acting as the developer and another as the tester.
- **Orchestrator**: one role chosen as the lead. It has more authority than other roles (it can create channels, for instance). A message that mentions nobody goes to the orchestrator by default, and replies from other roles automatically trigger it.
- **Channel**: a chat room. Every agent can read the conversation history and collaborate in it.
- **Guidelines**: a special kind of document whose list is injected into context. Use them to define workflows such as development or testing requirements.
- **Docs**: every project gets a versioned document library, a good home for material that does not belong in the code repository.
- **Automation**: a script triggered on a schedule or by hand that calls the MissionCrew API to perform actions.

**None of these resources need to be managed by hand.** Channels, tasks, documents, guidelines, skills, dashboards and automation scripts can all be created and maintained by the orchestrator through the Agent Tool: say "split the login rework into tasks and open a channel to track it", "write up what we just concluded as a document" or "put these testing requirements into a guideline" in the chat, and the orchestrator calls the matching actions and posts the results back as links; other roles can create and update tasks and publish documents too. The web UI is mainly for viewing, reviewing and the occasional manual tweak, not the everyday entry point for data entry.

Detailed docs (Chinese): [Runtimes](docs/runtimes.md), [Agent Tool API](docs/agent-tool-api.md), [Resources and URLs](docs/resources.md), [Harness workspace boundaries](docs/agent-harness-workspace.md), [Project skills](docs/skills.md), [CLI](docs/cli.md).

## Page guide

- **Chat**: the project switcher at the top is the entry point for every page. Type a request in a channel and it goes to the orchestrator by default. Typing `@` opens a role picker: pick one role and it executes directly, with the result staying in the channel; pick several and only the orchestrator starts, holding the full list and deciding whether to run them in parallel, in sequence, or to re-assign (in a project without an orchestrator, each starts independently). A hand-typed `@name` is plain text and never triggers anyone.
- **Channels**: create, archive and delete channels from the sidebar. A channel records its purpose and working directory and can be bound to a real code repository. While agents are queued or running, the composer offers *Stop agent / Stop all*.
- **Task board**: tasks are issue-like — title, body, status, labels, channel bindings and append-only status briefs. *Hand to Lead* simply posts a message to the orchestrator in the bound channel; everything after that is ordinary chat collaboration, there is no separate task execution loop.
- **Project settings**: roles (fixed runtime/model + positioning + capabilities + preferences), multiple guideline Markdown files, complete skill packages, the versioned document library and custom dashboards. Documents, guidelines and skill packages are versioned in their own Git repositories with history, diff and restore; deleted items go to the project recycle bin.
- **Global settings**: role templates for new projects, the runtime list (install state, version, on/off switch, model list) and custom model providers.
- **Runtime status**: global runtime instances, call history, and the quota windows / reset times of the Codex, Claude, Kimi and Grok accounts logged in on this machine. Roles can opt into usage linkage individually: when a quota is exhausted only opted-in roles are disabled automatically, and they come back once the window resets.

## Security and data directory

**There is no authentication of any kind.** The web UI and API can browse local directories and dispatch agents that run commands, so `mc serve` listens on `127.0.0.1` only by default. To reach it from other devices on your LAN, pass `--host 0.0.0.0` explicitly (or set `MISSIONCREW_HOST`), do so only on a trusted network, and never expose it to the internet — see [SECURITY.md](SECURITY.md). The chat concurrency limit defaults to 16 and can be changed with `--chat-workers <N>` or `MISSIONCREW_CHAT_MAX_WORKERS`.

Platform data (SQLite, document libraries, agent workspaces, logs) lives in `.missioncrew/` under the current directory; override it with `MISSIONCREW_HOME`. Nothing is ever written into your code repositories.

## Running under pm2

For day-to-day use let [pm2](https://pm2.keymetrics.io/) manage the service. The repository ships a single entry point, `scripts/serve.sh` (configured in `ecosystem.config.cjs`, process name `missioncrew`, started as `.venv/bin/python -m missioncrew.cli serve`). Do not start the service by hand with `nohup`/`setsid`:

```bash
npm install -g pm2          # once

scripts/serve.sh start      # start (loopback only; for LAN access: MISSIONCREW_HOST=0.0.0.0 scripts/serve.sh start)
scripts/serve.sh status     # pm2 status missioncrew
scripts/serve.sh logs 100   # last 100 log lines
scripts/serve.sh restart    # restart
scripts/serve.sh stop       # stop
```

Conventions:

- The service inherits the caller's environment as usual — in particular the full `PATH`, which runtime detection relies on to find the installed Agent CLIs. After installing a new CLI run `scripts/serve.sh restart` (it passes `--update-env`) so the new tool is detected. Variables injected by host terminals (`VSCODE_*`, `CLAUDE_*`, `GIT_ASKPASS`, `SSH_AUTH_SOCK`, …) are stripped by the dispatch layer before agent subprocesses start, regardless of how the service was launched.
- pm2 captures stdout/stderr into `.missioncrew/server.log`.
- Health-check an endpoint that reads the database, such as `/api/overview`; the home page is static HTML and a 200 there proves nothing.
- Before restarting or stopping, make sure no agent is running: no active instance on the *Runtime status* page and no queued or running task in any channel. pm2 gives the service up to 30 seconds to shut down gracefully and end the resident CLI sessions one by one.

## Supported local agents

*Re-detect* probes `PATH` for each tool in the table below (except pi, see below) and keeps one registry record per tool; roles are bound to a fixed runtime and model. Three integration styles:

- **Native protocol**: claude, codex, pi and Antigravity each have a dedicated provider for structured events, session resume and reasoning effort. Interactive permissions are available when the tool supports a bidirectional protocol.
- **ACP over stdio**: the CLI runs as a long-lived JSON-RPC server on stdio and the platform answers its permission requests automatically.
- **Print mode**: the prompt is passed through a built-in command template and sessions are resumed with each tool's session/resume flags.

| CLI | adapter | Integration |
|---|---|---|
| `claude` (Claude Code) | `claude_code` | Native bidirectional stream-json; built-in haiku / sonnet / opus / fable aliases |
| `codex` (OpenAI Codex) | `codex` | Native app-server; model list read from the current account |
| `agy` (Google Antigravity CLI) | `antigravity` | Native headless stream-json; conversation resume, model discovery and low / medium / high effort |
| `pi` | `pi` | Native RPC (vendored install); executes custom API models |
| `grok` (Grok Build) | `grok_build` | ACP stdio |
| `copilot` (GitHub Copilot CLI) | `copilot` | ACP stdio |
| `kimi` (Kimi CLI) | `kimi` | ACP stdio |
| `kiro-cli` (Kiro) | `kiro` | ACP stdio |
| `qodercli` (Qoder) | `qoder` | ACP stdio |
| `traecli` (Trae) | `trae` | ACP stdio |
| `opencode` | `opencode` | Print mode |
| `cursor-agent` (Cursor) | `cursor` | Print mode |
| `codebuddy` | `codebuddy` | Print mode |

For Antigravity, install the [official CLI](https://antigravity.google/docs/cli/getting-started/), sign in by running `agy` once, then click **Re-detect** and bind a role to `antigravity`. Headless execution supports automatic approval; interactive approval, enforced read-only access and network denial are unavailable.

Protocol flows, detection and upgrade mechanics, where model lists come from, and how to add a new tool are described in [docs/runtimes.md](docs/runtimes.md).

### Using API models (via pi)

Besides local CLIs, MissionCrew can talk directly to **OpenAI / Anthropic compatible APIs** — self-hosted inference servers, gateways, third-party hosting — over the OpenAI Chat Completions, OpenAI Responses, Anthropic Messages and Google Generative AI protocols. These models are executed by [pi](https://www.npmjs.com/package/@mariozechner/pi-coding-agent): the platform does not reimplement an agent loop; it reuses pi's tool execution and session management through pi's RPC mode. Setup:

1. **Install pi.** pi is vendored inside the platform data directory; detection only recognises that copy and never a system-wide pi. Node.js/npm is required. Install it once by hand (adjust the prefix if you changed `MISSIONCREW_HOME`):

   ```bash
   npm install --prefix .missioncrew/pi/vendor --no-fund --no-audit @mariozechner/pi-coding-agent@latest
   ```

   Then click **Re-detect** in *Global settings* to register and enable `pi`. Later upgrades use the *Update* button on that page and only touch the vendor directory — never a global `-g` install.
2. **Add a provider.** Under *Global settings → Custom model providers* add an entry: name, protocol, base URL, API key (a literal or a `$ENV_VAR` reference; keys are never sent back to the browser) and the list of model ids.
3. **Bind a role.** After saving, the models appear in the role editor's model list as `provider/model-id`; give the role runtime `pi` and that model.

Provider configuration is stored in `.missioncrew/pi/agent/models.json` (mode 0600) and sessions in `.missioncrew/pi/sessions`; `~/.pi` is never read or written. See the "pi RPC" and "Custom model providers" sections of [docs/runtimes.md](docs/runtimes.md) for details.

## License

Released under the [MIT License](LICENSE).

## Acknowledgements

Inspired by [multica](https://github.com/multica-ai/multica), [hapi](https://github.com/tiann/hapi) and other happy-family apps.

Thanks to the [linux.do](https://linux.do/) community.
