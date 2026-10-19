/* ---------------- 自定义面板 ----------------
   两种形态:widgets(主控 Agent 通过 dashboard.save 创建维护的组件网格)与
   taskboard(任务看板:选数据源,独立筛选与属性分列)。日常只读渲染;
   编辑模式从侧栏面板列表的 ✎ 进入——widgets 面板可继续与主控在面板专属频道
   对话,或直接改布局 JSON;taskboard 面板改名称与数据源,日常筛选与显示选项
   在看板工具条操作,共享表达式列在单独的管理对话框维护。
   列定义与卡片由服务端数据源注册表统一解析(/boards/{id}/data),前端只做
   通用渲染,不感知数据来自任务还是脚本同步的外部列表(如 GitCode Issue)。
   内置任务看板(#board)用同一渲染器,见下方「任务看板渲染器」。 */
let currentCustomBoard = null;
let customBoardEditing = false;
let boardEditorVisible = false;   // JSON 编辑表单是否展开(仅编辑模式内)
let boardEditMode = false;        // 编辑模式:侧栏 ✎ 进入,"完成编辑"退出
let customBoardRenderSignature = null;
let boardWidgetRenderToken = 0;

/* ---- 看板数据源清单:随项目总览下发(ui.js 的 projBoardSources);未到时回退内置任务源 ---- */
function boardSourceName(id) {
  const hit = projBoardSources().find(s => s.id === id);
  return hit ? hit.name : (id || "built-in");
}

function boardSourceOptionsHtml(selected) {
  return projBoardSources()
    .map(s => `<option value="${esc(s.id)}"${s.id === selected ? " selected" : ""}>
      ${esc(s.name)}${s.description ? ` — ${esc(s.description)}` : ""}</option>`).join("");
}

function currentCustomBoardStateSignature() {
  const board = projBoards().find(value => value.id === currentCustomBoard) || null;
  return JSON.stringify([currentProject, currentCustomBoard, boardEditorVisible,
                         boardEditMode, board]);
}

// taskboard 组件的 content.source 是数据源 id;其余组件的 content.source 是取数绑定
function widgetIsLive(widget) {
  const source = widget.content?.source;
  return widget.type === "taskboard"
    || (!!source && typeof source === "object" && !Array.isArray(source));
}

function boardHasLiveWidgets(board) {
  // 任务看板与实时组件都从服务端取数,需随轮询重绘
  if (board?.kind === "taskboard") return true;
  return (board?.layout || []).some(widgetIsLive);
}

function renderCustomBoards(force = false) {
  if (customBoardEditing && !force) return;
  const boards = projBoards();
  if (currentCustomBoard && !boards.some(b => b.id === currentCustomBoard)) {
    currentCustomBoard = null;
    boardEditMode = false;
  }
  if (!currentCustomBoard && boards.length) currentCustomBoard = boards[0].id;
  const board = boards.find(value => value.id === currentCustomBoard);
  const signature = currentCustomBoardStateSignature();
  if (!force && signature === customBoardRenderSignature) {
    if (boardHasLiveWidgets(board)) renderBoardContent(board);
    return;
  }
  renderCustomBoardEditor();
}

function selectCustomBoard(id) {
  customBoardEditing = false;
  boardEditorVisible = false;
  boardEditMode = false;
  currentCustomBoard = id || null;
  renderCustomBoardEditor();
  syncUrl();
}

function toggleBoardEditor() {
  boardEditorVisible = !boardEditorVisible;
  if (!boardEditorVisible) customBoardEditing = false;
  renderCustomBoardEditor();
}

function exitBoardEditMode() {
  boardEditMode = false;
  boardEditorVisible = false;
  customBoardEditing = false;
  renderCustomBoardEditor();
}

function renderCustomBoardEditor() {
  const form = document.getElementById("custom-board-form");
  const preview = document.getElementById("custom-board-preview");
  if (!form || !preview) return;
  const board = projBoards().find(b => b.id === currentCustomBoard);
  customBoardRenderSignature = currentCustomBoardStateSignature();
  const title = document.getElementById("custom-board-title");
  const desc = document.getElementById("custom-board-desc");
  const jsonBtn = document.getElementById("custom-board-json-btn");
  const doneBtn = document.getElementById("custom-board-done-btn");
  if (!board) {
    if (title) title.textContent = "自定义面板";
    if (desc) desc.textContent = "";
    if (jsonBtn) jsonBtn.hidden = true;
    if (doneBtn) doneBtn.hidden = true;
    form.style.display = "none";
    preview.classList.remove("taskboard-host");
    preview.innerHTML = `<div class="empty" style="grid-column:1/-1">还没有面板:点击侧栏「面板」的 ＋ 新建。</div>`;
    if (typeof updateConfigChatContext === "function") updateConfigChatContext();
    return;
  }
  const editable = boardEditMode && board.kind !== "taskboard";
  if (title) title.textContent = (board.name || board.id)
    + (boardEditMode ? " · 编辑中" : "");
  if (desc) desc.textContent = board.kind === "taskboard"
    ? `数据源:${boardSourceName(board.source)}`
    : (board.description || "");
  if (jsonBtn) jsonBtn.hidden = !editable;
  if (doneBtn) doneBtn.hidden = !boardEditMode;
  if (!editable || !boardEditorVisible) {
    form.style.display = "none";
  } else {
    form.style.display = "block";
    form.innerHTML = `
      <div class="row">
        <div><label>面板 id</label><input id="cb-id" value="${esc(board.id.split(":").pop())}" disabled></div>
        <div><label>名称</label><input id="cb-name" oninput="customBoardEditing=true" value="${esc(board.name)}"></div>
      </div>
      <label>用途说明</label><input id="cb-desc" oninput="customBoardEditing=true" value="${esc(board.description || "")}">
      <label>布局 JSON(应急手动编辑;日常修改建议在下方主控对话里提需求)</label>
      <textarea id="cb-layout" rows="14" spellcheck="false" oninput="previewBoardDraft()">${esc(JSON.stringify(board.layout || [], null, 2))}</textarea>
      <div class="form-actions"><button class="action" onclick="saveCustomBoard()">保存面板</button>
        <button class="danger" onclick="deleteCustomBoard()">删除面板</button></div>`;
  }
  renderBoardContent(board);
  if (typeof updateConfigChatContext === "function") updateConfigChatContext();
}

function renderBoardContent(board) {
  if (!board) return;
  if (board.kind === "taskboard") renderTaskboard(customTaskboardBinding(board));
  else renderBoardWidgets(board.layout || []);
}

/* ---- 标签表达式:与 & 或 | 非 ! 与括号;标签不区分大小写,&&/|| 同义。
   项与整串标签匹配(高级标签经归一化),`属性: *` 匹配带该属性的对象;
   与服务端 core/label_query.py 同一套语义。 ---- */
function compileLabelQuery(expr) {
  const tokens = [];
  let buffer = "";
  const flush = () => {
    const text = buffer.trim();
    if (text) tokens.push({ raw: text });
    buffer = "";
  };
  for (let i = 0; i < expr.length; i++) {
    const ch = expr[i];
    if ("&|!()".includes(ch)) {
      flush();
      if ((ch === "&" || ch === "|") && expr[i + 1] === ch) i++;
      tokens.push(ch);
    } else buffer += ch;
  }
  flush();
  if (!tokens.length) throw new Error("表达式不能为空");
  let pos = 0;
  const peek = () => tokens[pos];
  const parseOr = () => {
    let node = parseAnd();
    while (peek() === "|") {
      pos++;
      const left = node, right = parseAnd();
      node = ctx => left(ctx) || right(ctx);
    }
    return node;
  };
  const parseAnd = () => {
    let node = parseNot();
    while (peek() === "&") {
      pos++;
      const left = node, right = parseNot();
      node = ctx => left(ctx) && right(ctx);
    }
    return node;
  };
  const parseNot = () => {
    const token = peek();
    if (token === "!") { pos++; const inner = parseNot(); return ctx => !inner(ctx); }
    if (token === "(") {
      pos++;
      const inner = parseOr();
      if (peek() !== ")") throw new Error("缺少右括号");
      pos++;
      return inner;
    }
    if (!token || typeof token === "string") throw new Error("运算符后缺少标签");
    pos++;
    const [prop, value] = splitLabel(token.raw);
    if (prop && value === "*") {
      const wanted = prop.toLowerCase();
      return ctx => ctx.props.has(wanted);
    }
    const wanted = normalizeLabel(token.raw).toLowerCase();
    return ctx => ctx.labels.has(wanted);
  };
  const matcher = parseOr();
  if (pos !== tokens.length) throw new Error("表达式有多余内容");
  return matcher;
}

function matchLabelQuery(query, labels) {
  const matcher = compileLabelQuery(query);
  return matcher({
    labels: new Set((labels || []).map(l => normalizeLabel(l).toLowerCase())),
    props: new Set((labels || []).map(l => splitLabel(l)[0].toLowerCase())
      .filter(Boolean)),
  });
}

/* 看板通用卡片:所有卡片都是任务,点击打开任务详情;带 url 的另给外链 ↗。
   showStatus 时在标题后加状态药丸(列不是单个状态时才有信息量),颜色取 source 数据源的声明。 */
function boardCardHtml(card, { showStatus = false, source = "built-in" } = {}) {
  const click = card.task_id
    ? `data-task-id="${esc(card.task_id)}" onclick="openTask(this.dataset.taskId)"`
    : (card.url ? `data-url="${esc(card.url)}" onclick="window.open(this.dataset.url,'_blank','noopener')"` : "");
  const meta = [`更新 ${new Date(card.updated_at * 1000).toLocaleString()}`,
                ...(card.meta || [])];
  const labels = (card.labels || []).filter(l =>
    splitLabel(l)[0].toLowerCase() !== "status");
  return `<div class="card${card.archived ? " task-archived" : ""}" ${click}>
    <div class="title">${esc(card.title)}${showStatus && card.status
      ? ` ${statusPillHtml(card.status, source)}` : ""}${card.archived
      ? '<span class="badge task-archived-badge">已归档</span>' : ""}${card.url
      ? ` <a class="badge" href="${esc(card.url)}" target="_blank" rel="noopener"
           onclick="event.stopPropagation()" title="打开外部链接">↗</a>` : ""}</div>
    ${card.summary ? `<div class="task-card-summary">${esc(card.summary)}</div>` : ""}
    <div class="meta">${meta.map(esc).join(" · ")}</div>
    <div class="meta">${labels.map(l => `<span class="badge">${esc(l)}</span>`).join("")}</div>
  </div>`;
}

/* ---------------- 任务看板:个人筛选/显示与共享表达式列分开 ----------------
   服务端负责匹配和分列;taskboard-view.js 负责纯视图状态,此处只负责 DOM 与请求。
   绑定捕获项目/数据源,避免切换项目后迟到响应覆盖新看板。 */
const taskboardBindings = {};
const taskboardStates = {};

function builtinTaskboardBinding() {
  const projectId = currentProject, project = projObj();
  return {
    key: JSON.stringify([projectId, "builtin", "built-in"]),
    projectId, boardId: "builtin", source: "built-in",
    defaultGroupBy: project?.task_board_group_by
      || (project?.task_board_filters?.length ? "" : "status"),
    host: () => document.getElementById("board"),
    visible: () => currentProject === projectId && currentTab === "board",
    dataUrl: () => `/api/projects/${encodeURIComponent(projectId)}/builtin_board/data`,
    save: patch => persistTaskBoardConfig({
      task_board_filters: taskboardFilterPayload(patch.filters.filter(f => !f.locked)),
    }),
  };
}

function customTaskboardBinding(board) {
  const projectId = currentProject;
  const shortId = board.id.replace(`${projectId}:`, "");
  const source = board.source || "built-in";
  const statusQueries = sourceStatusValues(source).map(s => `status: ${s.value}`.toLowerCase());
  const configured = (board.filters || []).map(f => f.query.toLowerCase());
  const standardColumns = !configured.length || (configured.length === statusQueries.length
    && configured.every(q => statusQueries.includes(q)));
  return {
    key: JSON.stringify([projectId, board.id, source]),
    projectId, boardId: board.id, source,
    defaultGroupBy: board.group_by || (standardColumns ? "status" : ""),
    host: () => document.getElementById("custom-board-preview"),
    visible: () => currentProject === projectId && currentTab === "custom"
      && currentCustomBoard === board.id && projBoards().some(item =>
        item.id === board.id && item.kind === "taskboard"
        && (item.source || "built-in") === source),
    dataUrl: () => `/api/projects/${encodeURIComponent(projectId)}/boards/${encodeURIComponent(shortId)}/data`,
    save: patch => saveTaskboardConfig(board, { filters: taskboardFilterPayload(patch.filters) }),
    afterData: data => {
      const desc = document.getElementById("custom-board-desc");
      const updated = data.source.updated_at
        ? ` · 最后更新 ${new Date(data.source.updated_at * 1000).toLocaleString()}` : "";
      if (desc) desc.textContent = `数据源:${data.source.name}${updated}`;
    },
  };
}

function taskboardState(binding) {
  return taskboardStates[binding.key] ||= {
    data: null, token: 0, loading: false, error: "", timer: null,
    view: loadTaskboardView(binding.projectId, binding.boardId, binding.source,
      defaultTaskboardView(binding.defaultGroupBy, taskFilter)),
  };
}

function rememberTaskboardView(binding) {
  const state = taskboardState(binding);
  state.view = normalizeTaskboardView(state.view,
    defaultTaskboardView(binding.defaultGroupBy, taskFilter));
  saveTaskboardView(binding.projectId, binding.boardId, binding.source, state.view);
}

// 服务端只接受 {title,query,color};locked 等渲染标记不回传
function taskboardFilterPayload(filters) {
  return filters.map(f => ({ title: f.title, query: f.query, color: f.color || "" }));
}

function taskboardBindingOf(element) {
  const host = element.closest("[data-board-key]");
  return host ? taskboardBindings[host.dataset.boardKey] : null;
}

function currentTaskboardFilters(binding) {
  return (taskboardState(binding).data?.filters || [])
    .map(f => ({ title: f.title, query: f.query, color: f.color || "",
                 locked: Boolean(f.locked) }));
}

async function saveTaskboardConfig(board, patch) {
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
    id: board.id.replace(`${currentProject}:`, ""), name: board.name, ...patch,
  });
  await loadOverview();
  renderCustomBoards(true);
}

function taskboardActiveFilterCount(view) {
  return view.facets.length + (view.advanced ? 1 : 0) + (view.search.trim() ? 1 : 0);
}

function taskboardFacetHtml(data, view, source) {
  // 状态优先沿用工作流声明顺序,其余候选与后端给出的标签全集一致。
  const labels = [...sourceStatusValues(source).map(s => `status: ${s.value}`),
    ...(data.labels || [])];
  return taskboardFacetGroups(labels, view).map(group => `
    <fieldset class="tb-facet" data-facet-field="${esc(group.field)}">
      <legend>${esc(group.title)}</legend>
      ${group.options.map(option => `<label class="tb-option"
          data-option-search="${esc((group.title + " " + option.label).toLowerCase())}">
        <input type="checkbox" ${option.selected ? "checked" : ""}
          data-field="${esc(group.field)}" data-value="${esc(option.value)}"
          data-missing="${Boolean(option.missing)}"
          data-tb-focus="${esc(JSON.stringify(["facet", group.field, option.value, option.missing]))}"
          onchange="toggleTaskboardFacet(this)">
        <span>${esc(option.label)}</span></label>`).join("")}
    </fieldset>`).join("");
}

function taskboardGroupOptions(data, view) {
  const props = new Map([["status", "status"]]);
  for (const label of data.labels || []) {
    const prop = splitLabel(label)[0];
    if (prop && taskboardViewProperty(prop) !== null && !props.has(prop.toLowerCase()))
      props.set(prop.toLowerCase(), prop);
  }
  if (view.groupBy) props.set(view.groupBy.toLowerCase(), view.groupBy);
  const choices = [...props.values()].map(prop => ({
    value: prop, title: prop.toLowerCase() === "status" ? "状态" : prop,
  }));
  choices.push({ value: "", title: "自定义表达式列" });
  return choices.map(item => `<option value="${esc(item.value)}"${
    item.value.toLowerCase() === view.groupBy.toLowerCase() ? " selected" : ""}>${esc(item.title)}</option>`).join("");
}

function taskboardFilterChipsHtml(view) {
  const chip = (text, kind, field = "") => `<span class="tb-chip">
    <span title="${esc(text)}">${esc(text)}</span><button class="tb-chip-x"
      aria-label="${esc("移除筛选 " + text)}" data-kind="${kind}" data-field="${esc(field)}"
      onclick="removeTaskboardViewFilter(this)">×</button></span>`;
  const chips = view.facets.map(f => {
    const title = f.field.toLowerCase() === "status" ? "状态" : (f.field || "标签");
    return chip(`${title}：${[...f.values, ...(f.missing ? ["未设置"] : [])].join("、")}`, "facet", f.field);
  });
  if (view.search.trim()) chips.unshift(chip(`搜索：${view.search.trim()}`, "search"));
  if (view.advanced) chips.push(chip(`表达式：${view.advanced}`, "advanced"));
  return chips.length ? chips.join("") + `<button class="tb-text-button"
    onclick="clearTaskboardFilters(this)">清除筛选</button>` : "";
}

function taskboardFilterBarHtml(binding, data) {
  const view = taskboardState(binding).view;
  const count = taskboardActiveFilterCount(view);
  const option = (value, label, current) =>
    `<option value="${value}"${current === value ? " selected" : ""}>${label}</option>`;
  return `<div class="tb-filter-bar">
    <label class="tb-search"><span aria-hidden="true">⌕</span>
      <input type="search" class="tb-search-input" aria-label="搜索任务"
        placeholder="搜索任务标题、编号、标签…" maxlength="512"
        value="${esc(view.search)}" data-tb-focus="search"
        oninput="searchTaskboard(this)"></label>
    <details class="tb-popover" data-tb-menu="filters">
      <summary class="ghost" data-tb-focus="filters-menu">筛选${
        count ? ` <span class="tb-counter">${count}</span>` : ""}</summary>
      <div class="tb-popover-panel tb-filter-panel">
        <div class="tb-panel-heading"><strong>筛选任务</strong>
          <button class="tb-text-button" onclick="clearTaskboardFilters(this)">清空</button></div>
        <p class="tb-help">同一条件任选其一，不同条件同时满足。</p>
        <input type="search" class="tb-facet-search" placeholder="查找筛选值…"
          aria-label="查找筛选值" data-tb-focus="facet-search"
          oninput="filterTaskboardFacetOptions(this)">
        <div class="tb-facet-list" data-scroll-key="tb:facets">${taskboardFacetHtml(data, view, binding.source)}</div>
        <details class="tb-advanced" data-tb-menu="advanced">
          <summary data-tb-focus="advanced-menu">高级标签表达式</summary>
          <p class="tb-help">支持 &amp;、|、! 和括号，与上方条件同时生效。</p>
          <div class="tb-advanced-row"><input class="tb-advanced-query"
            aria-label="高级标签表达式" placeholder="例如 bug &amp; !wontfix"
            maxlength="2000" value="${esc(view.advanced)}" data-tb-focus="advanced"
            onkeydown="if(event.key==='Enter'&&!imeComposing(event))applyTaskboardAdvanced(this)">
            <button class="ghost" data-tb-focus="apply-advanced" onclick="applyTaskboardAdvanced(this)">应用</button></div>
          <div class="tb-query-error" role="alert"></div>
        </details>
      </div>
    </details>
    <label class="tb-control">分列
      <select class="tb-group-by" aria-label="分列依据" data-tb-focus="group"
        onchange="setTaskboardOption(this, 'groupBy', this.value)">${taskboardGroupOptions(data, view)}</select>
    </label>
    <label class="tb-scope">显示
      <select aria-label="任务显示范围" data-tb-focus="scope"
        onchange="setTaskboardOption(this, 'scope', this.value)">
        ${option("active", "活跃 Task", view.scope)}${option("all", "全部 Task", view.scope)}${option("archived", "已归档 Task", view.scope)}
      </select>
    </label>
    <details class="tb-popover tb-display-menu" data-tb-menu="display">
      <summary class="ghost" data-tb-focus="display-menu">显示选项</summary>
      <div class="tb-popover-panel tb-display-panel">
        <strong>显示选项</strong>
        <label class="tb-display-row">列内排序<select aria-label="列内排序" data-tb-focus="sort"
          onchange="setTaskboardOption(this, 'sort', this.value)">
          ${option("updated_desc", "最近更新", view.sort)}${option("updated_asc", "最早更新", view.sort)}
          ${option("created_desc", "最近创建", view.sort)}${option("title_asc", "标题 A–Z", view.sort)}
        </select></label>
        <label class="tb-option"><input type="checkbox" ${view.hideEmpty ? "checked" : ""}
          data-tb-focus="hide-empty" onchange="setTaskboardOption(this, 'hideEmpty', this.checked)">隐藏空列</label>
        <div class="tb-display-actions">
          <button class="ghost" data-tb-focus="collapse-all" onclick="setTaskboardCollapsed(this, true)">折叠所有列</button>
          <button class="ghost" data-tb-focus="expand-all" onclick="setTaskboardCollapsed(this, false)">展开所有列</button>
        </div>
        <hr>
        <button class="tb-menu-button" onclick="openTaskboardColumns(this)">管理自定义表达式列…</button>
        <button class="tb-menu-button"
          onclick="this.closest('details').open=false;openTaskRuleEditor()">新建自动处理规则…</button>
        <button class="tb-menu-button" data-tb-focus="reset-view" onclick="resetTaskboardView(this)">重置当前视图</button>
        <p class="tb-help">视图偏好在此浏览器中按项目和看板分别记住。</p>
      </div>
    </details>
    <button class="action tb-new-task" data-source="${esc(binding.source)}"
      onclick="openNewTask(this.dataset.source)">+ 新建任务</button>
  </div><div class="tb-view-summary">
    <div class="tb-active-filters">${taskboardFilterChipsHtml(view)}</div>
    <span class="tb-results-count" role="status" aria-live="polite"></span>
  </div>`;
}

// 多选立即生效,但弹层、候选搜索和键盘焦点在响应后继续保留。
function toggleTaskboardFacet(element) {
  const binding = taskboardBindingOf(element);
  if (!binding) return;
  const view = taskboardState(binding).view;
  const field = element.dataset.field, value = element.dataset.value;
  if (element.checked && view.facets.reduce((n, f) => n + f.values.length + Number(f.missing), 0) >= 100) {
    element.checked = false;
    toast("最多同时选择 100 个筛选值", "warning");
    return;
  }
  let facet = view.facets.find(f => f.field.toLowerCase() === field.toLowerCase());
  if (!facet) { facet = { field, values: [], missing: false }; view.facets.push(facet); }
  if (element.dataset.missing === "true") facet.missing = element.checked;
  else if (element.checked && !facet.values.some(v => v.toLowerCase() === value.toLowerCase()))
    facet.values.push(value);
  else if (!element.checked)
    facet.values = facet.values.filter(v => v.toLowerCase() !== value.toLowerCase());
  view.facets = view.facets.filter(f => f.values.length || f.missing);
  rememberTaskboardView(binding);
  renderTaskboard(binding);
}

function filterTaskboardFacetOptions(input) {
  const panel = input.closest(".tb-filter-panel");
  const query = input.value.trim().toLowerCase();
  panel?.querySelectorAll(".tb-facet").forEach(group => {
    let found = false;
    group.querySelectorAll("[data-option-search]").forEach(option => {
      option.hidden = !option.dataset.optionSearch.includes(query);
      found ||= !option.hidden;
    });
    group.hidden = !found;
  });
}

function taskboardSearchComposition(input, composing) {
  const binding = taskboardBindingOf(input);
  if (!binding) return;
  taskboardState(binding).composing = composing;
  if (!composing) searchTaskboard(input);
}

function taskboardDraftComposition(input, composing) {
  const binding = taskboardBindingOf(input);
  if (!binding) return;
  taskboardState(binding).composing = composing;
  if (!composing) drawTaskboard(binding);
}

function searchTaskboard(input) {
  const binding = taskboardBindingOf(input);
  if (!binding) return;
  const state = taskboardState(binding);
  if (state.composing) return;
  state.view.search = input.value;
  rememberTaskboardView(binding);
  // 输入一变就作废旧请求,避免防抖期间旧搜索结果覆盖新条件。
  ++state.token;
  state.controller?.abort();
  clearTimeout(state.timer);
  state.loading = true;
  updateTaskboardResultCount(binding);
  state.timer = setTimeout(() => {
    state.timer = null;
    if (binding.visible()) renderTaskboard(binding);
  }, 250);
}

function applyTaskboardAdvanced(element) {
  const binding = taskboardBindingOf(element);
  const panel = element.closest(".tb-advanced");
  const input = panel?.querySelector(".tb-advanced-query");
  if (!binding || !input) return;
  const value = input.value.trim(), errorNode = panel.querySelector(".tb-query-error");
  try { if (value) compileLabelQuery(value); }
  catch (error) { errorNode.textContent = `表达式不合法：${error.message}`; return; }
  errorNode.textContent = "";
  taskboardState(binding).view.advanced = value;
  rememberTaskboardView(binding);
  renderTaskboard(binding);
}

function removeTaskboardViewFilter(element) {
  const binding = taskboardBindingOf(element);
  if (!binding) return;
  const view = taskboardState(binding).view;
  if (element.dataset.kind === "facet")
    view.facets = view.facets.filter(f => f.field !== element.dataset.field);
  else if (["search", "advanced"].includes(element.dataset.kind)) {
    view[element.dataset.kind] = "";
    if (element.dataset.kind === "advanced") {
      const input = binding.host().querySelector(".tb-advanced-query");
      if (input) input.value = "";
    }
  }
  rememberTaskboardView(binding);
  drawTaskboard(binding);
  renderTaskboard(binding);
}

function clearTaskboardFilters(element) {
  const binding = taskboardBindingOf(element);
  if (!binding) return;
  const view = taskboardState(binding).view;
  view.facets = []; view.search = ""; view.advanced = "";
  rememberTaskboardView(binding);
  // 清空主动丢弃高级表达式草稿,轮询才保留未应用草稿。
  const input = binding.host().querySelector(".tb-advanced-query");
  if (input) input.value = "";
  drawTaskboard(binding);
  renderTaskboard(binding);
}

function setTaskboardOption(element, field, value) {
  const binding = taskboardBindingOf(element);
  if (!binding || !["groupBy", "scope", "sort", "hideEmpty"].includes(field)) return;
  const state = taskboardState(binding);
  state.view[field] = value;
  rememberTaskboardView(binding);
  if (field === "groupBy" || field === "scope") renderTaskboard(binding);
  else drawTaskboard(binding);
}

function resetTaskboardView(element) {
  const binding = taskboardBindingOf(element);
  if (!binding) return;
  taskboardState(binding).view = defaultTaskboardView(binding.defaultGroupBy, "active");
  rememberTaskboardView(binding);
  const input = binding.host().querySelector(".tb-advanced-query");
  if (input) input.value = "";
  drawTaskboard(binding);
  renderTaskboard(binding);
}

function toggleTaskboardColumn(element) {
  const binding = taskboardBindingOf(element);
  if (!binding) return;
  const view = taskboardState(binding).view, key = element.dataset.columnKey;
  view.collapsed = view.collapsed.includes(key)
    ? view.collapsed.filter(k => k !== key) : [...view.collapsed, key];
  rememberTaskboardView(binding);
  drawTaskboard(binding);
}

function setTaskboardCollapsed(element, collapsed) {
  const binding = taskboardBindingOf(element);
  if (!binding) return;
  const state = taskboardState(binding);
  state.view.collapsed = collapsed ? (state.data?.columns || []).map(c => c.key) : [];
  rememberTaskboardView(binding);
  drawTaskboard(binding);
}

// 表达式列是共享配置,放在单独的管理对话框,不再冒充日常筛选条件。
function openTaskboardColumns(element) {
  const binding = taskboardBindingOf(element);
  if (!binding) return;
  binding.host()?.querySelectorAll(".tb-popover[open]").forEach(menu => { menu.open = false; });
  const filters = currentTaskboardFilters(binding);
  const rows = filters.map((col, i) => `<div class="tb-column-setting">
    <div><strong>${esc(col.title)}</strong><code>${esc(col.query)}</code></div>
    ${col.locked ? '<span class="muted">固定状态列</span>' : `<button class="ghost"
      data-index="${i}" onclick="removeTaskboardFilter(this, +this.dataset.index)"
      aria-label="${esc("移除自定义列 " + col.title)}">移除</button>`}</div>`).join("");
  openFormDialog("管理自定义表达式列", `<div class="tb-columns-editor"
      data-board-key="${esc(binding.key)}" data-filter-snapshot="${esc(JSON.stringify(filters))}">
    <p class="tb-help">每个表达式生成一列；任务可以同时出现在多列中。
      这里的修改会保存为共享看板配置，已有 ⚡ 自动处理规则保持原义。</p>
    <div class="tb-column-settings">${rows}</div>
    <label>新列名称（可选）<input class="tb-new-filter-title" maxlength="100" placeholder="例如 高优先级缺陷"></label>
    <label>标签表达式<input class="tb-new-filter" maxlength="2000"
      placeholder="例如 bug &amp; priority: P0"
      onkeydown="if(event.key==='Enter'&&!imeComposing(event))addTaskboardFilter(this)"></label>
    <div class="tb-query-error" role="alert"></div>
    <button class="ghost" onclick="addTaskboardFilter(this)">＋ 添加表达式列</button>
    </div>`, `<button class="action" data-board-key="${esc(binding.key)}"
      onclick="useTaskboardExpressionColumns(this)">查看表达式列</button>
      <button class="ghost" onclick="fdlg.close()">完成</button>`);
}

function useTaskboardExpressionColumns(element) {
  setTaskboardOption(element, "groupBy", "");
  fdlg.close();
}

async function commitTaskboardColumns(binding, form, filters, message) {
  const state = taskboardState(binding);
  if (state.columnsSaving) return;
  const errorNode = form.querySelector(".tb-query-error");
  // 对话框中的序号只对其快照有效,轮询发现共享配置改变时须重新确认列表。
  if (form.dataset.filterSnapshot !== JSON.stringify(currentTaskboardFilters(binding))) {
    errorNode.innerHTML = '列配置已发生变化，请重新加载后再修改。 '
      + '<button class="tb-text-button" onclick="openTaskboardColumns(this)">重新加载列设置</button>';
    return;
  }
  state.columnsSaving = true;
  const controls = [...form.querySelectorAll("input, button")];
  controls.forEach(control => { control.disabled = true; });
  try {
    await binding.save({ filters });
    if (!binding.visible()) return;
    await renderTaskboard(taskboardBindings[binding.key] || binding);
    // 保存期间可能已关闭对话框或切到其他表单,不能抢回焦点或覆盖新草稿。
    if (fdlg.open && form.isConnected && !state.error) openTaskboardColumns(form);
    toast(message, "success");
  } catch (error) {
    if (form.isConnected) errorNode.textContent = error.message;
  } finally {
    state.columnsSaving = false;
    controls.forEach(control => { control.disabled = false; });
  }
}

async function addTaskboardFilter(element) {
  const binding = taskboardBindingOf(element);
  const form = element.closest(".tb-columns-editor");
  const query = (form?.querySelector(".tb-new-filter")?.value || "").trim();
  if (!binding || !query || binding.projectId !== currentProject) return;
  const errorNode = form.querySelector(".tb-query-error");
  try { compileLabelQuery(query); }
  catch (error) { errorNode.textContent = `表达式不合法：${error.message}`; return; }
  const filters = currentTaskboardFilters(binding);
  if (filters.some(f => f.query.toLowerCase() === query.toLowerCase())) {
    errorNode.textContent = "该表达式的列已经存在"; return;
  }
  const title = form.querySelector(".tb-new-filter-title").value.trim() || query;
  await commitTaskboardColumns(binding, form,
    [...filters, { title, query, color: "" }], "已添加表达式列");
}

async function removeTaskboardFilter(element, index) {
  const binding = taskboardBindingOf(element);
  if (!binding || binding.projectId !== currentProject) return;
  const form = element.closest(".tb-columns-editor");
  if (!form || taskboardState(binding).columnsSaving) return;
  const filters = currentTaskboardFilters(binding), target = filters[index];
  if (!target || target.locked) return;
  if (filters.length <= 1) { uiAlert("看板至少保留一列"); return; }
  if (ruleForQuery(target.query)) {
    uiAlert("该列绑定了自动处理规则；请先在 ⚡ 设置中删除规则，再移除列。"); return;
  }
  filters.splice(index, 1);
  await commitTaskboardColumns(binding, form, filters, "已移除表达式列");
}

// 列本身就是单个状态时不重复显示药丸;特殊值无可绑定表达式时仍显示状态。
function isStatusColumnQuery(query) {
  const text = String(query || "").trim();
  if (/[&|!()]/.test(text)) return false;
  const [prop, value] = splitLabel(text);
  return prop.toLowerCase() === "status" && value !== "*";
}

function taskboardColumnHtml(col, source, view) {
  const collapsed = view.collapsed.includes(col.key);
  const showStatus = !isStatusColumnQuery(col.query);
  const rule = col.query ? ruleForQuery(col.query) : null;
  const ruleState = rule ? (rule.enabled ? "rule-on" : "rule-off") : "";
  const ruleTitle = rule ? (rule.enabled ? "自动处理规则已启用，点击修改" : "自动处理规则已停用，点击修改")
    : "为该列设置自动处理规则（不包含当前个人筛选条件）";
  const cards = collapsed ? "" : taskboardSortedCards(col.cards, view.sort)
    .map(card => boardCardHtml(card, { showStatus, source })).join("")
      || '<div class="tb-column-empty">暂无任务</div>';
  return `<section class="col${collapsed ? " tb-col-collapsed" : ""}"
      data-column-key="${esc(col.key)}" aria-label="${esc(col.title)}">
    <h2><span class="col-dot" style="background:${esc(col.color || "var(--muted)")}"></span>
      <button class="tb-col-title" title="${esc((collapsed ? "展开 " : "折叠 ") + col.title)}"
        aria-expanded="${!collapsed}" data-column-key="${esc(col.key)}"
        data-tb-focus="${esc("column:" + col.key)}"
        onclick="toggleTaskboardColumn(this)">${esc(col.title)}</button>
      <span class="col-count">${col.cards.length}</span>
      ${!collapsed && col.query ? `<button class="col-tool col-rule ${ruleState}"
        aria-label="${esc(col.title + " 自动处理规则")}" title="${esc(ruleTitle)}"
        data-query="${esc(col.query)}" onclick="openColumnRule(this.dataset.query)">⚡</button>` : ""}
      <button class="col-tool tb-collapse-button" aria-label="${esc((collapsed ? "展开 " : "折叠 ") + col.title)}"
        data-tb-focus="${esc("collapse:" + col.key)}"
        data-column-key="${esc(col.key)}" onclick="toggleTaskboardColumn(this)">${collapsed ? "›" : "‹"}</button>
    </h2>${collapsed ? "" : `<div class="col-list" data-scroll-key="tb:${esc(col.key)}">${cards}</div>`}
  </section>`;
}

function updateTaskboardResultCount(binding) {
  const state = taskboardState(binding), data = state.data, host = binding.host();
  host?.setAttribute("aria-busy", String(state.loading));
  const counter = host?.querySelector(".tb-results-count");
  if (!counter) return;
  if (state.loading) { counter.textContent = "正在筛选…"; return; }
  const shown = new Set((data?.columns || []).flatMap(c => c.cards.map(card => card.id))).size;
  const total = data?.counts?.[state.view.scope] ?? 0;
  counter.textContent = `显示 ${shown} / ${total} 个任务`;
}

function drawTaskboard(binding) {
  const host = binding.host(), state = taskboardState(binding), data = state.data;
  if (!host || !data || !binding.visible()) return;
  if (state.composing) { updateTaskboardResultCount(binding); return; }
  const sameBoard = host.dataset.boardKey === binding.key;
  const scrollState = sameBoard ? captureKeyedScrollPositions(host) : [];
  const openMenus = sameBoard ? [...host.querySelectorAll("details[open][data-tb-menu]")]
    .map(e => e.dataset.tbMenu) : [];
  const active = sameBoard && host.contains(document.activeElement) ? document.activeElement : null;
  const focus = active?.dataset.tbFocus;
  const selection = active && typeof active.selectionStart === "number"
    ? [active.selectionStart, active.selectionEnd] : null;
  const drafts = sameBoard ? ["facet-search", "advanced"].map(key => {
    const input = [...host.querySelectorAll("[data-tb-focus]")].find(e => e.dataset.tbFocus === key);
    return [key, input?.value];
  }) : [];
  host.dataset.boardKey = binding.key;
  host.classList.add("taskboard-host");
  const view = state.view;
  const columns = (data.columns || []).filter(c => !view.hideEmpty || c.cards.length);
  const shown = new Set((data.columns || []).flatMap(c => c.cards.map(card => card.id))).size;
  const unmatched = Math.max(0, (data.matched_count ?? shown) - shown);
  const empty = !shown ? `<div class="tb-empty-state">
    <strong>${taskboardActiveFilterCount(view) ? "没有符合条件的任务" : "当前范围暂无任务"}</strong>
    <p>${view.scope === "archived" ? "已归档的任务会显示在这里。" : "调整筛选条件或显示范围，也可以创建新任务。"}</p>
    ${taskboardActiveFilterCount(view) ? '<button class="ghost" onclick="clearTaskboardFilters(this)">清除筛选</button>' : ""}
  </div>` : "";
  const result = columns.length
    ? `<div class="taskboard-grid" data-scroll-key="tb:grid">${columns.map(col =>
        taskboardColumnHtml(col, binding.source, view)).join("")}</div>`
    : `<div class="taskboard-grid tb-grid-empty" data-scroll-key="tb:grid">${empty}</div>`;
  // 不变的轮询响应不替换 DOM,避免打断选择器、滚动与中文输入。
  const signature = JSON.stringify([data, view, state.error]);
  if (sameBoard && signature === state.signature) { updateTaskboardResultCount(binding); return; }
  state.signature = signature;
  host.innerHTML = taskboardFilterBarHtml(binding, data)
    + (state.error ? `<div class="tb-error" role="alert">${esc(state.error)}
        <button class="tb-text-button" onclick="retryTaskboard(this)">重试</button></div>` : "")
    + (unmatched ? `<div class="tb-notice">还有 ${unmatched} 个匹配任务未落入自定义表达式列。
        <button class="tb-text-button" onclick="setTaskboardOption(this, 'groupBy', 'status')">按状态查看全部</button></div>` : "")
    + (columns.length ? empty : "") + result;
  for (const menu of host.querySelectorAll("[data-tb-menu]")) menu.open = openMenus.includes(menu.dataset.tbMenu);
  const controls = [...host.querySelectorAll("[data-tb-focus]")];
  for (const [key, value] of drafts) {
    const input = controls.find(e => e.dataset.tbFocus === key);
    if (input && value !== undefined) input.value = value;
  }
  const facetSearch = host.querySelector(".tb-facet-search");
  if (facetSearch?.value) filterTaskboardFacetOptions(facetSearch);
  const restored = controls.find(e => e.dataset.tbFocus === focus);
  if (restored) {
    restored.focus({ preventScroll: true });
    if (selection && typeof restored.setSelectionRange === "function")
      restored.setSelectionRange(...selection);
  }
  restoreKeyedScrollPositions(host, scrollState);
  positionTaskboardMenus(host);
  updateTaskboardResultCount(binding);
}

function positionTaskboardMenus(host) {
  for (const menu of host.querySelectorAll(".tb-popover[open]")) {
    const panel = menu.querySelector(".tb-popover-panel");
    const viewport = document.documentElement.clientWidth;
    if (!panel || viewport <= 760) continue;
    const bounds = host.getBoundingClientRect();
    const left = Math.max(12, bounds.left), right = Math.min(viewport - 12, bounds.right);
    panel.style.setProperty("--tb-menu-max-width", `${Math.max(0, right - left)}px`);
    panel.style.setProperty("--tb-menu-shift-x", "0px");
    const rect = panel.getBoundingClientRect();
    const shift = Math.max(left - rect.left, Math.min(0, right - rect.right));
    panel.style.setProperty("--tb-menu-shift-x", `${shift}px`);
  }
}

function retryTaskboard(element) {
  const binding = taskboardBindingOf(element);
  if (binding) renderTaskboard(binding);
}

async function renderTaskboard(binding) {
  const host = binding.host();
  if (!host || !currentProject || !binding.visible()) return;
  // 自定义看板与实时组件共用宿主,接管时作废此前尚未返回的 widget 请求。
  if (host.id === "custom-board-preview") ++boardWidgetRenderToken;
  taskboardBindings[binding.key] = binding;
  const state = taskboardState(binding);
  clearTimeout(state.timer); state.timer = null;
  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  const token = ++state.token;
  state.loading = true;
  if (host.dataset.boardKey !== binding.key) {
    host.dataset.boardKey = binding.key;
    host.classList.add("taskboard-host");
    host.innerHTML = '<div class="empty" role="status">正在加载任务看板…</div>';
    state.signature = null;
  }
  updateTaskboardResultCount(binding);
  try {
    const response = await fetch(`${binding.dataUrl()}?${taskboardViewParams(state.view)}`,
      { signal: controller.signal });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === "string" ? error.detail : `请求失败 (${response.status})`);
    }
    const data = await response.json();
    if (token !== state.token || !binding.visible()) return;
    state.data = data; state.error = "";
    binding.afterData?.(data);
  } catch (error) {
    if (token !== state.token || !binding.visible() || error.name === "AbortError") return;
    state.error = `看板更新失败：${error.message}${state.data ? "（保留上次结果）" : ""}`;
    // 首次加载失败也提供重试/重置入口,坏的本地筛选不会把看板锁死。
    if (!state.data) state.data = { columns: [], labels: [], filters: [], counts: {} };
  } finally {
    if (token === state.token && binding.visible()) {
      state.loading = false;
      drawTaskboard(binding);
    }
  }
}

// composition 事件需显式监听,浏览器不会执行 HTML oncompositionstart 属性。
for (const type of ["compositionstart", "compositionend"]) {
  document.addEventListener(type, event => {
    const input = event.target;
    if (!input.closest?.(".taskboard-host")) return;
    if (input.matches(".tb-search-input")) taskboardSearchComposition(input, type === "compositionstart");
    else if (input.matches(".tb-facet-search, .tb-advanced-query"))
      taskboardDraftComposition(input, type === "compositionstart");
  });
}

// 原生 details 支持键盘;点击外侧关闭浮层,Esc 关闭当前层并把焦点还给入口。
document.addEventListener("toggle", event => {
  if (!event.target.matches?.(".tb-popover[open]")) return;
  const host = event.target.closest(".taskboard-host");
  if (host) positionTaskboardMenus(host);
}, true);
document.defaultView?.addEventListener("resize", () => {
  document.querySelectorAll(".taskboard-host").forEach(positionTaskboardMenus);
});
document.addEventListener("click", event => {
  document.querySelectorAll(".taskboard-host .tb-popover[open]").forEach(menu => {
    if (!menu.contains(event.target)) menu.open = false;
  });
});
document.addEventListener("keydown", event => {
  if (event.key !== "Escape" || imeComposing(event) || fdlg.open) return;
  const menu = document.activeElement?.closest(".tb-popover[open]")
    || document.querySelector(".taskboard-host .tb-popover[open]");
  if (menu) {
    menu.open = false;
    menu.querySelector("summary")?.focus();
    event.preventDefault();
    event.stopPropagation();
  }
});

let _previewTimer = null;
function previewBoardDraft() {
  customBoardEditing = true;
  clearTimeout(_previewTimer);   // 防抖:避免每个按键都触发数据源解析请求
  _previewTimer = setTimeout(() => {
    try { renderBoardWidgets(JSON.parse(document.getElementById("cb-layout").value)); }
    catch (_) { document.getElementById("custom-board-preview").innerHTML = `<div class="empty">布局 JSON 尚未完成。</div>`; }
  }, 400);
}

// ---- 组件渲染器:按 type 分派,未知类型回退 JSON 展示 ----
function widgetTable(columns, rows) {
  const rowList = Array.isArray(rows) ? rows : [];
  // 列定义:[{key,label}] / [str];缺省从首行对象键自动推导
  let cols = Array.isArray(columns) ? columns.map(c =>
    typeof c === "string" ? { key: c, label: c } : c) : [];
  if (!cols.length && rowList.length && !Array.isArray(rowList[0]))
    cols = Object.keys(rowList[0]).map(k => ({ key: k, label: k }));
  const body = rowList.map(r => {
    const cells = Array.isArray(r) ? r : cols.map(c => (r || {})[c.key]);
    return `<tr>${cells.map(c => `<td>${esc(typeof c === "object" && c !== null ? JSON.stringify(c) : c ?? "")}</td>`).join("")}</tr>`;
  }).join("");
  return `<table><tr>${cols.map(c => `<th>${esc(c.label ?? c.key)}</th>`).join("")}</tr>${
    body || `<tr><td colspan="${cols.length || 1}" class="empty">暂无数据</td></tr>`}</table>`;
}

// 极简 SVG 图表:bar / line / pie,无外部依赖
function widgetChart(c) {
  const data = Array.isArray(c.data) ? c.data : [];
  if (!data.length) return `<div class="empty">暂无数据</div>`;
  const xk = c.x_key || c.xKey || "x", yk = c.y_key || c.yKey || "y";
  const vals = data.map(d => Number(d[yk]) || 0);
  const labels = data.map(d => String(d[xk] ?? ""));
  const W = 560, H = 200, max = Math.max(...vals, 1);
  const colors = ["#3564d7", "#2e9e5b", "#c98a1b", "#c94b3c", "#8b5cf6", "#0e9488"];
  if (c.kind === "pie") {
    const total = vals.reduce((a, b) => a + b, 0) || 1;
    let angle = -Math.PI / 2, slices = "", legend = "";
    vals.forEach((v, i) => {
      const a2 = angle + v / total * Math.PI * 2;
      const large = (a2 - angle) > Math.PI ? 1 : 0;
      const [x1, y1] = [100 + 80 * Math.cos(angle), 100 + 80 * Math.sin(angle)];
      const [x2, y2] = [100 + 80 * Math.cos(a2), 100 + 80 * Math.sin(a2)];
      slices += `<path d="M100,100 L${x1},${y1} A80,80 0 ${large} 1 ${x2},${y2} Z" fill="${colors[i % colors.length]}"></path>`;
      legend += `<span class="pill"><span style="color:${colors[i % colors.length]}">●</span> ${esc(labels[i])}: ${vals[i]}</span> `;
      angle = a2;
    });
    return `<svg viewBox="0 0 200 200" style="max-height:160px">${slices}</svg><div>${legend}</div>`;
  }
  const bw = W / data.length;
  let marks = "";
  if (c.kind === "line") {
    const pts = vals.map((v, i) => `${bw * i + bw / 2},${H - 20 - v / max * (H - 40)}`).join(" ");
    marks = `<polyline points="${pts}" fill="none" stroke="${colors[0]}" stroke-width="2"></polyline>` +
      vals.map((v, i) => `<circle cx="${bw * i + bw / 2}" cy="${H - 20 - v / max * (H - 40)}" r="3" fill="${colors[0]}"></circle>`).join("");
  } else {   // bar
    marks = vals.map((v, i) =>
      `<rect x="${bw * i + bw * 0.15}" y="${H - 20 - v / max * (H - 40)}" width="${bw * 0.7}" height="${v / max * (H - 40)}" fill="${colors[0]}" rx="2"></rect>`).join("");
  }
  const axis = labels.map((l, i) =>
    `<text x="${bw * i + bw / 2}" y="${H - 5}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".6">${esc(l.slice(0, 8))}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%">${marks}${axis}</svg>`;
}

function renderWidgetContent(w, resolved) {
  if (resolved && resolved.error)
    return `<div class="empty">${esc(resolved.error)}</div>`;
  const c = { ...(w.content || {}), ...(resolved || {}) };
  switch (w.type) {
    case "markdown":
      return `<div class="markdown-body">${markdownPreviewHtml(c.markdown || c.text || "")}</div>`;
    case "table":
      return widgetTable(c.columns, c.rows);
    case "card": {
      const items = Array.isArray(c.metrics) ? c.metrics
        : Object.entries(c).filter(([k]) => k !== "source")
            .map(([label, value]) => ({ label, value }));
      return `<div style="display:flex;flex-wrap:wrap;gap:14px">` + (items.map(m =>
        `<div><div style="font-size:22px;font-weight:700">${esc(m.value)}${m.unit ? `<span class="muted" style="font-size:12px"> ${esc(m.unit)}</span>` : ""}</div>
         <div class="muted">${esc(m.label)}</div></div>`).join("")
        || `<div class="empty">暂无指标</div>`) + `</div>`;
    }
    case "chart":
      return widgetChart(c);
    case "list": {
      const items = (Array.isArray(c.items) ? c.items : []).map(it => {
        const o = typeof it === "string" ? { text: it } : (it || {});
        const tone = { danger: "var(--bad)", warning: "var(--warn)", success: "var(--ok)" }[o.tone];
        return `<li${tone ? ` style="color:${tone}"` : ""}>${esc(o.text ?? "")}</li>`;
      }).join("");
      return items ? `<ul style="margin:4px 0;padding-left:18px">${items}</ul>`
                   : `<div class="empty">暂无条目</div>`;
    }
    case "log": {
      const lines = Array.isArray(c.lines) ? c.lines.join("\n") : (c.text || "");
      return `<pre style="white-space:pre-wrap;margin:0;font-size:12px;max-height:100%;overflow:auto">${esc(lines) || "(空)"}</pre>`;
    }
    case "code":
      return `${c.language ? `<span class="pill">${esc(c.language)}</span>` : ""}
        <pre style="white-space:pre-wrap;margin:4px 0 0;font-size:12px">${esc(c.code || "")}</pre>`;
    case "taskboard": {   // 标签任务看板:与看板列同数据(服务端按数据源 + 标签表达式取卡片)、同规则入口
      // 渲染前已等过取数,这里没有结果说明请求失败
      if (!resolved) return `<div class="empty">任务数据暂不可用</div>`;
      const label = String(c.label || "").trim();
      const cardList = Array.isArray(c.cards) ? c.cards : [];
      const rule = ruleForQuery(label);
      const ruleText = rule
        ? (rule.enabled ? "⚡ 自动规则已启用" : "⚡ 自动规则已停用") : "⚡ 设置自动规则";
      const cards = cardList.map(card =>
        boardCardHtml(card, { showStatus: true, source: c.source })).join("")
        || `<div class="empty">暂无带此标签的 Task</div>`;
      const sourceBadge = c.source && c.source !== "built-in"
        ? `<span class="badge">${esc(boardSourceName(c.source))}</span>` : "";
      return `<div class="widget-taskboard-head">
          ${sourceBadge}<span class="badge">${esc(label)}</span>
          <span class="muted">${cardList.length} 个 Task</span>
          <button class="ghost" data-label="${esc(label)}"
            onclick="openColumnRule(this.dataset.label)">${ruleText}</button>
        </div><div class="widget-taskboard-cards">${cards}</div>`;
    }
    default:
      return `<pre style="white-space:pre-wrap;margin:0">${esc(JSON.stringify(c, null, 2))}</pre>`;
  }
}

async function renderBoardWidgets(layout) {
  const renderToken = ++boardWidgetRenderToken;
  const preview = document.getElementById("custom-board-preview");
  preview?.classList.remove("taskboard-host");
  const widgets = Array.isArray(layout) ? layout : [];
  // 实时组件:批量向平台解析(保存态与预览态共用同一端点)
  let resolved = {};
  const sourced = widgets.filter(widgetIsLive);
  if (sourced.length && currentProject) {
    try {   // 静默取数:失败时按静态内容渲染,不弹窗
      const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/widget_data`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ widgets: sourced }),
      });
      if (r.ok) resolved = await r.json();
    } catch (e) { /* ignore */ }
  }
  if (renderToken !== boardWidgetRenderToken || !preview) return;
  const scrollState = captureKeyedScrollPositions(preview);
  preview.innerHTML = widgets.map(w =>
    `<section class="widget" data-scroll-key="widget:${esc(w.id)}" style="grid-column:${Number(w.x || 0) + 1}/span ${Number(w.width || 6)};grid-row:${Number(w.y || 0) + 1}/span ${Number(w.height || 4)}">
      <span class="widget-type">${esc(w.type)}${widgetIsLive(w) ? " · 实时" : ""}</span><h3>${esc(w.title || w.id)}</h3>${renderWidgetContent(w, resolved[w.id])}</section>`
  ).join("") || `<div class="empty" style="grid-column:1/-1">面板中还没有组件。</div>`;
  restoreKeyedScrollPositions(preview, scrollState);
}

async function saveCustomBoard() {
  let layout;
  try { layout = JSON.parse(document.getElementById("cb-layout").value); }
  catch (_) { uiAlert("布局必须是合法 JSON"); return; }
  const id = document.getElementById("cb-id").value.trim();
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
    id, name: document.getElementById("cb-name").value.trim(),
    description: document.getElementById("cb-desc").value, layout,
  });
  currentCustomBoard = `${currentProject}:${id}`;
  customBoardEditing = false;
  window._newBoardDraft = null;
  await loadOverview(); renderCustomBoards(true);
  toast("面板已保存", "success");
}

async function deleteCustomBoard(boardId = null) {
  const board = projBoards().find(b => b.id === (boardId || currentCustomBoard));
  if (!board || !await uiConfirm(`将面板「${board.name}」移入项目回收站？`)) return;
  const shortId = board.id.replace(`${currentProject}:`, "");
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/boards/${encodeURIComponent(shortId)}`);
  if (currentCustomBoard === board.id) currentCustomBoard = null;
  customBoardEditing = false;
  boardEditMode = false;
  if (fdlg.open) fdlg.close();
  await loadOverview(); renderCustomBoards(true);
  toast("面板已移入回收站", "success");
}

/* ---------------- 新建面板与 taskboard 编辑 ---------------- */

function openNewBoardDialog() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  openFormDialog("新建面板", `
    <label>面板类型</label>
    <div class="new-board-kinds">
      <label><input type="radio" name="nb-kind" value="taskboard" checked
        onchange="toggleNewBoardKind()"> 任务看板 <span class="muted">选数据源,按标签表达式筛选,按状态分列</span></label>
      <label><input type="radio" name="nb-kind" value="agent"
        onchange="toggleNewBoardKind()"> ${peerModeProject() ? "角色创建面板" : "主控创建面板"} <span class="muted">一句话提需求,${peerModeProject() ? "由指定角色" : "主控"}用组件搭建</span></label>
    </div>
    <div id="nb-taskboard">
      <label>名称</label><input type="text" id="nb-name" placeholder="例如 缺陷追踪">
      <label>数据源</label>
      <select id="nb-source">${boardSourceOptionsHtml("built-in")}</select>
      <p class="muted">创建后默认按状态分列。顶部可搜索、多选筛选、切换分列属性；
        「显示选项」中可调整排序、折叠列或管理自定义表达式列。</p>
    </div>
    <div id="nb-agent" style="display:none">
      ${peerModeProject() ? `<label>交给哪个角色</label>
      <select id="nb-role">${activeProjRoles().map(r =>
        `<option value="${esc(r.id)}">@${esc(r.id)} — ${esc(r.name)}</option>`).join("")}</select>` : ""}
      <label>需求描述</label>
      <textarea id="nb-request" rows="4" style="height:auto"
        placeholder="例如:建一个需求管理面板,上面是需求清单表格,下面实时显示进行中的任务"></textarea>
      <p class="muted">需求会发到项目 general 频道${peerModeProject() ? "交给所选角色" : "交给主控"},过程可在聊天页查看;创建完成后面板自动出现在侧栏,之后的修改在面板编辑模式的专属频道里继续对话。</p>
    </div>`,
    `<button class="action" onclick="submitNewBoard()">创建</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

function toggleNewBoardKind() {
  const kind = document.querySelector('input[name="nb-kind"]:checked')?.value;
  document.getElementById("nb-taskboard").style.display =
    kind === "taskboard" ? "block" : "none";
  document.getElementById("nb-agent").style.display =
    kind === "agent" ? "block" : "none";
}

async function submitNewBoard() {
  const kind = document.querySelector('input[name="nb-kind"]:checked')?.value;
  if (kind === "taskboard") {
    const name = document.getElementById("nb-name").value.trim();
    const source = document.getElementById("nb-source").value;
    if (!name) { uiAlert("名称不能为空"); return; }
    const shortId = `tb-${Math.random().toString(36).slice(2, 8)}`;
    await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
      id: shortId, name, kind: "taskboard", source, layout: [],
    });
    fdlg.close();
    currentCustomBoard = `${currentProject}:${shortId}`;
    boardEditMode = false;
    await loadOverview();
    if (currentTab !== "custom") switchTab("custom");
    else { renderCustomBoards(true); renderSidebar(); syncUrl(); }
    toast(`已创建任务看板「${name}」`, "success");
    return;
  }
  // Agent 创建:需求发到 general 频道,创建过程全程可见;有主控交主控,
  // 无主控交所选角色(结构化提及,正文 @ 只是普通文字)
  const project = projObj();
  const request = document.getElementById("nb-request").value.trim();
  if (!request) { uiAlert("请描述你想要的面板"); return; }
  const target = peerModeProject()
    ? document.getElementById("nb-role")?.value : project.orchestrator_role_id;
  if (!target) { uiAlert("本项目没有可用角色"); return; }
  const channel = projChannels().find(c =>
    c.id.endsWith(":general") || c.id === "general") || projChannels()[0];
  if (!channel) { uiAlert("本项目还没有频道,请先在项目设置中创建"); return; }
  await api("POST", `/api/chat/${encodeURIComponent(channel.id)}/messages`, {
    author: "human",
    content: `@${target} 自定义面板需求:${request}\n` +
      `请用 MissionCrew Agent Tool 的 dashboard.save 完成,面板 id 用英文短横线命名。`,
    mentions: [{ role_id: target, start: 0, end: target.length + 1 }],
  });
  fdlg.close();
  toast(`已交给 @${target}(频道 #${channel.name});完成后面板会出现在侧栏`, "success", 6000);
}

function openTaskboardDialog(boardId) {
  const board = projBoards().find(item => item.id === boardId);
  if (!board) return;
  openFormDialog(`编辑任务看板 · ${board.name || board.id}`, `
    <label>名称</label>
    <input type="text" id="tbf-name" value="${esc(board.name || "")}">
    <label>数据源</label>
    <select id="tbf-source">${boardSourceOptionsHtml(board.source || "built-in")}</select>
    <p class="muted">日常筛选与分列在看板顶部调整；共享表达式列在「显示选项 → 管理自定义表达式列」中维护。</p>`,
    `<button class="action" data-id="${esc(board.id)}"
       onclick="saveTaskboardDialog(this.dataset.id)">保存</button>
     <button class="danger" data-id="${esc(board.id)}"
       onclick="deleteCustomBoard(this.dataset.id)">删除面板</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

async function saveTaskboardDialog(boardId) {
  const board = projBoards().find(item => item.id === boardId);
  if (!board) return;
  const name = document.getElementById("tbf-name").value.trim();
  const source = document.getElementById("tbf-source").value;
  if (!name) { uiAlert("名称不能为空"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
    id: board.id.replace(`${currentProject}:`, ""), name, source,
  });
  fdlg.close();
  await loadOverview();
  renderCustomBoards(true);
  renderSidebar();
  toast("任务看板已更新", "success");
}
