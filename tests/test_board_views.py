"""任务看板个人视图:独立筛选/分列、精确多选与旧配置兼容。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import board_sources
from missioncrew.core.models import Board, BoardDataSource, Task


def _task(store, id, labels, **fields):
    values = {"id": id, "project_id": "webshop", "title": id,
              "labels": labels, **fields}
    task = Task(**values)
    store.put_task(task)
    return task


def _cards(data):
    return {card["id"] for column in data["columns"] for card in column["cards"]}


def _columns(data):
    return {column["title"]: {card["id"] for card in column["cards"]}
            for column in data["columns"]}


@pytest.fixture
def view_client(seeded):
    seeded.put_board(Board(
        id="webshop:view", project_id="webshop", kind="taskboard",
        filters=[{"title": "缺陷", "query": "bug", "color": "#f00"}],
        group_by="owner"))
    client = TestClient(create_app())
    yield client
    client.close()


def test_view_grouping_is_independent_and_saved_configuration_is_unchanged(seeded, view_client):
    _task(seeded, "bug", ["bug", "owner: Alice", "status: 待处理"])
    _task(seeded, "feature", ["feature", "owner: Bob", "status: Reopened"])
    _task(seeded, "unassigned", ["feature"])
    url = "/api/projects/webshop/boards/view/data"
    saved = seeded.get_board("webshop:view").to_dict()

    # 没有个人分列参数仍按保存的表达式列并集圈定范围,列 key 保持旧协议。
    legacy = view_client.get(url).json()
    assert _columns(legacy) == {"Alice": {"bug"}, "未设置 owner": set()}
    assert [column["key"] for column in legacy["columns"]] == ["g0", "g1"]
    assert legacy["matched_count"] == 1

    # 显式分列从 source + scope 全集取数,不受保存的 bug 范围限制。
    grouped = view_client.get(url, params={"view_group_by": "owner"}).json()
    assert _columns(grouped) == {
        "Alice": {"bug"}, "Bob": {"feature"}, "未设置 owner": {"unassigned"}}
    assert grouped["matched_count"] == 3
    assert grouped["filters"] == legacy["filters"]
    filtered = view_client.get(url, params={
        "view_group_by": "owner", "filter_query": "feature"}).json()
    assert _columns(filtered) == {
        "Alice": set(), "Bob": {"feature"}, "未设置 owner": {"unassigned"}}
    assert filtered["labels"] == grouped["labels"]
    assert filtered["counts"] == grouped["counts"]

    # 空参数显式恢复表达式列;未落入列的结果计入 matched_count,供 UI 解释。
    expressions = view_client.get(url, params={"view_group_by": ""}).json()
    assert expressions["group_by"] == ""
    assert _columns(expressions) == {"缺陷": {"bug"}}
    assert expressions["matched_count"] == 3
    hidden_match = view_client.get(url, params={
        "view_group_by": "", "search": "unassigned"}).json()
    assert _cards(hidden_match) == set() and hidden_match["matched_count"] == 1
    assert seeded.get_board("webshop:view").to_dict() == saved


@pytest.mark.parametrize("endpoint", ["builtin_board", "boards/view"])
def test_status_view_filters_without_losing_unknown_or_unset_status(seeded, view_client, endpoint):
    _task(seeded, "bug-open", ["bug", "status: 待处理"])
    _task(seeded, "feature", ["feature", "status: 处理中"])
    _task(seeded, "bug-unknown", ["bug", "status: Reopened"])
    _task(seeded, "bug-unset", ["bug"])
    url = f"/api/projects/webshop/{endpoint}/data"
    response = view_client.get(url, params={
        "view_group_by": "status", "filter_label": "bug"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert list(_columns(data)) == [
        "待处理", "处理中", "已阻塞", "已完成", "Reopened", "未设置 status"]
    assert _cards(data) == {"bug-open", "bug-unknown", "bug-unset"}
    assert data["matched_count"] == 3
    assert data["counts"] == {"all": 4, "active": 4, "archived": 0}
    assert "feature" in data["labels"] and "status: Reopened" in data["labels"]

    # 零结果保留列与标签建议,以便用户调整或清空条件。
    empty = view_client.get(url, params={
        "view_group_by": "status", "search": "does-not-exist"}).json()
    assert empty["matched_count"] == 0 and _cards(empty) == set()
    assert [column["key"] for column in empty["columns"]] == [
        column["key"] for column in data["columns"]]
    assert empty["labels"] == data["labels"]


@pytest.mark.parametrize("endpoint", ["builtin_board", "boards/view"])
def test_exact_multiselect_or_within_fields_and_and_between_fields(seeded, view_client, endpoint):
    _task(seeded, "alice-rd", ["R&D", "owner: Alice", "status: 待处理"])
    _task(seeded, "bob-stable", ["v1 (stable)", "owner: Bob", "status: 待处理"])
    _task(seeded, "unset-rd", ["R&D", "status: 待处理"])
    _task(seeded, "carol-rd", ["R&D", "owner: Carol", "status: 待处理"])
    _task(seeded, "alice-other", ["other", "owner: Alice", "status: 待处理"])
    _task(seeded, "split-rd", ["R", "D", "owner: Alice", "status: 待处理"])
    url = f"/api/projects/webshop/{endpoint}/data"
    params = [("view_group_by", "owner"), ("filter_label", "R&D"),
              ("filter_label", "v1 (stable)"), ("filter_label", " OWNER : alice "),
              ("filter_label", "owner: Bob")]
    first = view_client.get(url, params=params).json()
    assert _cards(first) == {"alice-rd", "bob-stable"}
    assert first["matched_count"] == 2

    with_missing = view_client.get(url, params=[
        *params, ("missing_property", "OWNER")]).json()
    assert _cards(with_missing) == {"alice-rd", "bob-stable", "unset-rd"}
    # 高级表达式及文字搜索继续与多选条件取交集。
    narrowed = view_client.get(url, params=[
        *params, ("missing_property", "owner"), ("filter_query", "!owner: Bob"),
        ("search", "R&D")]).json()
    assert _cards(narrowed) == {"alice-rd", "unset-rd"}
    missing_only = view_client.get(url, params={
        "view_group_by": "owner", "missing_property": "owner"}).json()
    assert _cards(missing_only) == {"unset-rd"}


def test_multivalued_cards_count_once_and_property_keys_remain_stable(seeded, view_client):
    _task(seeded, "shared", ["owner: Bob", "owner: Carol", "bug"])
    _task(seeded, "unicode", ["owner: Straße", "R&D"])
    _task(seeded, "special", ["owner: R&D", "owner: v1 (stable)", "owner: *"])
    url = "/api/projects/webshop/boards/view/data"
    data = view_client.get(url, params={"view_group_by": "owner"}).json()
    keys = {column["title"]: column["key"] for column in data["columns"]}
    assert data["matched_count"] == 3
    assert sum(len(column["cards"]) for column in data["columns"]) == 6
    assert _columns(data)["R&D"] == _columns(data)["*"] == {"special"}
    assert [column["query"] for column in data["columns"]
            if column["title"] in {"R&D", "v1 (stable)", "*"}] == ["", "", ""]
    assert next(column["query"] for column in data["columns"]
                if column["title"] == "Bob") == "owner: Bob"

    # 新属性值插入到已有列前面,原列身份保持稳定;Unicode 大小写折叠用于精确筛选。
    _task(seeded, "new-owner", ["owner: Alice", "bug"])
    after = view_client.get(url, params={"view_group_by": "owner"}).json()
    assert all(column["key"] == keys[column["title"]] for column in after["columns"]
               if column["title"] in keys)
    folded = view_client.get(url, params={
        "view_group_by": "owner", "filter_label": "owner: STRASSE"}).json()
    assert _cards(folded) == {"unicode"}
    literal = view_client.get(url, params={
        "view_group_by": "owner", "filter_label": "owner: *"}).json()
    assert _cards(literal) == {"special"}


@pytest.mark.parametrize("search", [
    "task-xyz", "GH-1042", "CHECKOUT", "简介中文", "LABELneedle", "METAneedle", "STRASSE",
])
def test_search_finds_card_fields_and_external_id(seeded, view_client, search):
    task = _task(seeded, "task-xyz", ["labelNeedle", "status: 待处理"],
                 title="Checkout Straße", summary="修复简介中文",
                 external_id="gh-1042", meta=["metaNeedle"], body="secret-body-needle")
    url = "/api/projects/webshop/boards/view/data"
    response = view_client.get(url, params={"view_group_by": "status", "search": search})
    assert response.status_code == 200, response.text
    data = response.json()
    assert _cards(data) == {task.id}
    card = next(card for column in data["columns"] for card in column["cards"])
    assert card["external_id"] == "gh-1042" and card["created_at"] == task.created_at
    assert "body" not in card
    body_match = view_client.get(url, params={
        "view_group_by": "status", "search": "secret-body-needle"}).json()
    assert body_match["matched_count"] == 0


@pytest.mark.parametrize("source", ["built-in", "external"])
def test_view_scope_and_source_are_isolated(seeded, view_client, source):
    seeded.put_board_datasource(BoardDataSource(id="webshop:external", project_id="webshop"))
    board = seeded.get_board("webshop:view")
    board.source = source
    seeded.put_board(board)
    _task(seeded, "active", ["bug", "owner: Active"], source_id=source)
    _task(seeded, "archived", ["bug", "owner: Archived"], source_id=source, archived=True)
    other_source = "external" if source == "built-in" else "built-in"
    _task(seeded, "other-source", ["bug", "owner: Other"], source_id=other_source)
    _task(seeded, "other-project", ["bug", "owner: Other"],
          source_id=source, project_id="elsewhere")
    url = "/api/projects/webshop/boards/view/data"
    for scope, expected in [("active", {"active"}), ("archived", {"archived"}),
                            ("all", {"active", "archived"})]:
        data = view_client.get(url, params={
            "view_group_by": "owner", "scope": scope, "filter_label": "bug"}).json()
        assert _cards(data) == expected
        assert data["counts"] == {"all": 2, "active": 1, "archived": 1}
        assert data["matched_count"] == len(expected)
        assert "owner: Other" not in data["labels"]
        assert ("owner: Archived" in data["labels"]) == (scope != "active")
        assert ("owner: Active" in data["labels"]) == (scope != "archived")


def test_expression_view_keys_follow_queries_and_duplicate_cards_count_once(seeded):
    _task(seeded, "both", ["bug", "urgent"])
    _task(seeded, "outside", ["feature"])
    filters = [{"query": "bug"}, {"query": "urgent"}]
    data = board_sources.resolve_board_data(
        seeded, "webshop", "built-in", filters, view_group_by="")
    assert data["matched_count"] == 2
    assert sum(len(column["cards"]) for column in data["columns"]) == 2
    filtered = board_sources.resolve_board_data(
        seeded, "webshop", "built-in", filters, view_group_by="", filter_query="bug")
    assert filtered["matched_count"] == 1
    assert sum(len(column["cards"]) for column in filtered["columns"]) == 2
    keys = {column["query"]: column["key"] for column in data["columns"]}
    reordered = board_sources.resolve_board_data(
        seeded, "webshop", "built-in", list(reversed(filters)), view_group_by="")
    assert {column["query"]: column["key"] for column in reordered["columns"]} == keys


@pytest.mark.parametrize("endpoint", ["builtin_board", "boards/view"])
@pytest.mark.parametrize("params", [
    {"view_group_by": "owner:value"},
    {"view_group_by": "owner & status"},
    {"view_group_by": "x" * (board_sources.MAX_BOARD_VIEW_PROPERTY_LENGTH + 1)},
    {"filter_query": "(bug"},
    {"filter_query": "x" * (board_sources.MAX_BOARD_VIEW_QUERY_LENGTH + 1)},
    {"filter_query": "!" * 201 + "bug"},
    {"search": "x" * (board_sources.MAX_BOARD_VIEW_SEARCH_LENGTH + 1)},
    {"filter_label": ["bug"] * (board_sources.MAX_BOARD_VIEW_FILTERS + 1)},
    {"filter_label": "x" * (board_sources.MAX_BOARD_VIEW_LABEL_LENGTH + 1)},
    {"filter_label": " "},
    {"missing_property": "owner: value"},
    {"missing_property": ""},
])
def test_invalid_or_oversized_view_options_return_400(view_client, endpoint, params):
    response = view_client.get(f"/api/projects/webshop/{endpoint}/data", params=params)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]
