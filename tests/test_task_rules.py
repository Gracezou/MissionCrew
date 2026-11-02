"""自动处理规则编辑器:服务端预览口径与前端条件模型(执行真实浏览器 JS)。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import tasks as task_ops
from missioncrew.core import label_query
from missioncrew.core.models import BoardDataSource, Task, TaskAutoRule

RULE_JS = Path(__file__).resolve().parents[1] / "missioncrew" / "web" / "js" / "rule-condition.js"
NODE = shutil.which("node")


def _task(store, id, labels, **fields):
    task = Task(**{"id": id, "project_id": "webshop", "title": id,
                   "labels": labels, **fields})
    store.put_task(task)
    return task


def _rules(store, *rules):
    project = store.get_project("webshop")
    project.task_auto_rules = [TaskAutoRule(**rule) for rule in rules]
    store.put_project(project)


@pytest.fixture
def client(seeded):
    seeded.put_board_datasource(BoardDataSource(
        id="webshop:issues", project_id="webshop", name="GitHub Issues",
        status_values=[{"value": "Open", "color": ""}, {"value": "Reopened", "color": ""}]))
    _task(seeded, "alice-bug", ["bug", "owner: Alice", "status: 待处理"])
    _task(seeded, "urgent-bug", ["bug", "urgent", "status: 处理中"])
    _task(seeded, "done-bug", ["bug", "status: 已完成"])
    _task(seeded, "archived-bug", ["bug", "archived-only"], archived=True)
    _task(seeded, "issue-bug", ["bug", "status: Open"], source_id="issues")
    _task(seeded, "feature", ["feature", "status: 待处理"])
    _rules(seeded, {"query": "owner: Alice"}, {"query": "urgent", "enabled": False})
    test_client = TestClient(create_app())
    yield test_client
    test_client.close()


def _preview(client, **params):
    response = client.get("/api/projects/webshop/task_rules/preview", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_marks_where_each_matching_task_goes(client):
    data = _preview(client, query="bug")
    outcomes = {task["id"]: task["outcome"] for task in data["tasks"]}
    # 跨数据源、排除归档;已完成不派发;只有启用的前序规则会抢先命中
    assert outcomes == {"alice-bug": "shadowed", "urgent-bug": "handled",
                        "done-bug": "done", "issue-bug": "handled"}
    assert data["matched"] == 4 and data["handled"] == 2 and data["position"] == 3
    shadowed = next(task for task in data["tasks"] if task["id"] == "alice-bug")
    assert shadowed["shadowed_by"] == {"index": 1, "query": "owner: Alice"}
    issue = next(task for task in data["tasks"] if task["id"] == "issue-bug")
    assert issue["source"] == "issues" and issue["source_name"] == "GitHub Issues"
    assert issue["status"] == "Open"


def test_preview_of_an_existing_rule_only_counts_rules_before_it(client):
    data = _preview(client, query="bug", rule="OWNER: alice")
    assert data["position"] == 1
    assert {task["id"]: task["outcome"] for task in data["tasks"]}["alice-bug"] == "handled"
    assert data["handled"] == 3


def test_preview_labels_cover_all_sources_and_declared_statuses(client):
    data = _preview(client)
    assert data["matched"] == 0 and data["tasks"] == []
    labels = set(data["labels"])
    assert {"bug", "owner: Alice", "feature", "status: Open",
            "status: Reopened", "status: 待处理", "status: 已完成"} <= labels
    assert "archived-only" not in labels


def test_preview_truncates_long_results(seeded, client):
    data = task_ops.preview_auto_rule(seeded, seeded.get_project("webshop"), "bug", limit=1)
    assert data["matched"] == 4 and len(data["tasks"]) == 1 and data["truncated"]


@pytest.mark.parametrize("query", ["(bug", "bug &", "x" * 2001])
def test_invalid_preview_condition_returns_400(client, query):
    response = client.get("/api/projects/webshop/task_rules/preview", params={"query": query})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("条件不合法")


def test_preview_requires_an_existing_project(client):
    response = client.get("/api/projects/missing/task_rules/preview", params={"query": "bug"})
    assert response.status_code == 404


def evaluate(script: str):
    if NODE is None:
        pytest.skip("node is required to execute the rule condition model")
    runner = r"""
const fs = require("fs"), vm = require("vm");
const context = {};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
process.stdout.write(JSON.stringify(vm.runInContext(fs.readFileSync(0, "utf8"), context)));
"""
    result = subprocess.run([NODE, "-e", runner, str(RULE_JS)], input=script,
                            text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


def test_visual_rows_round_trip_to_canonical_expressions():
    result = evaluate("""
const model = ruleConditionFromQuery('bug&&owner:Alice & !status: 已完成 & !priority : *');
({ model, query: ruleConditionToQuery(model).query,
   any: ruleConditionFromQuery('urgent | owner: *'),
   empty: ruleConditionFromQuery('  ') });
""")
    assert result["model"] == {"join": "all", "rows": [
        {"field": "", "op": "is", "value": "bug"},
        {"field": "owner", "op": "is", "value": "Alice"},
        {"field": "status", "op": "not", "value": "已完成"},
        {"field": "priority", "op": "unset", "value": ""},
    ]}
    assert result["query"] == "bug & owner: Alice & !status: 已完成 & !priority: *"
    assert result["any"] == {"join": "any", "rows": [
        {"field": "", "op": "is", "value": "urgent"},
        {"field": "owner", "op": "set", "value": ""},
    ]}
    assert result["empty"] == {"join": "all", "rows": []}


def test_nested_or_mixed_conditions_stay_in_expression_mode():
    result = evaluate("""
['(a & b) | c', 'a & b | c', '!(a | b)', '!!a'].map(ruleConditionFromQuery)
""")
    assert result == [None, None, None, None]


def test_canonical_form_matches_backend_semantics():
    expressions = ["a&&b||c", "!(a|b)", "(a | b) & c", "owner:Alice", "owner : *",
                   "!(owner: *) & (bug | R)", "a & (b & c)"]
    canonical = evaluate(f"{json.dumps(expressions)}.map(canonicalRuleQuery)")
    assert canonical[:5] == ["a & b | c", "!(a | b)", "(a | b) & c",
                             "owner: Alice", "owner: *"]
    samples = [[], ["a"], ["a", "b"], ["c"], ["b", "c"], ["owner: Alice"], ["bug"],
               ["owner: Bob", "R"], ["a", "b", "c"]]
    for original, formatted in zip(expressions, canonical):
        for labels in samples:
            assert (label_query.matches(original, labels)
                    == label_query.matches(formatted, labels)), (original, formatted, labels)
    same = evaluate("[sameRuleQuery('Bug&&owner:alice', 'bug & owner: Alice'),"
                    " sameRuleQuery('bug', 'bugs')]")
    assert same == [True, False]


def test_row_validation_rejects_values_the_expression_language_cannot_hold():
    result = evaluate("""
[{ field: '', op: 'is', value: 'R&D' }, { field: 'owner', op: 'is', value: '*' },
 { field: 'owner', op: 'is', value: ' ' }, { field: 'a:b', op: 'set', value: '' },
 { field: '', op: 'set', value: '' }, { field: 'owner', op: 'unset', value: '' },
 { field: 'owner', op: 'not', value: 'Bob' }].map(ruleRowError)
""")
    assert [bool(message) for message in result] == [True, True, True, True, True, False, False]
    partial = evaluate("""
ruleConditionToQuery({ join: 'any', rows: [{ field: '', op: 'is', value: 'bug' },
  { field: '', op: 'is', value: '' }, { field: 'owner', op: 'not', value: 'Bob' }] })
""")
    assert partial["query"] == "bug | !owner: Bob"
    assert [bool(message) for message in partial["errors"]] == [False, True, False]


def test_descriptions_and_suggestions():
    result = evaluate("""
({ text: describeRuleCondition('bug & owner: Alice & !status: 已完成 & !priority: * & 责任人: *'),
   any: describeRuleCondition('a | !b'),
   nested: describeRuleCondition('(a & b) | c'),
   invalid: describeRuleCondition('(a'),
   choices: ruleConditionChoices(['status: 处理中', 'status: 待处理', 'Status: 待处理',
     'zeta: 1', 'Alpha: b', 'alpha: A', 'bug', 'BUG', 'api', null]) });
""")
    assert result["text"] == ("带标签「bug」，且 owner 是「Alice」，且状态不是「已完成」，"
                              "且未设置 priority，且已设置责任人")
    assert result["any"] == "带标签「a」，或不带标签「b」"
    assert result["nested"] is None and result["invalid"] is None
    assert result["choices"] == {
        "fields": [{"field": "status", "values": ["处理中", "待处理"]},
                   {"field": "Alpha", "values": ["A", "b"]},
                   {"field": "zeta", "values": ["1"]}],
        "plain": ["api", "bug"],
    }
