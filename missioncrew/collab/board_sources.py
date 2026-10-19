"""任务看板数据源:所有卡片都来自统一的 tasks 表,按 source_id 圈定范围。

数据源分两类:内置源 `built-in`(平台原生任务)与自定义源(BoardDataSource,
由主控 board_source.save 创建)。两类源的任务操作对等:task.create 带 source
建进指定源,改、删、归档都按任务 id,两类源的任务都可带 url、meta 与
external_id(外部条目的同步键,同源唯一)。自定义源另可由 board_source.save 按
external_id 整体同步卡片(适合定时同步 GitCode/GitHub Issue 这类外部列表),
整体同步只管理带 external_id 的任务,不论它是同步建的还是 task.create 建的。状态就是
`status: 文本` 标签,数据源只声明状态取值的顺序与颜色,用于生成默认筛选列。

看板由 filters([{title,query,color}],每列一个标签表达式)分列;
group_by 非空时改为按该属性的取值横向分列(每个取值一列,末尾追加「未设置」列),
此时各筛选列的并集只用来划定进入看板的卡片范围。
个人视图显式传 view_group_by 时,分列属性独立于旧筛选列范围;临时标签筛选与
文字搜索只裁剪卡片,不改变列、保存的看板配置或自动处理规则。
标签匹配在服务端完成,前端只做通用渲染。
"""
from __future__ import annotations

import time
from typing import Optional

from ..core import label_query
from ..core.models import (BUILTIN_SOURCE_ID, BUILTIN_STATUS_VALUES,
                           LEGACY_STATUS_TEXT, STATUS_PROPERTY, Task, new_id,
                           normalize_labels, normalize_properties,
                           normalize_status_values, status_of, with_property,
                           with_status)

MAX_BOARD_FILTERS = 20
MAX_BOARD_VIEW_FILTERS = 100
MAX_BOARD_VIEW_LABEL_LENGTH = 4096
MAX_BOARD_VIEW_PROPERTY_LENGTH = 256
MAX_BOARD_VIEW_QUERY_LENGTH = 2000
MAX_BOARD_VIEW_SEARCH_LENGTH = 512
MAX_SOURCE_STATUS_VALUES = 12
# 看板显示范围:活跃 / 已归档 / 全部(counts 始终按全量口径统计)
BOARD_SCOPES = ("active", "archived", "all")
MAX_SOURCE_CARDS = 1000
# 同步卡片允许的字段(status 是状态文本;旧脚本发状态列 key 也能映射;
# properties 逐个设置属性,null 清空)
SOURCE_CARD_FIELDS = {"id", "title", "summary", "status", "labels",
                      "properties", "updated_at", "meta", "url"}
# 状态取值不能包含表达式运算符:状态文本要参与标签表达式匹配
_OPERATOR_CHARS = set("&|!()")

# 内置源沿用过的旧 id,读取时归一化
_LEGACY_BUILTIN_IDS = {"tasks", BUILTIN_SOURCE_ID}


def normalize_source_id(source_id: str) -> str:
    text = str(source_id or "").strip()
    return BUILTIN_SOURCE_ID if text in _LEGACY_BUILTIN_IDS or not text else text


def custom_source_full_id(project_id: str, source_id: str) -> str:
    return f"{project_id}:{source_id}"


def get_custom_source(store, project_id: str, source_id: str):
    record = store.get_board_datasource(
        custom_source_full_id(project_id, source_id))
    if record is not None and record.project_id != project_id:
        return None
    return record


def source_exists(store, project_id: str, source_id: str) -> bool:
    source_id = normalize_source_id(source_id)
    return (source_id == BUILTIN_SOURCE_ID
            or get_custom_source(store, project_id, source_id) is not None)


def source_status_values(store, project_id: str, source_id: str) -> list[dict]:
    source_id = normalize_source_id(source_id)
    if source_id == BUILTIN_SOURCE_ID:
        return [dict(c) for c in BUILTIN_STATUS_VALUES]
    record = get_custom_source(store, project_id, source_id)
    if record is None:
        raise ValueError(f"未知数据源: {source_id}")
    return [dict(c) for c in record.status_values]


def require_source(store, project_id: str, source) -> str:
    """新建 Task 的归属数据源:缺省为内置源,自定义源必须属于本项目。"""
    if source is not None and not isinstance(source, str):
        raise ValueError("source 必须是字符串")
    source_id = normalize_source_id(source)
    if not source_exists(store, project_id, source_id):
        raise ValueError(f"数据源不存在: {source_id}")
    return source_id


def default_status(store, project_id: str, source_id: str) -> str:
    """没指定状态的新 Task 落到该源第一个状态取值(内置源即「待处理」)。"""
    values = source_status_values(store, project_id, source_id)
    return str(values[0]["value"]) if values else BUILTIN_STATUS_VALUES[0]["value"]


def declares_status(store, project_id: str, source_id: str, text: str) -> bool:
    """该源是否声明了这个状态取值;数据源已不存在时视为未声明。"""
    try:
        values = source_status_values(store, project_id, source_id)
    except ValueError:
        return False
    return any(str(c.get("value") or "").lower() == text.lower() for c in values)


def clean_url(value) -> str:
    """外部条目链接:去掉首尾空白,空串表示没有(同步卡片与 task.* 同一口径)。"""
    return str(value or "").strip()


def clean_meta(value, owner: str = "") -> list[str]:
    """卡片附注:字符串数组;owner 是报错时的主语(如「卡片 42 的」)。"""
    if not isinstance(value, list):
        raise ValueError(f"{owner}meta 必须是数组")
    return [str(item) for item in value]


def external_twin(store, project_id: str, source_id: str, external_id: str,
                  exclude_task_id: str = "") -> Optional[Task]:
    """同一数据源里已带这个 external_id 的任务。

    整体同步按 external_id 匹配任务,同一源内必须唯一,否则同一条目会有两张
    卡片且其中一张永远不再被同步更新;内置源没有整体同步,但同样用它识别外部条目。
    """
    if not external_id:
        return None
    return next((task for task in store.list_tasks(project_id, source_id=source_id)
                 if task.external_id == external_id
                 and task.id != exclude_task_id), None)


def describe_sources(store=None, project_id: Optional[str] = None,
                     stats: bool = True) -> list[dict]:
    """创建/编辑看板时展示的可选数据源清单(内置 + 本项目自定义)。

    stats=False 时不带任务数与同步时间:项目总览轮询下发这份清单(前端据此取
    各源状态取值与颜色),只随数据源配置变化,不因每次同步而换 ETag。
    """
    items = [{"id": BUILTIN_SOURCE_ID, "name": "项目任务",
              "description": "本项目的平台原生 Task",
              "status_values": [dict(c) for c in BUILTIN_STATUS_VALUES],
              "custom": False}]
    if store is not None and project_id:
        for record in store.list_board_datasources(project_id):
            short_id = record.id.removeprefix(f"{project_id}:")
            item = {
                "id": short_id,
                "name": record.name or record.id,
                "description": record.description,
                "status_values": [dict(c) for c in record.status_values],
                "custom": True,
            }
            if stats:
                item["cards"] = len(store.list_tasks(project_id, source_id=short_id))
                item["updated_at"] = record.updated_at
            items.append(item)
    return items


def default_filters(status_values) -> list[dict]:
    """按数据源状态取值生成默认筛选列:一列一个状态标签表达式。"""
    return [{"title": str(c.get("value") or ""),
             "query": f"{STATUS_PROPERTY}: {c.get('value')}",
             "color": str(c.get("color") or "")}
            for c in status_values if str(c.get("value") or "").strip()]


def validate_group_by(raw) -> str:
    """分列属性名:去空白;含冒号或表达式运算符时抛 ValueError(空串 = 不分列)。"""
    prop = str(raw or "").strip()
    if ":" in prop or set(prop) & _OPERATOR_CHARS:
        raise ValueError("分组属性名不能包含冒号或表达式运算符")
    return prop


def validate_filters(raw) -> list[dict]:
    """校验筛选列定义并归一化;非法抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("filters 必须是数组")
    if len(raw) > MAX_BOARD_FILTERS:
        raise ValueError(f"筛选列最多 {MAX_BOARD_FILTERS} 个")
    filters = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("filters 每项必须是 {title,query,color} 对象")
        unknown = set(item) - {"title", "query", "color"}
        if unknown:
            raise ValueError(f"筛选列含未知字段: {', '.join(sorted(unknown))}")
        query = str(item.get("query") or "").strip()
        try:
            label_query.parse(query)
        except ValueError as exc:
            raise ValueError(f"筛选列表达式不合法: {exc}") from exc
        filters.append({
            "title": str(item.get("title") or "").strip() or query,
            "query": query,
            "color": str(item.get("color") or ""),
        })
    return filters


def _clean_tag_text(value, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{what}不能为空")
    if set(text) & _OPERATOR_CHARS:
        raise ValueError(f"{what}不能包含表达式运算符 & | ! ( ):{text}")
    return text


def validate_source_columns(raw) -> list[dict]:
    """校验数据源状态取值声明;兼容旧状态列 {key,title,color} 写法。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("columns 必须是非空数组")
    if len(raw) > MAX_SOURCE_STATUS_VALUES:
        raise ValueError(f"状态取值最多 {MAX_SOURCE_STATUS_VALUES} 个")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("columns 每项必须是 {value,color}(或旧版 {key,title,color}) 对象")
        unknown = set(item) - {"value", "key", "title", "color"}
        if unknown:
            raise ValueError(f"状态取值含未知字段: {', '.join(sorted(unknown))}")
        _clean_tag_text(item.get("value") or item.get("title")
                        or item.get("key"), "状态取值")
    values = normalize_status_values(raw)
    if not values:
        raise ValueError("columns 必须声明至少一个状态取值")
    return values


def _column_key_map(raw_columns) -> dict[str, str]:
    """旧脚本卡片用状态列 key 表示状态;建 key -> 状态文本 的映射。

    没有显式列声明时,旧内置四态 key(open/in_progress/...)仍按惯例映射。
    """
    mapping = {}
    for item in raw_columns or []:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            text = str(item.get("value") or item.get("title") or key).strip()
            if key and text:
                mapping[key] = text
    return mapping or dict(LEGACY_STATUS_TEXT)


def validate_source_cards(raw) -> list[dict]:
    """校验并归一化同步卡片;非法抛 ValueError。状态文本不做取值校验。

    只保留卡片实际带了的字段(值为 null 等同没带):同步按「带了才改、没带
    保留」合并进已有任务,所以缺省要和空值区分开。id 必填;title 只在新建
    任务时必填,由 sync_source_tasks 检查。
    """
    if not isinstance(raw, list):
        raise ValueError("cards 必须是数组")
    if len(raw) > MAX_SOURCE_CARDS:
        raise ValueError(f"卡片最多 {MAX_SOURCE_CARDS} 张")
    cards, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("cards 每项必须是对象")
        unknown = set(item) - SOURCE_CARD_FIELDS
        if unknown:
            raise ValueError(f"卡片含未知字段: {', '.join(sorted(unknown))}")
        card_id = str(item.get("id") or "").strip()
        if not card_id:
            raise ValueError("每张卡片必须有 id")
        if card_id in seen:
            raise ValueError(f"卡片 id 重复: {card_id}")
        seen.add(card_id)
        given = {key: value for key, value in item.items() if value is not None}
        card = {"id": card_id,
                "updated_at": float(given.get("updated_at") or time.time())}
        if "title" in given:
            card["title"] = str(given["title"]).strip()
            if not card["title"]:
                raise ValueError(f"卡片 {card_id} 的 title 不能为空")
        if "summary" in given:
            card["summary"] = str(given["summary"])
        if "url" in given:
            card["url"] = clean_url(given["url"])
        status = str(given.get("status") or "").strip()
        if status:   # 空状态等同没带
            card["status"] = status
        if "labels" in given:
            labels_raw = given["labels"]
            if (not isinstance(labels_raw, list)
                    or not all(isinstance(x, str) for x in labels_raw)):
                raise ValueError(f"卡片 {card_id} 的 labels 必须是字符串数组")
            card["labels"] = normalize_labels(labels_raw)
        if "meta" in given:
            card["meta"] = clean_meta(given["meta"], f"卡片 {card_id} 的 ")
        if "properties" in item:   # properties 里的 null 是「清空」,单独校验
            card["properties"] = normalize_properties(
                item["properties"], f"卡片 {card_id} 的 properties")
        cards.append(card)
    return cards


def _merge_card_labels(labels: list[str], card: dict,
                       status_text: str) -> list[str]:
    """把卡片带了的标签部分合并进任务标签,没带的保持原样。

    - labels 里的普通标签:整组以卡片为准(外部源删掉的标签才能同步删掉),
      保留的标签留在原位,新增的追加在后;
    - labels 里的 `属性: 值`:只替换同名属性;
    - properties:逐个属性替换,空取值即清空;
    - status:最后写入,优先级最高。
    """
    result = list(labels)
    if "labels" in card:
        plain = [label for label in card["labels"]
                 if not label_query.split_label(label)[0]]
        wanted = {label.lower() for label in plain}
        result = [label for label in result
                  if label_query.split_label(label)[0]
                  or label.lower() in wanted]
        have = {label.lower() for label in result}
        result += [label for label in plain if label.lower() not in have]
        grouped: dict[str, tuple[str, list[str]]] = {}
        for label in card["labels"]:
            prop, value = label_query.split_label(label)
            if prop:
                grouped.setdefault(prop.lower(), (prop, []))[1].append(value)
        for prop, values in grouped.values():
            result = with_property(result, prop, values)
    for prop, values in card.get("properties", {}).items():
        result = with_property(result, prop, values)
    if status_text:
        result = with_status(result, status_text)
    return normalize_labels(result)


def sync_source_tasks(store, project_id: str, source_id: str,
                      cards: list[dict], raw_columns=None,
                      status_values=None, auto_process=None) -> dict:
    """把同步卡片 upsert 成该源的任务(整体同步语义)。

    卡片按 external_id 匹配既有任务:命中则只更新卡片带了的字段与属性,
    没带的保持本地值(本地改过的状态、Agent 加的属性不会被下一次同步冲掉;
    内容没有变化时不写库,不刷新 updated_at);未命中则新建,没带状态时落到
    第一个状态取值,新任务会走项目自动处理规则(auto_process 回调);本次
    未出现的 external_id 任务连带简报删除。用 task.create 建进本源的任务带了
    external_id 就同样参与匹配与删除,没带的不参与匹配,也不会被删除。
    """
    key_text = _column_key_map(raw_columns)
    declared = [str(c.get("value") or "") for c in status_values or []]
    default_status = (next((str(item.get("value") or item.get("title")
                                or item.get("key") or "")
                            for item in raw_columns or []
                            if isinstance(item, dict)), "")
                      or (declared[0] if declared else "待处理"))
    existing = {task.external_id: task
                for task in store.list_tasks(project_id, source_id=source_id)
                if task.external_id}
    # 先整体检查再写库,避免半途报错留下部分同步结果
    untitled = [card["id"] for card in cards
                if card["id"] not in existing and "title" not in card]
    if untitled:
        raise ValueError(f"新卡片必须有 title: {', '.join(untitled[:5])}")
    created, updated = 0, 0
    seen_ids = set()
    for card in cards:
        external_id = card["id"]
        seen_ids.add(external_id)
        status_text = (key_text.get(card["status"], card["status"])
                       if "status" in card else "")
        task = existing.get(external_id)
        if task is None:
            labels = _merge_card_labels([], card, status_text)
            if not status_of(labels):
                labels = with_status(labels, default_status)
            task = Task(
                id=new_id("t"), project_id=project_id, title=card["title"],
                source_id=source_id, external_id=external_id,
                summary=card.get("summary", ""), labels=labels,
                url=card.get("url", ""), meta=card.get("meta", []),
                created_at=card["updated_at"], updated_at=card["updated_at"],
            )
            store.put_task(task)
            created += 1
            if auto_process is not None:
                auto_process(task)
        else:
            before = (task.title, task.summary, task.url, task.meta, task.labels)
            for key in ("title", "summary", "url", "meta"):
                if key in card:
                    setattr(task, key, card[key])
            task.labels = _merge_card_labels(task.labels, card, status_text)
            if (task.title, task.summary, task.url, task.meta,
                    task.labels) != before:
                store.put_task(task)
                updated += 1
    removed = 0
    for external_id, task in existing.items():
        if external_id not in seen_ids:
            store.delete_task(task.id)
            removed += 1
    return {"created": created, "updated": updated, "removed": removed}


def _task_card(task: Task) -> dict:
    card = {
        "id": task.id, "title": task.title, "summary": task.summary,
        "status": status_of(task.labels), "labels": list(task.labels),
        "external_id": task.external_id, "created_at": task.created_at,
        "updated_at": task.updated_at, "meta": list(task.meta),
        "task_id": task.id, "archived": bool(task.archived),
    }
    if task.url:
        card["url"] = task.url
    return card


def _default_meta(store, task: Task) -> list[str]:
    meta = [task.id]
    for channel_id in task.channel_ids:
        channel = store.get_channel(channel_id)
        meta.append(f"#{channel.name or channel.id}" if channel else channel_id)
    return meta


def resolve_taskboard_widget(store, project_id: str, content: dict) -> dict:
    """面板里的 taskboard 组件:取 content.source 数据源(缺省内置源)的活跃任务,
    content.label 按标签表达式匹配,与看板筛选列、自动处理规则同一语法。"""
    label = str(content.get("label") or "").strip()
    if not label:
        return {"error": "缺少 content.label(要聚合的 Task 标签)"}
    try:
        source_id = require_source(store, project_id, content.get("source"))
        ast = label_query.parse(label)
    except ValueError as exc:
        return {"error": str(exc)}
    cards = []
    for task in store.list_tasks(project_id, include_archived=False,
                                 source_id=source_id):
        if label_query.matches(ast, task.labels):
            task.meta = task.meta or _default_meta(store, task)
            cards.append(_task_card(task))
    return {"source": source_id, "cards": cards}


def _property_columns(tasks: list[Task], prop: str,
                      status_values: list[dict], exact: bool = False) -> list[dict]:
    """按属性取值动态分列;status 属性按数据源声明的顺序与颜色排前,
    其余取值按出现的值排序,末尾固定一列收纳缺少该属性的卡片。"""
    prop = str(prop or "").strip()
    normalize = str.casefold if exact else str.lower
    declared = ([{"value": c["value"], "color": c.get("color") or ""}
                 for c in status_values]
                if normalize(prop) == STATUS_PROPERTY else [])
    seen = {normalize(item["value"]) for item in declared}
    extras = []
    for task in tasks:
        for label in task.labels:
            label_prop, value = label_query.split_label(label)
            if normalize(label_prop) == normalize(prop) and normalize(value) not in seen:
                seen.add(normalize(value))
                extras.append({"value": value, "color": ""})
    extras.sort(key=lambda item: item["value"])
    columns = []
    for item in declared + extras:
        value = item["value"]
        query = f"{prop}: {value}"
        # 个人视图按实际属性值匹配;包含运算符或字面 * 的值无法安全复用为
        # 自动处理表达式,query 留空,前端不为这种列提供规则入口。
        if exact and (set(value) & _OPERATOR_CHARS or value == "*"):
            query = ""
        columns.append({"title": value, "query": query,
                        "color": item["color"], "value": value})
    columns.append({"title": f"未设置 {prop}", "query": f"!{prop}: *",
                    "color": "", "value": None})
    return columns


def _view_property(raw) -> str:
    """个人视图属性名:保留空串表示表达式列,限制用户输入长度。"""
    prop = validate_group_by(raw)
    if len(prop) > MAX_BOARD_VIEW_PROPERTY_LENGTH:
        raise ValueError(f"分列属性名最多 {MAX_BOARD_VIEW_PROPERTY_LENGTH} 个字符")
    return prop


def _view_label_groups(filter_labels, missing_properties) -> tuple[dict, set]:
    """多选值同属性取并集,不同属性取交集;普通标签共用一个筛选维度。

    值按标签本身精确匹配,不经过表达式解析,因此 R&D、v1 (stable) 等标签
    不会被误当作运算符。属性的「未设置」与该属性选中的取值取并集。
    """
    raw_labels = filter_labels or []
    raw_missing = missing_properties or []
    if not isinstance(raw_labels, list) or not isinstance(raw_missing, list):
        raise ValueError("标签筛选和未设置属性必须是数组")
    if len(raw_labels) + len(raw_missing) > MAX_BOARD_VIEW_FILTERS:
        raise ValueError(f"筛选条件最多 {MAX_BOARD_VIEW_FILTERS} 个")
    groups: dict[str, set[str]] = {}
    missing = set()
    for raw in raw_labels:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("筛选标签必须是非空字符串")
        if len(raw) > MAX_BOARD_VIEW_LABEL_LENGTH:
            raise ValueError(f"筛选标签最多 {MAX_BOARD_VIEW_LABEL_LENGTH} 个字符")
        label = label_query.normalize_label(raw)
        prop, _ = label_query.split_label(label)
        groups.setdefault(prop.casefold(), set()).add(label.casefold())
    for raw in raw_missing:
        prop = _view_property(raw)
        if not prop:
            raise ValueError("未设置属性名不能为空")
        key = prop.casefold()
        groups.setdefault(key, set())
        missing.add(key)
    return groups, missing


def _matches_view_labels(task: Task, groups: dict, missing: set) -> bool:
    if not groups:
        return True
    labels = {label_query.normalize_label(label).casefold()
              for label in task.labels}
    properties = {label_query.split_label(label)[0].casefold()
                  for label in task.labels if label_query.split_label(label)[0]}
    return all(bool(values & labels) or (prop in missing and prop not in properties)
               for prop, values in groups.items())


def _matches_search(task: Task, search: str) -> bool:
    """只搜索卡片信息,不读取/扫描任务正文;外部编号也可直接搜索。"""
    if not search:
        return True
    fields = [task.id, task.external_id, task.title, task.summary,
              *task.labels, *task.meta]
    return any(search in value.casefold() for value in fields)


def _matches_property_column(task: Task, prop: str, value: Optional[str]) -> bool:
    values = {text.casefold() for name, text in map(label_query.split_label, task.labels)
              if name.casefold() == prop.casefold()}
    return value.casefold() in values if value is not None else not values


def resolve_board_data(store, project_id: str, source_id: str,
                       filters: Optional[list] = None,
                       group_by: str = "", scope: str = "active", *,
                       view_group_by: Optional[str] = None,
                       filter_query: str = "", search: str = "",
                       filter_labels: Optional[list[str]] = None,
                       missing_properties: Optional[list[str]] = None) -> dict:
    """解析看板数据:按源取任务 -> 按筛选列分列,或按 group_by 属性取值分列。

    scope 决定显示范围(活跃/已归档/全部),counts 始终按该源全量统计。
    卡片可命中多列(列是标签视角,不是互斥状态);表达式非法抛 ValueError。
    缺省 view_group_by 时保留原有范围语义;显式传值时作为个人视图的分列
    覆盖(空串为表达式列,非空按整个数据源当前 scope 的属性值分列)。标签与
    文字筛选只裁剪卡片,动态列和标签建议始终基于快筛前的范围。
    matched_count 按通过快筛的任务去重,表达式模式包含未命中任何列的任务。
    """
    if scope not in BOARD_SCOPES:
        raise ValueError("scope 只能是 active/archived/all")
    independent_view = view_group_by is not None
    if independent_view:
        view_group_by = _view_property(view_group_by)
    filter_query = str(filter_query or "").strip()
    search = str(search or "").strip()
    if len(filter_query) > MAX_BOARD_VIEW_QUERY_LENGTH:
        raise ValueError(f"筛选表达式最多 {MAX_BOARD_VIEW_QUERY_LENGTH} 个字符")
    if len(search) > MAX_BOARD_VIEW_SEARCH_LENGTH:
        raise ValueError(f"搜索文字最多 {MAX_BOARD_VIEW_SEARCH_LENGTH} 个字符")
    # 表达式递归解析/匹配,同时限制运算符数量,避免短而极深的表达式越过长度限制。
    if sum(char in _OPERATOR_CHARS for char in filter_query) > 200:
        raise ValueError("筛选表达式过于复杂")
    try:
        view_ast = label_query.parse(filter_query) if filter_query else None
    except ValueError as exc:
        raise ValueError(f"筛选表达式不合法: {exc}") from exc
    groups, missing = _view_label_groups(filter_labels, missing_properties)
    search_term = search.casefold()
    source_id = normalize_source_id(source_id)
    if source_id == BUILTIN_SOURCE_ID:
        source_info = {"id": source_id, "name": "项目任务"}
        status_values = [dict(c) for c in BUILTIN_STATUS_VALUES]
    else:
        record = get_custom_source(store, project_id, source_id)
        if record is None:
            raise ValueError(f"未知数据源: {source_id}")
        source_info = {
            "id": source_id,
            "name": record.name or source_id,
            "updated_at": record.updated_at,
        }
        status_values = [dict(c) for c in record.status_values]
    all_tasks = store.list_tasks(project_id, include_archived=True,
                                 source_id=source_id)
    counts = {"all": len(all_tasks),
              "archived": sum(1 for task in all_tasks if task.archived)}
    counts["active"] = counts["all"] - counts["archived"]
    tasks = (all_tasks if scope == "all" else
             [task for task in all_tasks
              if task.archived == (scope == "archived")])
    # 没有附注的卡片(内置任务、task.create 建进自定义源的任务)显示 id 与绑定频道
    for task in tasks:
        if not task.meta:
            task.meta = _default_meta(store, task)

    group_by = (view_group_by if independent_view
                else str(group_by or "").strip())
    column_filters = (validate_filters(filters) if filters
                      else default_filters(status_values))
    filter_asts = [label_query.parse(item["query"]) for item in column_filters]
    if group_by:
        # 旧分组以表达式列并集划范围;个人分列与旧列解耦,从当前 scope 全集取值。
        scoped = (tasks if independent_view else
                  [task for task in tasks
                   if any(label_query.matches(ast, task.labels)
                          for ast in filter_asts)])
        columns = _property_columns(scoped, group_by, status_values,
                                    exact=independent_view)
        key_prefix = "g"
    else:
        scoped, columns, key_prefix = tasks, column_filters, "f"
    scoped = [task for task in scoped
              if (view_ast is None or label_query.matches(view_ast, task.labels))
              and _matches_view_labels(task, groups, missing)
              and _matches_search(task, search_term)]
    out_columns = []
    seen_keys: dict[str, int] = {}
    for index, item in enumerate(columns):
        if independent_view and group_by:
            value = item["value"]
            # 匹配实际属性值,而非再次解析生成的表达式;带运算符的值也能准确分列。
            cards = [task for task in scoped
                     if _matches_property_column(task, group_by, value)]
            identity = f"{group_by.casefold()}:{value.casefold() if value is not None else ''}"
        else:
            ast = label_query.parse(item["query"])
            cards = [task for task in scoped if label_query.matches(ast, task.labels)]
            identity = item["query"].casefold()
        key = f"{key_prefix}{index}"
        if independent_view:
            # query/value 是稳定身份;重排或新增其他列不会把滚动位置/折叠状态串列。
            # 同表达式允许重复,只在相同身份内编号,不依赖全局列下标。
            occurrence = seen_keys.get(identity, 0)
            seen_keys[identity] = occurrence + 1
            key = f"{key_prefix}:{len(identity)}:{identity}:{occurrence}"
        out_columns.append({
            "key": key, "title": item["title"],
            "query": item["query"], "color": item["color"] or "var(--muted)",
            "cards": [_task_card(task) for task in cards],
        })
    # 当前 scope 的可筛选标签全集,不受分组范围与快筛影响。
    labels = {label for task in tasks for label in task.labels}
    labels.update(f"{STATUS_PROPERTY}: {c['value']}" for c in status_values)
    labels.discard("")
    return {"source": source_info,
            "columns": out_columns,
            "labels": sorted(labels),
            "filters": column_filters,
            "group_by": group_by,
            "scope": scope,
            "counts": counts,
            "matched_count": len({task.id for task in scoped})}
