/* ---------------- 自动处理规则:项目级列表页与规则编辑器 ----------------
   规则存在项目上(task_auto_rules),对本项目所有数据源的新建任务生效,按列表顺序
   取第一条命中的启用规则。列表页只读展示并提供新建/编辑入口,不做筛选或分组;
   看板列头与面板组件的 ⚡ 打开同一个编辑器。条件在可视化与表达式两种模式间切换
   (转换见 rule-condition.js),预览由服务端按新建时同一口径计算命中与处理归属。 */
let ruleEditor = null;          // 打开中的编辑器状态;关闭即置空,迟到的预览响应据此丢弃
let taskRulesSignature = null;  // 列表页上次渲染的数据指纹,轮询无变化时不重绘
// 「其他属性…」选项的值:属性名不允许含冒号,不会与真实属性冲突
const RULE_CUSTOM_FIELD = ":custom";

function openTaskRulesPage() {
  if (currentTab === "task-rules") renderTaskRulesPage(true);
  else switchTab("task-rules");
}

function taskRuleRoles(rule) {
  return [...new Set([...(rule.mentions || []).map(item => item.role_id),
                      ...(rule.role_ids || [])])];
}

function taskRuleHandlerHtml(rule, peer) {
  const roles = taskRuleRoles(rule).map(id => `@${id}`);
  if (!roles.length) return peer
    ? '<span class="rule-warn">未指定角色：无主控项目必须 @ 角色，否则派发会失败</span>'
    : "交给项目主控";
  const suffix = roles.length < 2 ? "" : peer ? "（各自启动）" : "（由主控协调）";
  return `交给 ${esc(roles.join("、"))}${suffix}`;
}

function taskRuleItemHtml(rule, index, peer) {
  const prompt = String(rule.prompt || "").replace(/\s+/g, " ").trim();
  return `<li><button type="button" class="rule-item${rule.enabled ? "" : " rule-disabled"}"
      data-query="${esc(rule.query)}" onclick="openTaskRuleEditor({ query: this.dataset.query })">
    <span class="rule-order" aria-label="第 ${index + 1} 条">${index + 1}</span>
    <span class="rule-item-body">
      <span class="rule-item-title">${esc(describeRuleCondition(rule.query) || "自定义表达式条件")}${
        rule.enabled ? "" : ' <span class="pill">已停用</span>'}</span>
      <code class="rule-item-query">${esc(rule.query)}</code>
      <span class="rule-item-meta">${taskRuleHandlerHtml(rule, peer)}${
        prompt ? ` · ${esc(prompt.length > 140 ? prompt.slice(0, 140) + "…" : prompt)}` : ""}</span>
    </span>
    <span class="rule-item-edit">编辑</span>
  </button></li>`;
}

function renderTaskRulesPage(force = false) {
  const host = document.getElementById("task-rules-list");
  if (!host || currentTab !== "task-rules") return;
  const rules = projTaskRules(), peer = peerModeProject();
  const signature = JSON.stringify([currentProject, peer, rules]);
  if (!force && signature === taskRulesSignature) return;
  taskRulesSignature = signature;
  document.getElementById("task-rules-count").textContent = rules.length ? `${rules.length} 条` : "";
  host.innerHTML = rules.length
    ? `<ol class="rule-list">${rules.map((rule, index) =>
        taskRuleItemHtml(rule, index, peer)).join("")}</ol>`
    : `<div class="tb-empty-state"><strong>还没有自动处理规则</strong>
        <p>新建规则，或在任务看板列头点 ⚡ 以该列的条件创建。</p>
        <button class="action" onclick="openTaskRuleEditor()">＋ 新建规则</button></div>`;
}

/* ---- 编辑器 ---- */
function ruleBlankRow() {
  return { field: "", op: "is", value: "", custom: false, touched: false };
}

// 看板列与面板组件的 ⚡:已有同条件规则则编辑,否则以该列条件新建
function openColumnRule(query) {
  const rule = ruleForQuery(query);
  openTaskRuleEditor(rule ? { query: rule.query, fromColumn: true }
                          : { prefill: query, fromColumn: true });
}

function openTaskRuleEditor({ query = null, prefill = "", fromColumn = false } = {}) {
  if (!currentProject) return;
  const rule = query ? projTaskRules().find(item => sameRuleQuery(item.query, query)) || null : null;
  const initial = rule ? rule.query : String(prefill || "");
  let model = null;
  try { model = ruleConditionFromQuery(initial); } catch (_) { model = null; }
  ruleEditor = {
    project: currentProject, original: rule ? rule.query : null,
    mode: model ? "visual" : "expression", expression: initial,
    model: model?.rows.length ? model : { join: model?.join || "all", rows: [ruleBlankRow()] },
    labels: [], preview: null, previewError: "", loading: false,
    token: 0, timer: null, saving: false, showErrors: false,
  };
  const peer = peerModeProject();
  openFormDialog(rule ? "编辑自动处理规则" : "新建自动处理规则", `
    <div class="rule-editor">
      <div class="rule-editor-main">
        <div class="tb-error" id="rule-editor-error" role="alert" hidden></div>
        ${fromColumn ? `<p class="tb-help rule-editor-hint">从看板列打开，条件已预填为该列的表达式。
          修改条件后，这一列的 ⚡ 不再对应此规则，可在侧栏「面板 → 自动处理规则」中找到它。</p>` : ""}
        <section class="rule-block">
          <div class="rule-block-head"><strong>触发条件</strong>
            <div class="rule-mode" role="group" aria-label="条件编辑方式">
              <button type="button" data-mode="visual" onclick="setRuleEditorMode('visual')">可视化</button>
              <button type="button" data-mode="expression" onclick="setRuleEditorMode('expression')">表达式</button>
            </div></div>
          <div id="rule-condition-editor"></div>
          <div class="rule-expression-line"><span>表达式</span><code id="rule-expression-text"></code></div>
          <div class="tb-query-error" id="rule-condition-error" role="alert"></div>
        </section>
        <section class="rule-block">
          <div class="rule-block-head"><strong>处理要求</strong></div>
          <p class="tb-help">${peer ? "本项目没有主控：输入 @ 选择处理角色（必填），多个角色各自启动。"
            : "输入 @ 选择角色：单个角色直接执行（不经主控），多个角色由主控协调；不 @ 任何角色则交给项目主控。"}</p>
          <div class="composer-wrap task-dispatch-wrap">
            <div id="task-rule-input" class="task-dispatch-input" contenteditable="true"
              role="textbox" aria-multiline="true" aria-label="处理要求"
              data-placeholder="处理要求，可 @指定角色…（Enter 保存，Shift+Enter 换行）"></div>
            <div id="task-rule-picker" class="task-dispatch-picker" role="listbox" hidden></div>
          </div>
          <label class="rule-enabled"><input type="checkbox" id="tr-enabled"
            ${rule && !rule.enabled ? "" : "checked"} onchange="renderRulePreview()"> 启用本规则</label>
        </section>
      </div>
      <aside class="rule-preview">
        <div class="rule-block-head"><strong>预览</strong>
          <span class="muted" id="rule-preview-count" role="status" aria-live="polite"></span></div>
        <p class="tb-help">规则只在任务新建时触发。这里用本项目所有数据源当前未归档的任务，
          演示哪些会命中、实际由哪条规则处理（按列表顺序取第一条命中的启用规则）。</p>
        <div id="rule-preview-list" class="rule-preview-list"></div>
      </aside>
    </div>`,
    `<button class="action" id="rule-save-btn" onclick="saveTaskRule()">保存规则</button>
     ${rule ? '<button class="danger" onclick="deleteTaskRule()">删除规则</button>' : ""}
     <button class="ghost" onclick="closeTaskRuleEditor()">取消</button>`,
    { size: "wide" });
  const state = ruleEditor;
  fdlg.addEventListener("close", () => {
    clearTimeout(state.timer);
    if (ruleEditor === state) ruleEditor = null;
    activateComposer("input", "mention-picker");
  }, { once: true });
  bindComposerEvents("task-rule-input", "task-rule-picker", () => saveTaskRule());
  if (rule) {
    // 旧规则只有 role_ids:合成 "@角色 " 前缀提及,保存后自然升级成结构化提及
    let content = rule.prompt || "", mentions = rule.mentions || [];
    if (!mentions.length && rule.role_ids?.length) {
      let prefix = "";
      mentions = rule.role_ids.map(id => {
        const start = Array.from(prefix).length;
        prefix += `@${id} `;
        return { role_id: id, start, end: start + id.length + 1 };
      });
      content = prefix + content;
    }
    restoreComposerPayload(content, mentions, document.getElementById("task-rule-input"));
  }
  renderRuleConditionEditor();
  if (!model && initial.trim() && !currentRuleCondition().error)
    setRuleConditionError("这个条件含括号嵌套或与、或混用，只能在表达式模式编辑。");
}

function closeTaskRuleEditor() {
  activateComposer("input", "mention-picker");
  fdlg.close();
}

// 当前条件:query 只在整体合法时非空;partial 是可视化模式下已填好的行拼出的表达式
function currentRuleCondition(state = ruleEditor) {
  if (!state) return { query: "", partial: "", errors: [], error: "" };
  if (state.mode === "visual") {
    const { query, errors } = ruleConditionToQuery(state.model);
    const error = !state.model.rows.length ? "至少添加一个条件"
      : errors.some(Boolean) ? "有条件未填写完整" : "";
    return { query: error ? "" : canonicalRuleQuery(query), partial: query, errors, error };
  }
  const text = state.expression.trim();
  if (!text) return { query: "", partial: "", errors: [], error: "请填写条件" };
  try {
    const query = formatRuleCondition(parseRuleCondition(text));
    return { query, partial: query, errors: [], error: "" };
  } catch (error) {
    return { query: "", partial: "", errors: [], error: `表达式不合法：${error.message}` };
  }
}

function ruleChoices(state = ruleEditor) {
  // 状态候选先放各数据源声明的取值(保留工作流顺序),再并入预览返回的标签全集
  const declared = projBoardSources().flatMap(source =>
    (source.status_values || []).map(item => `status: ${item.value}`));
  return ruleConditionChoices([...declared, ...(state?.labels || [])]);
}

function ruleFieldOptionsHtml(row, choices) {
  const known = choices.fields.map(item => item.field.toLowerCase());
  const fields = [...choices.fields.map(item => item.field)];
  if (!row.custom && row.field && !known.includes(row.field.toLowerCase())) fields.push(row.field);
  const current = row.custom ? RULE_CUSTOM_FIELD : row.field.toLowerCase();
  const option = (value, title) => `<option value="${esc(value)}"${
    value.toLowerCase() === current ? " selected" : ""}>${esc(title)}</option>`;
  return option("", "标签") + fields.map(field => option(field, ruleFieldTitle(field))).join("")
    + option(RULE_CUSTOM_FIELD, "其他属性…");
}

function ruleValueOptionsHtml(row, choices) {
  const field = row.field.trim().toLowerCase();
  const values = !field ? choices.plain
    : choices.fields.find(item => item.field.toLowerCase() === field)?.values || [];
  return values.map(value => `<option value="${esc(value)}"></option>`).join("");
}

function ruleRowHtml(row, index, choices, showError) {
  const property = row.custom || Boolean(row.field);
  const ops = property
    ? [["is", "是"], ["not", "不是"], ["set", "已设置"], ["unset", "未设置"]]
    : [["is", "包含"], ["not", "不包含"]];
  const needsValue = row.op !== "set" && row.op !== "unset";
  const error = showError ? ruleRowError(row) : "";
  return `<div class="rule-row" data-index="${index}">
    <select class="rule-field" aria-label="条件字段" onchange="ruleFieldChange(this)">${
      ruleFieldOptionsHtml(row, choices)}</select>
    ${row.custom ? `<input class="rule-custom-field" aria-label="属性名" placeholder="属性名，如 owner"
      maxlength="256" value="${esc(row.field)}" oninput="ruleCustomFieldInput(this)">` : ""}
    <select class="rule-op" aria-label="条件判断" onchange="ruleOpChange(this)">${
      ops.map(([value, title]) => `<option value="${value}"${row.op === value ? " selected" : ""}>${title}</option>`).join("")}</select>
    ${needsValue ? `<input class="rule-value" aria-label="取值" list="rule-values-${index}"
      placeholder="${property ? "取值" : "标签，如 bug"}" maxlength="2000"
      value="${esc(row.value)}" oninput="ruleValueInput(this)"
      ><datalist id="rule-values-${index}">${ruleValueOptionsHtml(row, choices)}</datalist>` : ""}
    <button type="button" class="tb-chip-x rule-row-remove" aria-label="删除这条条件"
      onclick="removeRuleConditionRow(this)">×</button>
    <div class="rule-row-error"${error ? "" : " hidden"}>${esc(error)}</div>
  </div>`;
}

function renderRuleConditionEditor() {
  const state = ruleEditor, host = document.getElementById("rule-condition-editor");
  if (!state || !host) return;
  document.querySelectorAll(".rule-mode button").forEach(button => {
    const active = button.dataset.mode === state.mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  if (state.mode === "visual") {
    const choices = ruleChoices(state);
    host.innerHTML = `<label class="rule-join">满足以下
        <select aria-label="条件组合方式" onchange="ruleJoinChange(this.value)">
          <option value="all"${state.model.join === "all" ? " selected" : ""}>全部</option>
          <option value="any"${state.model.join === "any" ? " selected" : ""}>任一</option>
        </select>条件</label>
      <div class="rule-rows">${state.model.rows.map((row, index) =>
        ruleRowHtml(row, index, choices, state.showErrors || row.touched)).join("")}</div>
      <button type="button" class="ghost compact rule-add-row" onclick="addRuleConditionRow()">＋ 添加条件</button>`;
  } else {
    host.innerHTML = `<textarea id="rule-expression-input" class="tb-advanced-query rule-expression-input"
        rows="3" spellcheck="false" maxlength="2000" aria-label="条件表达式"
        placeholder="例如 bug &amp; !status: 已完成" oninput="ruleExpressionInput(this)">${esc(state.expression)}</textarea>
      <p class="tb-help">整串标签匹配（<code>bug</code>、<code>owner: 张三</code>），<code>属性: *</code>
        表示已设置该属性；用 &amp;（且）、|（或）、!（非）与括号组合。</p>`;
  }
  updateRuleExpression();
}

function updateRuleExpression() {
  const state = ruleEditor;
  if (!state) return;
  const condition = currentRuleCondition(state);
  const text = document.getElementById("rule-expression-text");
  if (text) text.textContent = condition.query || condition.partial || "—";
  // 可视化模式的问题显示在各行;条件行只给表达式模式报语法错误
  if (state.mode === "expression")
    setRuleConditionError(state.expression.trim() ? condition.error : "");
  scheduleRulePreview();
}

function setRuleConditionError(message) {
  const node = document.getElementById("rule-condition-error");
  if (node) node.textContent = message || "";
}

function ruleRowOf(element) {
  const index = Number(element.closest(".rule-row")?.dataset.index);
  return { index, row: ruleEditor?.model.rows[index] };
}

function ruleFieldChange(select) {
  const { index, row } = ruleRowOf(select);
  if (!row) return;
  row.custom = select.value === RULE_CUSTOM_FIELD;
  row.field = row.custom ? "" : select.value;
  if (!row.custom && !row.field && (row.op === "set" || row.op === "unset")) row.op = "is";
  row.value = "";
  renderRuleConditionEditor();
  document.querySelector(`.rule-row[data-index="${index}"] `
    + (row.custom ? ".rule-custom-field" : ".rule-value"))?.focus();
}

function ruleOpChange(select) {
  const { row } = ruleRowOf(select);
  if (!row) return;
  row.op = select.value;
  renderRuleConditionEditor();
}

// 输入框按键只改模型与表达式,不重绘行,保住焦点与输入法状态
function ruleValueInput(input) {
  const { row } = ruleRowOf(input);
  if (!row) return;
  row.value = input.value;
  row.touched = true;
  refreshRuleRowError(input, row);
  updateRuleExpression();
}

function ruleCustomFieldInput(input) {
  const { row } = ruleRowOf(input);
  if (!row) return;
  row.field = input.value;
  row.touched = true;
  refreshRuleRowError(input, row);
  updateRuleExpression();
}

function refreshRuleRowError(element, row) {
  const node = element.closest(".rule-row")?.querySelector(".rule-row-error");
  if (!node) return;
  node.textContent = ruleRowError(row);
  node.hidden = !node.textContent;
}

function ruleJoinChange(value) {
  if (!ruleEditor) return;
  ruleEditor.model.join = value === "any" ? "any" : "all";
  updateRuleExpression();
}

function addRuleConditionRow() {
  if (!ruleEditor) return;
  ruleEditor.model.rows.push(ruleBlankRow());
  renderRuleConditionEditor();
  [...document.querySelectorAll(".rule-row .rule-value")].pop()?.focus();
}

function removeRuleConditionRow(button) {
  const { index } = ruleRowOf(button);
  if (!ruleEditor || Number.isNaN(index)) return;
  ruleEditor.model.rows.splice(index, 1);
  renderRuleConditionEditor();
}

function ruleExpressionInput(textarea) {
  if (!ruleEditor) return;
  ruleEditor.expression = textarea.value;
  updateRuleExpression();
}

function setRuleEditorMode(mode) {
  const state = ruleEditor;
  if (!state || state.mode === mode) return;
  if (mode === "expression") {
    const { query, partial } = currentRuleCondition(state);
    state.expression = query || partial || "";
    state.mode = "expression";
    setRuleConditionError("");
  } else {
    let model;
    try { model = ruleConditionFromQuery(state.expression); }
    catch (error) {
      setRuleConditionError(`表达式不合法：${error.message}；改正后才能切换到可视化。`);
      return;
    }
    if (!model) {
      setRuleConditionError("这个条件含括号嵌套或与、或混用，无法转成可视化条件，请继续在表达式模式编辑。");
      return;
    }
    state.model = model.rows.length ? model : { join: model.join, rows: [ruleBlankRow()] };
    state.mode = "visual";
    setRuleConditionError("");
  }
  renderRuleConditionEditor();
}

/* ---- 预览:条件变化后防抖请求;token 丢弃迟到响应,标签全集顺带刷新候选 ---- */
function scheduleRulePreview() {
  const state = ruleEditor;
  if (!state) return;
  clearTimeout(state.timer);
  state.loading = true;
  renderRulePreview();
  state.timer = setTimeout(() => loadRulePreview(state), 250);
}

async function loadRulePreview(state) {
  const { query } = currentRuleCondition(state);
  const token = ++state.token;
  const params = new URLSearchParams({ query, rule: state.original || "" });
  let data = null, error = "";
  try {
    const response = await fetch(
      `/api/projects/${encodeURIComponent(state.project)}/task_rules/preview?${params}`);
    data = await response.json().catch(() => ({}));
    if (!response.ok)
      error = typeof data.detail === "string" ? data.detail : `请求失败 (${response.status})`;
  } catch (exc) { error = exc.message; }
  if (ruleEditor !== state || token !== state.token) return;
  state.loading = false;
  state.previewError = error;
  if (!error) {
    state.preview = data;
    const labelsChanged = JSON.stringify(data.labels || []) !== JSON.stringify(state.labels);
    state.labels = data.labels || [];
    if (labelsChanged) refreshRuleSuggestions(state);
  }
  renderRulePreview();
}

// 标签全集到达后更新候选:只改未聚焦的下拉框与 datalist,不打断正在输入的行
function refreshRuleSuggestions(state) {
  if (state.mode !== "visual") return;
  const choices = ruleChoices(state);
  document.querySelectorAll("#rule-condition-editor .rule-row").forEach(element => {
    const row = state.model.rows[Number(element.dataset.index)];
    if (!row) return;
    const select = element.querySelector(".rule-field");
    if (select && select !== document.activeElement)
      select.innerHTML = ruleFieldOptionsHtml(row, choices);
    const list = element.querySelector("datalist");
    if (list) list.innerHTML = ruleValueOptionsHtml(row, choices);
  });
}

function rulePreviewItemHtml(task, enabled, project) {
  const outcome = task.outcome === "done"
    ? '<span class="rule-outcome">已完成 · 不派发</span>'
    : task.outcome === "shadowed"
      ? `<span class="rule-outcome rule-outcome-shadowed" title="${esc(task.shadowed_by.query)}">由规则 #${
          task.shadowed_by.index} 先处理</span>`
      : `<span class="rule-outcome rule-outcome-handled">${enabled ? "本规则处理" : "启用后由本规则处理"}</span>`;
  const labels = (task.labels || []).filter(label =>
    splitLabel(label)[0].toLowerCase() !== "status");
  return `<div class="rule-preview-item">
    <div class="rule-preview-title"><a href="${esc(missionCrewResourceUrl(project, "tasks", task.id))}"
      target="_blank" rel="noopener">${esc(task.title)}</a>${
      task.status ? ` ${statusPillHtml(task.status, task.source)}` : ""}</div>
    <div class="rule-preview-meta">${outcome}<span>${esc(task.source_name)}${
      task.external_id ? ` · ${esc(task.external_id)}` : ""}</span></div>
    ${labels.length ? `<div class="rule-preview-labels">${labels.map(label =>
      `<span class="badge">${esc(label)}</span>`).join("")}</div>` : ""}
  </div>`;
}

function renderRulePreview() {
  const state = ruleEditor;
  const list = document.getElementById("rule-preview-list");
  const count = document.getElementById("rule-preview-count");
  if (!state || !list || !count) return;
  const { query } = currentRuleCondition(state);
  if (!query) {
    count.textContent = "";
    list.innerHTML = '<div class="rule-preview-empty">补全条件后显示命中的任务</div>';
    return;
  }
  if (state.previewError && !state.loading) {
    count.textContent = "";
    list.innerHTML = `<div class="tb-error">${esc(state.previewError)}</div>`;
    return;
  }
  const data = state.preview;
  // 加载中保留上一帧列表,只在计数处提示,避免每次按键闪烁
  if (state.loading || !data || data.query !== query) {
    count.textContent = "正在更新…";
    if (!data) list.innerHTML = "";
    return;
  }
  const enabled = document.getElementById("tr-enabled")?.checked !== false;
  count.textContent = `命中 ${data.matched} 个 · ${enabled
    ? `本规则处理 ${data.handled} 个` : "规则已停用，不处理任何任务"}`;
  list.innerHTML = data.tasks.length
    ? data.tasks.map(task => rulePreviewItemHtml(task, enabled, state.project)).join("")
      + (data.truncated ? `<div class="rule-preview-empty">只列出最近更新的 ${data.tasks.length} 个</div>` : "")
    : '<div class="rule-preview-empty">当前没有未归档任务命中这个条件</div>';
}

/* ---- 保存与删除:规则按原条件定位(条件在项目内唯一),保存时再核对一次 ---- */
function setRuleEditorError(message) {
  const node = document.getElementById("rule-editor-error");
  if (!node) return;
  node.textContent = message || "";
  node.hidden = !message;
  if (message) node.scrollIntoView({ block: "nearest" });
}

async function saveTaskRule() {
  const state = ruleEditor;
  if (!state || state.saving) return;
  state.showErrors = true;
  const condition = currentRuleCondition(state);
  if (!condition.query) {
    if (state.mode === "visual") renderRuleConditionEditor();
    setRuleEditorError(condition.error || "请填写条件");
    return;
  }
  if (state.project !== currentProject) {
    setRuleEditorError("已切换到其他项目，请关闭后重新打开"); return;
  }
  const { content, mentions } = composerPayload(document.getElementById("task-rule-input"));
  if (peerModeProject() && !mentions.length) {
    setRuleEditorError("本项目没有主控，处理要求里必须 @ 至少一个角色"); return;
  }
  const rules = projTaskRules().map(rule => ({ ...rule }));
  const index = state.original === null ? -1
    : rules.findIndex(rule => rule.query === state.original);
  if (state.original !== null && index < 0) {
    setRuleEditorError("这条规则已被删除或修改，请关闭后重新打开"); return;
  }
  const clash = rules.findIndex((rule, i) => i !== index && sameRuleQuery(rule.query, condition.query));
  if (clash >= 0) {
    setRuleEditorError(`规则 #${clash + 1} 已使用相同的条件，请直接编辑那条规则`); return;
  }
  const next = {
    query: condition.query, prompt: content, mentions,
    role_ids: [...new Set(mentions.map(item => item.role_id))],
    enabled: document.getElementById("tr-enabled").checked,
  };
  if (index >= 0) rules[index] = next; else rules.push(next);
  state.saving = true;
  const button = document.getElementById("rule-save-btn");
  if (button) button.disabled = true;
  try {
    await persistTaskBoardConfig({ task_auto_rules: rules });
  } catch (_) {   // api() 已提示错误;保留编辑器内容供重试
    state.saving = false;
    if (button) button.disabled = false;
    return;
  }
  if (ruleEditor === state) closeTaskRuleEditor();
  renderTaskRulesPage(true);
  toast(index >= 0 ? "自动处理规则已保存" : "自动处理规则已创建", "success");
}

async function deleteTaskRule() {
  const state = ruleEditor;
  if (!state || state.original === null || state.saving) return;
  const label = describeRuleCondition(state.original) || state.original;
  if (!await uiConfirm(`删除自动处理规则「${label}」？`)) return;
  if (ruleEditor !== state) return;
  state.saving = true;
  try {
    await persistTaskBoardConfig({
      task_auto_rules: projTaskRules().filter(rule => rule.query !== state.original)
        .map(rule => ({ ...rule })),
    });
  } catch (_) { state.saving = false; return; }
  if (ruleEditor === state) closeTaskRuleEditor();
  renderTaskRulesPage(true);
  toast("自动处理规则已删除", "success");
}
