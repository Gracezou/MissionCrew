# Scheduled automation and automatic task handling

Two mechanisms combine into one automation pipeline:

1. **Automation scripts (Automation)**: a script is triggered on a crontab schedule (or manually), the platform issues a one-time Agent Tool token for that run, and the script uses it to call platform actions such as creating tasks, syncing data sources, posting messages and publishing documents; this is the "scripts update MissionCrew" capability.
2. **Automatic task-handling rules (task auto rules)**: when the labels of a newly created task match a rule's label expression, the platform automatically dispatches the task into a channel for a role to handle, exactly the same way as the manual "Hand off to orchestrator" (交给主控处理) action.

Combined, items from external systems can flow through fully automatically: **GitHub/GitCode issues, PRs, alerts → synced into tasks by a scheduled script → a rule matches → automatically dispatched to a role → the agent handles it in the channel → results written back through `task.brief`/`task.update`**. Humans only follow progress in the channel and step in when needed.

## Automation scripts

- **Creation**: the ＋ in the "Automation" (自动化) section of the Web sidebar, or ask the orchestrator in chat to create one with `automation.save`; before changing a script the orchestrator reads its current text with `automation.list` (with `id`), and afterwards it can test-run it right away with `automation.run`. Fields: `id`, `name`, `description`, `script` (run as an executable when it has a shebang, otherwise passed to bash), `cron` (five-field crontab, empty = manual trigger only), `enabled`, `actions`, `timeout_seconds`.
- **Scheduling**: all cron triggers go through the unified scheduling entry point; a script is not started again while its previous run is still in progress; triggers missed while the service is down are not run afterwards.
- **Permissions**: the script's own `actions` allowlist is the source of truth for the actions available to the script identity; the default is `task.create`, `task.update`, `task.brief`, `message.publish`, `document.publish`, `dashboard.save`, `board_source.save`; the audit actor is recorded as `automation:<project>:<script>`.
- **Run records**: the automation detail page shows each run's trigger type, exit code and stdout/stderr. A non-zero exit code is recorded as a failure. The orchestrator reads the latest runs with `automation.list` and `id` (only the tail of the output is returned) to investigate a failing script.

### Execution environment

- **Working directory**: `<data-directory>/automations/<project-id>/<script-short-id>/`; it is kept across runs and can hold state files (for example, the time of the last sync). Before each run, the platform writes the script body to `script` in this directory, so do not use this file name for anything else.
- **Environment variables**: the script inherits the environment of the MissionCrew service process, and `PATH` as well as the login state of CLIs such as `gh` and `gitcode` all come from the user who started the service; the platform additionally injects:

  | Variable | Meaning |
  | --- | --- |
  | `MISSIONCREW_AGENT_TOOL_PYTHON` | The platform's own Python interpreter; use it to run `-m missioncrew.agent_tool` |
  | `MISSIONCREW_AGENT_TOOL_URL` | Root URL of the Agent Tool API |
  | `MISSIONCREW_AGENT_TOKEN_FILE` | The one-time token file of this run (`.agent-tool-token` in the working directory); the CLI reads it itself, so the script does not need to read it and must not print it |
  | `MISSIONCREW_PROJECT_ID` | Project id |
  | `MISSIONCREW_AUTOMATION_ID` | Script id, in the format `<project-id>:<script-short-id>` |

- **Token and timeout**: the token is revoked as soon as the run ends, and also when the run is killed for exceeding its timeout; a script that runs longer than `timeout_seconds` is terminated and recorded as timed out.
- **Calling platform actions**:

  ```bash
  "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call <action> --arguments '<JSON>'

  # For large arguments (e.g. syncing a whole batch of cards), read them from a file or stdin to avoid exceeding the length limit of a single command-line argument
  jq -c '{id: "github", cards: .}' cards.json \
    | "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call board_source.save --arguments-file -
  ```

  The CLI exits with code 0 on success; when the platform returns an error, the connection fails or the arguments are invalid, it exits with a non-zero code and prints the error JSON to stdout. Put `set -euo pipefail` at the top of bash scripts, so that a failure at any step makes the run fail and leaves a record.

## Syncing external lists into tasks (data sources)

`board_source.save` creates or updates a custom task data source: `columns` declares the status values (order and color), and `cards` are **synced as a whole** by id into this source's tasks, which adds new entries, updates existing ones, and deletes entries not present this time.

> **Tasks that are not in this call's `cards` are permanently deleted together with all of their status briefs, and they do not go to the recycle bin.** So when fetching the external list fails, the script must exit with a non-zero code and must not save an empty or partial list; paginated fetches must fetch every page; and the rule that generates card `id`s must stay the same across runs, otherwise every run deletes the old tasks and creates a new copy.

### Card fields

| Field | Format | Description |
| --- | --- | --- |
| `id` | String, required | Stable identifier of the external item, e.g. `#231`; sync matches existing tasks by it |
| `title` | String | Required when creating a task; may be omitted when updating |
| `summary` | String | Short description |
| `status` | String | Status text, stored as a `status: <text>` label; see the merge rules below |
| `labels` | Array of strings | Plain labels (e.g. `bug`) or `property: value` property labels (e.g. `owner: 张三`) |
| `properties` | Object | `{property: value \| [value...] \| null}`; sets an individual property, and `null` clears it |
| `url` | String | Original link; agents see it in the `url` field of the task snapshot and in the automatic dispatch message |
| `meta` | Array of strings | Note text shown on the card, e.g. `["@alice", "3 条评论"]` |
| `updated_at` | Number, Unix seconds | Used as the creation and update time only when a task is created; defaults to the time of the sync |

`columns` is an array of `[{value, color}]`: if it is omitted when a data source is created, the defaults are the four statuses `待处理` (to do), `处理中` (in progress), `已阻塞` (blocked) and `已完成` (done); if it is omitted on update, the existing status values are kept, and if it is passed, it replaces them entirely. The status text itself is not validated, so you can write values outside `columns`, but such tasks do not appear on a task board split into columns by status.

### Merge rules

Existing tasks are merged on the principle of "**change only what the card carries, keep what it omits**", so status that agents or humans have advanced locally and properties they have added are not wiped out by the next sync:

- `title`, `summary`, `url`, `meta`: overwritten only when present; a `null` value is the same as absent.
- `status`: set only when present; when absent, the local status is kept, and newly created tasks land in the first status value. Scripts should include it only when the external source has a definite conclusion, for example `已完成` when an issue is closed.
- Plain labels in `labels` (such as `bug`): as long as the card carries the `labels` field, the task's plain labels are replaced as a whole group by the card's, and labels removed in the external source are removed as well; when `labels` is an empty array or contains only `property: value` entries, all plain labels are cleared. If you do not want the sync to manage plain labels, do not include `labels`; for markers that must persist locally in the long term, use the `property: value` form and avoid the property names the script sends.
- `property: value` entries in `labels` and `properties` (`{property: value | [value...] | null}`): only properties with the same name are replaced, `null` clears, and properties not mentioned are left untouched. When the same property appears in more than one of them, the `status` field takes precedence over `properties`, and `properties` takes precedence over `labels`.
- Tasks whose content has not changed are not written to the database, so `updated_at` stays the same and does not invalidate the `task.update` snapshot an agent holds.

In other words, the fields the script includes are owned by the external source and are set back to the external values on every sync; the agent handling a task should record progress in the status, `property: value` labels and status briefs, and as long as the script does not include those fields, they are kept.

Synced items are ordinary tasks: they can be opened in the detail view, receive status briefs, be bound to channels and be dispatched; **newly synced tasks immediately go through the project's automatic handling rules**. Synced tasks are not bound to any channel before they are dispatched. To view them, create a taskboard dashboard bound to the source with `dashboard.save`; its columns are defined by label expressions (`filters`), or pass `group_by` to split columns dynamically by property value (in which case the union of the `filters` only defines which cards are in scope).

## Automatic task-handling rules

Rules are stored on the project; each one = **label expression + handling instructions + optional @role mentions + enable/disable switch**.

- **Where to configure**: the ⚡ icon in the column header of any task board (built-in or custom), which prefills the expression with that column's query. Rules can only be configured by a human on the page, and the Agent Tool has no corresponding action; when an agent is asked to set up a rule, it should tell the human to set it through the ⚡ in the column header of the relevant task board, and give the suggested expression and handling instructions.
- **When it fires**: when a task is created (by a human on the page, by an agent with `task.create`, or by a data source sync creating it), it is dispatched if its labels match the first enabled rule; new tasks whose status is already `已完成` are not dispatched. If the labels of an existing task are changed later, no dispatch is triggered even if they match a rule. When `task.create` triggers a dispatch, the result includes `auto_dispatch` (`rule_query`, `sent`).
- **Dispatch behavior**: a message containing the task details (including the original link) is posted in the channel the task is bound to, or in the project's `general` channel if the task has no bound channel, and after dispatch the task status changes to `处理中`; if a single role is mentioned, that role executes directly, and if multiple roles or no role is mentioned, the task is handed to the project orchestrator to schedule; from then on, chat collaboration is reused entirely. The agent that receives this message can see that it comes from an automatic task-handling rule.
- **Expression syntax**: whole-label matching (`bug`, `owner: 张三`) and `property: *` existence matching, combined with `&`, `|`, `!` and parentheses. Examples: `bug & !status: 已完成` (unfinished bugs), `!owner: *` (no owner yet).

## End-to-end example: automatically handling GitCode/GitHub issues

1. **Sync script**: create an automation script with cron `*/30 * * * *` that uses the `gitcode`/`gh` CLI to fetch the repository's issue list, converts it into cards, and calls `board_source.save`. Use the issue number as the card id, write the issue's labels and assignees as `labels` (e.g. `bug`, `owner: xxx`), and write the link as `url`. Include `status: 已完成` only for closed issues and no `status` for open issues, so that the handling progress written back by agents is always kept.

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   # If gh fails, set -e makes the script exit non-zero, so a partial list is never synced
   LIMIT=1000
   gh issue list --repo acme/webshop --state all --limit "$LIMIT" \
     --json number,title,body,state,labels,url > issues.json
   # Reaching the limit means the list may be incomplete, so fail and exit as well
   [ "$(jq length issues.json)" -lt "$LIMIT" ] || { echo "More than $LIMIT issues, increase LIMIT" >&2; exit 1; }

   jq '[.[] | {
         id: ("#" + (.number | tostring)),
         title: ("#" + (.number | tostring) + " " + .title),
         summary: (.body // "" | .[0:300]),
         labels: [.labels[].name],
         url: .url
       } + (if .state == "CLOSED" then {status: "已完成"} else {} end)]' issues.json > cards.json

   jq -c '{id: "github", name: "GitHub Issues",
     columns: [{value: "待处理"}, {value: "处理中"}, {value: "待验证"}, {value: "已完成"}],
     cards: .}' cards.json \
     | "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call board_source.save --arguments-file -
   ```

2. **Task board**: use `dashboard.save` to create a taskboard bound to this source, with columns by status or by owner.
3. **Rule**: a human configures it through the ⚡ in the task board column header, for example with the expression `bug & status: 待处理` and the handling instructions "@dev please analyze and fix this, then append a status brief when done". It is recommended to run the script manually once to sync in the existing issues before enabling the rule, so that the whole backlog is not dispatched at once.
4. **Automatic flow**: a new issue is synced into a task → it matches the rule → it is automatically dispatched to dev → dev analyzes and fixes it in the channel (and can also write the conclusion back to the external system with the `gh`/`gitcode` CLI) → dev updates the task status with `task.brief`/`task.update`; as long as the sync script does not include `status`, this status is kept across later syncs until the issue is closed and synced as `已完成`. With this approach, a reopened issue does not automatically go back to `待处理`; when needed, a human or an agent changes the status, or the script includes a status for reopened issues.

PR reviews and alert on-call work the same way: any data that can be expressed as "external list → cards" can enter this pipeline, and the handling instructions and the choice of roles determine how it is processed.

## Related documents

- Action list, arguments and permission model: [agent-tool-api.md](agent-tool-api.md)
- Agent workspace and task snapshot boundaries: [agent-harness-workspace.md](agent-harness-workspace.md)
