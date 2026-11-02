"""执行真实看板 JS:请求竞态、共享列提交与中文输入的行为回归。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


WEB_JS = Path(__file__).resolve().parents[1] / "missioncrew" / "web" / "js"
NODE = shutil.which("node")

# 只提供被测控制器需要的宿主/表单节点;不模拟浏览器布局或 HTML 解析。
# fetch 故意不响应 AbortSignal,以复现已在返回途中的旧请求。
_RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const host = {
  id: 'custom-board-preview', dataset: {}, innerHTML: '', attributes: {},
  classList: { add() {}, remove() {} },
  setAttribute(name, value) { this.attributes[name] = String(value); }, contains: () => false,
  querySelector: () => null, querySelectorAll: () => [],
};
const description = { textContent: '' }, requests = [], documentListeners = new Map();
const context = {
  AbortController, URLSearchParams, setTimeout, clearTimeout, deferred, host, requests,
  dispatchDocumentEvent: (type, target) => {
    for (const listener of documentListeners.get(type) || []) listener({ type, target });
  },
  currentProject: 'p', currentTab: 'custom', taskFilter: 'active',
  boardRows: [{ id: 'p:main', kind: 'taskboard', source: 'alpha', filters: [] }],
  fdlg: { open: true, close() { this.open = false; } },
  document: {
    activeElement: null,
    addEventListener(type, listener) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(listener);
    },
    getElementById: id => id === 'custom-board-preview' ? host
      : id === 'custom-board-desc' ? description : null,
    querySelector: () => null, querySelectorAll: () => [],
  },
  localStorage: { getItem: () => null, setItem() {} },
  esc: value => String(value ?? ''),
  // 与 tasks.js 同义的标签拆分/归一化,供表达式编译使用
  splitLabel: text => {
    const raw = String(text || '').trim(), idx = raw.indexOf(':');
    const prop = idx > 0 ? raw.slice(0, idx).trim() : '', value = raw.slice(idx + 1).trim();
    return prop && value ? [prop, value] : ['', raw];
  },
  normalizeLabel: text => {
    const [prop, value] = context.splitLabel(text);
    return prop ? `${prop}: ${value}` : value;
  },
  sourceStatusValues: () => [{ value: 'Open', color: '' }],
  captureKeyedScrollPositions: () => [], restoreKeyedScrollPositions() {},
  fetch: (url, options) => {
    const request = { ...deferred(), url, signal: options?.signal };
    requests.push(request);
    return request.promise;
  },
  response: marker => ({ ok: true, json: async () => ({
    marker, source: { id: marker, name: marker }, columns: [], labels: [], filters: [],
    counts: { active: 0, archived: 0, all: 0 }, matched_count: 0,
  }) }),
};
context.projBoards = () => context.boardRows;
vm.createContext(context);
for (const file of ['taskboard-view.js', 'boards.js']) {
  vm.runInContext(fs.readFileSync(process.argv[1] + '/' + file, 'utf8'), context);
}
vm.runInContext(`
currentCustomBoard = 'p:main';
function setupColumnEditor() {
  const binding = customTaskboardBinding(boardRows[0]);
  taskboardBindings[binding.key] = binding;
  taskboardState(binding).data = { filters: [
    { title: 'Bug', query: 'bug', color: '' },
    { title: 'Urgent', query: 'urgent', color: '' },
  ] };
  const error = { textContent: '', innerHTML: '' };
  const controls = [{ disabled: false }, { disabled: false }];
  const form = {
    id: 'editor', isConnected: true,
    dataset: { boardKey: binding.key,
      filterSnapshot: JSON.stringify(currentTaskboardFilters(binding)) },
    querySelector: selector => selector === '.tb-query-error' ? error : null,
    querySelectorAll: () => controls,
  };
  const saved = [], refreshed = [], reopened = [];
  renderTaskboard = async item => { refreshed.push(item.key); };
  openTaskboardColumns = item => { reopened.push(item.id); };
  toast = () => {};
  return { binding, form, controls, error, saved, refreshed, reopened };
}
`, context);
const script = fs.readFileSync(0, 'utf8');
Promise.resolve(vm.runInContext('(async () => {' + script + '\n})()', context))
  .then(result => process.stdout.write(JSON.stringify(result)))
  .catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
"""


def evaluate(script):
    if NODE is None:
        pytest.skip("node is required to execute the browser taskboard renderer")
    result = subprocess.run(
        [NODE, "-e", _RUNNER, str(WEB_JS)], input=script,
        text=True, capture_output=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def test_custom_binding_requires_current_source_kind_and_board_identity():
    result = evaluate("""
const board = boardRows[0], binding = customTaskboardBinding(board);
const visible = [binding.visible()];
board.source = 'beta'; visible.push(binding.visible());
board.source = 'alpha'; board.kind = 'widgets'; visible.push(binding.visible());
board.kind = 'taskboard'; currentCustomBoard = 'p:other'; visible.push(binding.visible());
currentCustomBoard = board.id; currentProject = 'q'; visible.push(binding.visible());
currentProject = 'p'; visible.push(binding.visible());
return visible;
""")
    assert result == [True, False, False, False, False, True]


def test_late_response_cannot_replace_a_newer_search_even_when_abort_is_ignored():
    result = evaluate("""
const binding = customTaskboardBinding(boardRows[0]), draws = [];
drawTaskboard = item => { draws.push(taskboardState(item).data.marker); };
const older = renderTaskboard(binding);
taskboardState(binding).view.search = 'new search';
const newer = renderTaskboard(binding);
requests[1].resolve(response('new'));
await newer;
requests[0].resolve(response('old'));
await older;
return { draws, marker: taskboardState(binding).data.marker,
  aborted: requests[0].signal.aborted, loading: taskboardState(binding).loading,
  search: new URLSearchParams(requests[1].url.split('?')[1]).get('search') };
""")
    assert result == {
        "draws": ["new"], "marker": "new", "aborted": True,
        "loading": False, "search": "new search",
    }


def test_late_response_from_previous_source_is_discarded():
    result = evaluate("""
const alpha = customTaskboardBinding(boardRows[0]), draws = [];
drawTaskboard = item => { draws.push([item.source, taskboardState(item).data.marker]); };
const older = renderTaskboard(alpha);
boardRows[0].source = 'beta';
const beta = customTaskboardBinding(boardRows[0]);
const newer = renderTaskboard(beta);
requests[1].resolve(response('beta-result'));
await newer;
requests[0].resolve(response('alpha-result'));
await older;
return { draws, oldData: taskboardState(alpha).data,
  currentData: taskboardState(beta).data.marker, currentHost: host.dataset.boardKey === beta.key };
""")
    assert result == {
        "draws": [["beta", "beta-result"]], "oldData": None,
        "currentData": "beta-result", "currentHost": True,
    }


def test_pending_widget_response_cannot_overwrite_taskboard_host():
    result = evaluate("""
drawTaskboard = () => { host.innerHTML = 'current taskboard'; };
const widgets = renderBoardWidgets([{ id: 'live', type: 'list',
  content: { source: { from: 'tasks' } } }]);
const taskboard = renderTaskboard(customTaskboardBinding(boardRows[0]));
requests[1].resolve(response('current'));
await taskboard;
requests[0].resolve({ ok: true, json: async () => ({ live: { items: [] } }) });
await widgets;
return host.innerHTML;
""")
    assert result == "current taskboard"


def test_shared_columns_serialize_writes_until_the_refresh_has_completed():
    result = evaluate("""
const editor = setupColumnEditor(), save = deferred(), refresh = deferred(), refreshing = deferred();
const { binding, form, controls, saved, reopened } = editor;
binding.save = patch => { saved.push(patch); return save.promise; };
renderTaskboard = async () => { refreshing.resolve(); await refresh.promise; };
const first = commitTaskboardColumns(binding, form, [{ query: 'first' }], 'saved');
await commitTaskboardColumns(binding, form, [{ query: 'second' }], 'ignored');
const pending = { calls: saved.length, disabled: controls.every(item => item.disabled),
  saving: taskboardState(binding).columnsSaving };
save.resolve();
await refreshing.promise;
await commitTaskboardColumns(binding, form, [{ query: 'third' }], 'also ignored');
const duringRefresh = saved.length;
refresh.resolve();
await first;
return { pending, duringRefresh, saved, reopened, closed: !fdlg.open,
  saving: taskboardState(binding).columnsSaving,
  disabled: controls.some(item => item.disabled) };
""")
    assert result["pending"] == {"calls": 1, "disabled": True, "saving": True}
    assert result["duringRefresh"] == 1
    assert result["saved"] == [{"filters": [{"query": "first"}]}]
    assert result["closed"] and result["reopened"] == []
    assert not result["saving"] and not result["disabled"]


def test_stale_column_snapshot_blocks_the_write_after_background_reordering():
    result = evaluate("""
const { binding, form, error, controls, saved, reopened } = setupColumnEditor();
binding.save = async patch => { saved.push(patch); };
taskboardState(binding).data.filters.reverse();
await commitTaskboardColumns(binding, form, [{ query: 'urgent' }], 'saved');
return { saved, reopened, error: error.innerHTML, open: fdlg.open,
  disabled: controls.some(item => item.disabled), saving: !!taskboardState(binding).columnsSaving };
""")
    assert result["saved"] == [] and result["reopened"] == [] and result["open"]
    assert "列配置已被其他人修改" in result["error"]
    assert not result["saving"] and not result["disabled"]


@pytest.mark.parametrize("dismiss, still_open", [
    ("fdlg.open = false;", False), ("form.isConnected = false;", True)])
def test_finished_save_does_not_close_a_dialog_that_replaced_the_editor(dismiss, still_open):
    result = evaluate("""
const { binding, form, saved, refreshed, reopened } = setupColumnEditor();
const save = deferred();
binding.save = patch => { saved.push(patch); return save.promise; };
const pending = commitTaskboardColumns(binding, form, [{ query: 'bug' }], 'saved');
""" + dismiss + """
save.resolve();
await pending;
return { saves: saved.length, refreshes: refreshed.length, reopened, open: fdlg.open,
  saving: taskboardState(binding).columnsSaving };
""")
    # 编辑器已被关闭或替换成别的表单时,保存完成不能关掉当前对话框。
    assert result == {"saves": 1, "refreshes": 1, "reopened": [], "open": still_open,
                      "saving": False}


def test_failed_column_save_releases_controls_and_allows_retry():
    result = evaluate("""
const { binding, form, controls, error, refreshed } = setupColumnEditor();
let calls = 0;
binding.save = async () => { if (++calls === 1) throw new Error('save failed'); };
await commitTaskboardColumns(binding, form, [{ query: 'bug' }], 'saved');
const failed = { error: error.textContent, disabled: controls.some(item => item.disabled),
  saving: taskboardState(binding).columnsSaving };
await commitTaskboardColumns(binding, form, [{ query: 'bug' }], 'retried');
return { failed, calls, refreshes: refreshed.length };
""")
    assert result["failed"] == {"error": "save failed", "disabled": False, "saving": False}
    assert result["calls"] == 2 and result["refreshes"] == 1


def test_builtin_payload_keeps_locked_columns_only_as_moved_position_markers():
    result = evaluate("""
const open = { title: 'Open', query: 'status: Open', color: '', locked: true };
const bug = { title: 'Bug', query: 'bug', color: '', locked: false };
return [builtinTaskboardFilterPayload([open, bug]),
        builtinTaskboardFilterPayload([bug, open])];
""")
    assert result == [
        [{"title": "Bug", "query": "bug", "color": ""}],
        [{"title": "Bug", "query": "bug", "color": ""},
         {"title": "Open", "query": "status: Open", "color": ""}],
    ]


def test_frontend_label_query_matches_backend_semantics():
    from missioncrew.core.label_query import matches
    cases = [("owner", ["owner: 张三"]), ("!owner", ["owner: 张三"]), ("!owner", ["bug"]),
             ("! 责任人 & ! agent-ready", ["bug"]), ("! 责任人 & ! agent-ready", ["责任人: 李四"]),
             ("owner:", ["owner: x"]), ("owner: *", ["Owner: x"]), ("bug | crash", ["CRASH"])]
    result = evaluate(f"return {json.dumps(cases)}.map(([q, labels]) => matchLabelQuery(q, labels));")
    assert result == [matches(query, labels) for query, labels in cases]


def test_column_draft_problems_flag_syntax_duplicates_and_empty_only_when_saving():
    result = evaluate("""
const rows = [{ title: '', query: 'bug' }, { title: '', query: 'BUG ' },
  { title: 'x', query: '(bug' }, { title: '新列', query: '' }];
return [rows.map(row => columnRowProblem(row, rows, false)),
        columnRowProblem(rows[3], rows, true)];
""")
    assert result == [
        ["", "与上方列的表达式重复", "表达式不合法：缺少右括号", ""], "表达式不能为空"]


@pytest.mark.parametrize("input_class", [".tb-facet-search", ".tb-advanced-query"])
def test_registered_draft_ime_events_defer_redraw_without_applying_a_search(input_class):
    result = evaluate("const inputClass = " + json.dumps(input_class) + ";\n" + """
const binding = customTaskboardBinding(boardRows[0]);
taskboardBindings[binding.key] = binding;
host.dataset.boardKey = binding.key;
host.innerHTML = '正在组合中文输入';
const state = taskboardState(binding);
state.view.search = 'existing search';
state.view.advanced = 'bug';
const input = { value: '未应用的中文草稿', closest: () => host,
  matches: selector => selector.split(',').some(item => item.trim() === inputClass) };
// 触发 boards.js 实际注册的委托监听器;只有 helper 正确但未绑定事件也必须失败。
dispatchDocumentEvent('compositionstart', input);
const refresh = renderTaskboard(binding);
const busyBeforeResponse = host.attributes['aria-busy'];
requests[0].resolve(response('updated while composing'));
await refresh;
const during = { html: host.innerHTML, composing: state.composing,
  busy: host.attributes['aria-busy'], loading: state.loading };
dispatchDocumentEvent('compositionend', input);
const result = { busyBeforeResponse, during, redrawn: host.innerHTML.includes('tb-filter-bar'),
  composing: state.composing, search: state.view.search, advanced: state.view.advanced,
  requests: requests.length, pendingSearch: state.timer !== null };
clearTimeout(state.timer);
return result;
""")
    assert result == {
        "busyBeforeResponse": "true",
        "during": {
            "html": "正在组合中文输入", "composing": True,
            "busy": "false", "loading": False,
        },
        "redrawn": True, "composing": False,
        "search": "existing search", "advanced": "bug",
        "requests": 1, "pendingSearch": False,
    }


def test_registered_search_ime_events_submit_only_the_completed_text():
    result = evaluate("""
const binding = customTaskboardBinding(boardRows[0]);
taskboardBindings[binding.key] = binding;
host.dataset.boardKey = binding.key;
const state = taskboardState(binding), scheduled = [], submitted = [];
state.view.search = 'previous search';
state.view.advanced = 'bug';
state.controller = new AbortController();
const previousController = state.controller, previousToken = state.token;
setTimeout = callback => { scheduled.push(callback); return scheduled.length; };
clearTimeout = () => {};
renderTaskboard = item => {
  submitted.push(taskboardViewParams(taskboardState(item).view).get('search'));
};
const input = { value: 'zhongwen', closest: () => host,
  matches: selector => selector.split(',').some(item => item.trim() === '.tb-search-input') };
dispatchDocumentEvent('compositionstart', input);
searchTaskboard(input);  // 浏览器在组合输入期间也会产生普通 input 事件。
const during = { composing: state.composing, search: state.view.search,
  scheduled: scheduled.length, aborted: previousController.signal.aborted };
input.value = '中文任务';
dispatchDocumentEvent('compositionend', input);
const completed = { composing: state.composing, search: state.view.search,
  advanced: state.view.advanced, scheduled: scheduled.length,
  aborted: previousController.signal.aborted, invalidated: state.token > previousToken };
scheduled.forEach(callback => callback());
return { during, completed, submitted, pending: state.timer !== null };
""")
    assert result == {
        "during": {
            "composing": True, "search": "previous search", "scheduled": 0, "aborted": False,
        },
        "completed": {
            "composing": False, "search": "中文任务", "advanced": "bug", "scheduled": 1,
            "aborted": True, "invalidated": True,
        },
        "submitted": ["中文任务"], "pending": False,
    }


def test_delegated_ime_events_ignore_unrelated_inputs_and_non_element_targets():
    result = evaluate("""
const binding = customTaskboardBinding(boardRows[0]);
taskboardBindings[binding.key] = binding;
host.dataset.boardKey = binding.key;
const state = taskboardState(binding), transitions = [];
state.view.search = 'kept';
for (const target of [
  { value: 'elsewhere', closest: () => null, matches: () => true },
  { value: 'another board control', closest: () => host, matches: () => false },
  {},
]) {
  for (const type of ['compositionstart', 'compositionend']) {
    dispatchDocumentEvent(type, target);
    transitions.push(!!state.composing);
  }
}
return { transitions, search: state.view.search, requests: requests.length,
  pending: state.timer !== null };
""")
    assert result == {
        "transitions": [False] * 6, "search": "kept", "requests": 0, "pending": False,
    }
