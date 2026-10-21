"""无 DOM 的看板视图模型:直接执行浏览器 JS,覆盖个人状态与精确筛选契约。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VIEW_JS = ROOT / "missioncrew" / "web" / "js" / "taskboard-view.js"
NODE = shutil.which("node")


def evaluate(script: str):
    if NODE is None:
        pytest.skip("node is required to execute the browser taskboard view model")
    runner = r"""
const fs = require("fs");
const vm = require("vm");
const context = { URLSearchParams };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
process.stdout.write(JSON.stringify(vm.runInContext(fs.readFileSync(0, "utf8"), context)));
"""
    result = subprocess.run(
        [NODE, "-e", runner, str(VIEW_JS)], input=script, text=True,
        capture_output=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def test_defaults_are_fresh_and_keep_explicit_ungrouped_view():
    result = evaluate("""
const first = defaultTaskboardView();
first.facets.push({ field: 'status', values: ['待处理'], missing: false });
first.collapsed.push('some-column');
({ fresh: defaultTaskboardView(), ungrouped: defaultTaskboardView('', 'archived'),
   invalid: defaultTaskboardView('owner: *', 'unknown') });
""")
    assert result["fresh"] == {
        "version": 1, "search": "", "facets": [], "advanced": "", "groupBy": "status",
        "scope": "active", "sort": "updated_desc", "hideEmpty": False, "collapsed": [],
    }
    assert result["ungrouped"]["groupBy"] == ""
    assert result["ungrouped"]["scope"] == "archived"
    assert result["invalid"] == result["fresh"]


def test_invalid_json_and_future_versions_fall_back_to_board_defaults():
    result = evaluate("""
const defaults = defaultTaskboardView('Owner', 'all');
const inputs = ['{bad', 'null', '[]', '"text"', { version: 2, groupBy: 'status' },
                { version: '1', scope: 'active' }, ' '.repeat(300000)];
inputs.map(input => normalizeTaskboardView(input, defaults));
""")
    assert all(view["groupBy"] == "Owner" and view["scope"] == "all" for view in result)
    assert all(view["version"] == 1 and view["facets"] == [] for view in result)


def test_normalization_preserves_valid_values_and_deduplicates_by_case():
    result = evaluate("""
const raw = {
  version: 1, search: ' R&D ', advanced: 'status: 待处理 | status: 处理中',
  groupBy: '', scope: 'all', sort: 'title_asc', hideEmpty: true,
  facets: [
    { field: ' Owner ', values: [' Alice ', 'ALICE', 'Bob'], missing: true },
    { field: 'owner', values: ['Carol', 'bob'], missing: false },
    { field: '', values: ['R&D', 'r&d', '(bug)!', 'version:'], missing: true },
    { field: 'Milestone', values: ['Gone from inventory'], missing: false },
    { field: 'R&D', values: ['A|B: test'], missing: true },
    { field: 'bad:field', values: ['bad'], missing: true },
    { field: 'Empty', values: [], missing: false }, null, 1,
  ], collapsed: ['column:one', 'column:one', '', 7, 'Column:one'], unknown: 'ignored',
};
const snapshot = JSON.stringify(raw);
const view = normalizeTaskboardView(raw);
({ view, unchanged: snapshot === JSON.stringify(raw) });
""")
    view = result["view"]
    assert result["unchanged"]
    assert view["facets"] == [
        {"field": "Owner", "values": ["Alice", "Bob", "Carol"], "missing": True},
        {"field": "", "values": ["R&D", "(bug)!", "version:"], "missing": False},
        {"field": "Milestone", "values": ["Gone from inventory"], "missing": False},
        {"field": "R&D", "values": ["A|B: test"], "missing": False},
    ]
    assert view["groupBy"] == "" and view["scope"] == "all"
    assert view["search"] == " R&D " and view["sort"] == "title_asc"
    assert view["collapsed"] == ["column:one", "Column:one"]
    assert "unknown" not in view


def test_bad_fields_and_oversized_conditions_do_not_change_filter_meaning():
    result = evaluate("""
const defaults = { ...defaultTaskboardView('priority', 'archived'), search: 'kept',
  facets: [{ field: 'Owner', values: ['Alice'], missing: false }],
  advanced: 'bug | urgent', hideEmpty: true, collapsed: ['old'] };
const view = normalizeTaskboardView({
  search: 'x'.repeat(2049), advanced: {}, groupBy: 'R&D', scope: true,
  sort: 'random', hideEmpty: 'false', collapsed: 'broken',
  facets: [{ field: 'Owner', values: Array.from({length: 257}, (_, i) => String(i)) }],
}, defaults);
view.facets[0].values.push('Bob');
view.collapsed.push('new');
const boundary = [100, 101].map(length => normalizeTaskboardView({
  facets: Array.from({length}, (_, i) => ({field: `property${i}`, values: ['selected']})),
}, defaults).facets.length);
({ view, defaults, boundary });
""")
    view = result["view"]
    assert view["search"] == "kept" and view["advanced"] == "bug | urgent"
    assert view["groupBy"] == "priority" and view["scope"] == "archived"
    assert view["sort"] == "updated_desc" and view["hideEmpty"] is True
    assert view["facets"][0]["values"] == ["Alice", "Bob"]
    assert result["defaults"]["facets"][0]["values"] == ["Alice"]
    assert result["defaults"]["collapsed"] == ["old"]
    assert result["boundary"] == [100, 1]


def test_storage_isolates_project_board_and_source_without_delimiter_collisions():
    result = evaluate("""
const data = new Map();
const storage = { getItem: key => data.get(key) ?? null,
                  setItem: (key, value) => data.set(key, value) };
const identities = [['p', 'board', 'source'], ['q', 'board', 'source'],
  ['p', 'other', 'source'], ['p', 'board', 'external'],
  ['p:board', 'source', 'x'], ['p', 'board:source', 'x']];
const writes = identities.map((ids, i) => saveTaskboardView(...ids,
  { ...defaultTaskboardView(), search: `view-${i}` }, storage));
({ writes, keys: [...data.keys()],
  loaded: identities.map(ids => loadTaskboardView(...ids, undefined, storage).search),
  absent: loadTaskboardView('unknown', 'board', 'source', defaultTaskboardView('', 'all'), storage)
});
""")
    assert all(result["writes"])
    assert len(set(result["keys"])) == 6
    assert all("v1" in key for key in result["keys"])
    assert result["loaded"] == [f"view-{index}" for index in range(6)]
    assert result["absent"]["groupBy"] == "" and result["absent"]["scope"] == "all"


def test_storage_denial_quota_failure_and_corrupt_data_are_safe():
    result = evaluate("""
const defaults = defaultTaskboardView('Owner', 'archived');
const denied = { getItem() { throw new Error('blocked'); },
                 setItem() { throw new Error('quota'); } };
const corrupt = { getItem() { return '{broken'; } };
Object.defineProperty(globalThis, 'localStorage', { get() { throw new Error('denied'); } });
({ denied: loadTaskboardView('p', 'b', 's', defaults, denied),
   corrupt: loadTaskboardView('p', 'b', 's', defaults, corrupt),
   unavailable: loadTaskboardView('p', 'b', 's', defaults),
   quota: saveTaskboardView('p', 'b', 's', defaults, denied),
   missing: saveTaskboardView('p', 'b', 's', defaults) });
""")
    assert result["denied"] == result["corrupt"] == result["unavailable"]
    assert result["denied"]["groupBy"] == "Owner"
    assert result["quota"] is False and result["missing"] is False


def test_facet_inventory_keeps_missing_values_and_case_insensitive_selection():
    result = evaluate("""
taskboardFacetGroups(['owner: Alice', 'OWNER: ALICE', 'bug', 'BUG',
  'status: 待处理', 'Status: 处理中', 'Priority: High', 'R&D: alpha', ': plain', null], {
  ...defaultTaskboardView('Milestone'), facets: [
    { field: 'OWNER', values: ['alice', 'Deleted User'], missing: true },
    { field: '', values: ['Bug', 'gone'], missing: true },
    { field: 'status', values: ['已完成'], missing: false },
    { field: 'Retired Property', values: [], missing: true },
  ],
});
""")
    assert [group["title"] for group in result[:2]] == ["状态", "标签"]
    groups = {group["field"].lower(): group for group in result}
    assert groups["owner"]["options"] == [
        {"value": "Alice", "label": "Alice", "selected": True, "missing": False},
        {"value": "Deleted User", "label": "Deleted User", "selected": True, "missing": False},
        {"value": "", "label": "未设置", "selected": True, "missing": True},
    ]
    assert groups[""]["options"] == [
        {"value": "bug", "label": "bug", "selected": True, "missing": False},
        {"value": ": plain", "label": ": plain", "selected": False, "missing": False},
        {"value": "gone", "label": "gone", "selected": True, "missing": False},
    ]
    assert groups["status"]["options"][-2] == {
        "value": "已完成", "label": "已完成", "selected": True, "missing": False,
    }
    assert groups["milestone"]["options"] == [
        {"value": "", "label": "未设置", "selected": False, "missing": True},
    ]
    assert groups["retired property"]["options"][0]["selected"] is True
    assert not any(option["missing"] for option in groups["r&d"]["options"])


def test_request_parameters_preserve_exact_labels_without_building_expressions():
    result = evaluate("""
const params = taskboardViewParams({ ...defaultTaskboardView('', 'all'),
  search: '  Fix & test  ', advanced: ' status: 待处理 | status: 处理中 ', facets: [
    { field: 'status', values: ['待处理', '处理中'], missing: false },
    { field: '', values: ['R&D', 'bug!(a|b)', 'C++', 'hash#?%'], missing: false },
    { field: 'Owner', values: ['A&B: team'], missing: true },
    { field: 'Priority', values: [], missing: true },
  ], hideEmpty: true, collapsed: ['column'], sort: 'created_desc',
});
({ entries: [...new URLSearchParams(params.toString()).entries()],
   empty: [...taskboardViewParams(defaultTaskboardView()).entries()] });
""")
    assert result["entries"] == [
        ["scope", "all"], ["view_group_by", ""], ["search", "Fix & test"],
        ["filter_query", "status: 待处理 | status: 处理中"],
        ["filter_label", "status: 待处理"], ["filter_label", "status: 处理中"],
        ["filter_label", "R&D"], ["filter_label", "bug!(a|b)"],
        ["filter_label", "C++"], ["filter_label", "hash#?%"],
        ["filter_label", "Owner: A&B: team"], ["missing_property", "Owner"],
        ["missing_property", "Priority"],
    ]
    assert result["empty"] == [["scope", "active"], ["view_group_by", "status"]]


def test_sorts_have_stable_id_ties_and_do_not_mutate_cards_or_the_input_array():
    result = evaluate("""
const cards = Object.freeze([
  Object.freeze({ id: 'b', title: 'Bravo', updated_at: 30, created_at: 10 }),
  Object.freeze({ id: 'c', title: 'Alpha', updated_at: 10, created_at: 30 }),
  Object.freeze({ id: 'a', title: 'alpha', updated_at: 30, created_at: 20 }),
  Object.freeze({ id: 'd', title: 'Zulu', updated_at: 'invalid' }),
]);
const before = JSON.stringify(cards);
const modes = ['updated_desc', 'updated_asc', 'created_desc', 'title_asc', 'invalid'];
const sorted = modes.map(mode => taskboardSortedCards(cards, mode));
({ ids: sorted.map(items => items.map(card => card.id)),
   unchanged: JSON.stringify(cards) === before,
   copies: sorted.every(items => items !== cards),
   references: sorted.every(items => items.every(card => cards.includes(card))),
   empty: taskboardSortedCards(null, 'updated_desc') });
""")
    assert result["ids"] == [
        ["a", "b", "c", "d"], ["c", "a", "b", "d"], ["c", "a", "b", "d"],
        ["a", "c", "b", "d"], ["a", "b", "c", "d"],
    ]
    assert result["unchanged"] and result["copies"] and result["references"]
    assert result["empty"] == []


def test_update_sorts_prefer_external_time_and_system_sort_ignores_it():
    result = evaluate("""
const cards = [
  { id: 'a', updated_at: 50, external_updated_at: 10 },
  { id: 'b', updated_at: 20, external_updated_at: 0 },
  { id: 'c', updated_at: 5, external_updated_at: 40 },
];
({ updated: taskboardSortedCards(cards, 'updated_desc').map(card => card.id),
   oldest: taskboardSortedCards(cards, 'updated_asc').map(card => card.id),
   system: taskboardSortedCards(cards, 'system_updated_desc').map(card => card.id),
   kept: normalizeTaskboardView({ sort: 'system_updated_desc' }, defaultTaskboardView('status')).sort });
""")
    # 外部时间优先,未提供(0)时退回系统更新时间:c=40, b=20, a=10
    assert result == {"updated": ["c", "b", "a"], "oldest": ["a", "b", "c"],
                      "system": ["a", "b", "c"], "kept": "system_updated_desc"}
