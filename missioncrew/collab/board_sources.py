"""任务看板数据源:所有卡片都来自统一的 tasks 表,按 source_id 圈定范围。

数据源分两类:内置源 `built-in`(平台原生任务)与自定义源(BoardDataSource,
由主控 board_source.save 创建)。两类源的任务操作对等:task.create 带 source
建进指定源,改、删、归档都按任务 id。自定义源另可由 board_source.save 按
external_id 整体同步卡片(适合定时同步 GitCode/GitHub Issue 这类外部列表),
整体同步只管理同步来的任务,不碰 task.create 建的任务。状态就是
`status: 文本` 标签,数据源只声明状态取值的顺序与颜色,用于生成默认筛选列。

看板由 filters([{title,query,color}],每列一个标签表达式)分列;
group_by 非空时改为按该属性的取值横向分列(每个取值一列,末尾追加「未设置」列),
此时各筛选列的并集只用来划定进入看板的卡片范围。
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


def describe_sources(store=None, project_id: Optional[str] = None) -> list[dict]:
    """创建/编辑看板时展示的可选数据源清单(内置 + 本项目自定义)。"""
    items = [{"id": BUILTIN_SOURCE_ID, "name": "项目任务",
              "description": "本项目的平台原生 Task",
              "status_values": [dict(c) for c in BUILTIN_STATUS_VALUES],
              "custom": False}]
    if store is not None and project_id:
        for record in store.list_board_datasources(project_id):
            short_id = record.id.removeprefix(f"{project_id}:")
            items.append({
                "id": short_id,
                "name": record.name or record.id,
                "description": record.description,
                "status_values": [dict(c) for c in record.status_values],
                "custom": True,
                "cards": len(store.list_tasks(project_id, source_id=short_id)),
                "updated_at": record.updated_at,
            })
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
            card["url"] = str(given["url"]).strip()
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
            if not isinstance(given["meta"], list):
                raise ValueError(f"卡片 {card_id} 的 meta 必须是数组")
            card["meta"] = [str(x) for x in given["meta"]]
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
    未出现的 external_id 任务连带简报删除。没有 external_id 的任务(task.create
    建进本源的)不参与匹配,也不会被删除。
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


def _property_columns(tasks: list[Task], prop: str,
                      status_values: list[dict]) -> list[dict]:
    """按属性取值动态分列;status 属性按数据源声明的顺序与颜色排前,
    其余取值按出现的值排序,末尾固定一列收纳缺少该属性的卡片。"""
    prop = str(prop or "").strip()
    declared = ([{"value": c["value"], "color": c.get("color") or ""}
                 for c in status_values]
                if prop.lower() == STATUS_PROPERTY else [])
    seen = {item["value"].lower() for item in declared}
    extras = []
    for task in tasks:
        for label in task.labels:
            label_prop, value = label_query.split_label(label)
            if label_prop.lower() == prop.lower() and value.lower() not in seen:
                seen.add(value.lower())
                extras.append({"value": value, "color": ""})
    extras.sort(key=lambda item: item["value"])
    columns = [{"title": item["value"], "query": f"{prop}: {item['value']}",
                "color": item["color"]} for item in declared + extras]
    columns.append({"title": f"未设置 {prop}", "query": f"!{prop}: *",
                    "color": ""})
    return columns


def resolve_board_data(store, project_id: str, source_id: str,
                       filters: Optional[list] = None,
                       group_by: str = "", scope: str = "active") -> dict:
    """解析看板数据:按源取任务 -> 按筛选列分列,或按 group_by 属性取值分列。

    scope 决定显示范围(活跃/已归档/全部),counts 始终按该源全量统计。
    卡片可命中多列(列是标签视角,不是互斥状态);表达式非法抛 ValueError。
    响应里的 filters 始终是筛选列配置,分组模式下前端据此增删范围列。
    """
    if scope not in BOARD_SCOPES:
        raise ValueError("scope 只能是 active/archived/all")
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

    group_by = str(group_by or "").strip()
    column_filters = (validate_filters(filters) if filters
                      else default_filters(status_values))
    filter_asts = [label_query.parse(item["query"]) for item in column_filters]
    if group_by:
        # 分组模式:筛选列的并集划定卡片范围,列改为该属性的各个取值。
        scoped = [task for task in tasks
                  if any(label_query.matches(ast, task.labels)
                         for ast in filter_asts)]
        columns = _property_columns(scoped, group_by, status_values)
        key_prefix = "g"
    else:
        scoped, columns, key_prefix = tasks, column_filters, "f"
    out_columns = []
    for index, item in enumerate(columns):
        ast = label_query.parse(item["query"])
        out_columns.append({
            "key": f"{key_prefix}{index}", "title": item["title"],
            "query": item["query"], "color": item["color"] or "var(--muted)",
            "cards": [_task_card(task) for task in scoped
                      if label_query.matches(ast, task.labels)],
        })
    # 可筛选标签全集(整个数据源,不受分组范围影响),供前端做筛选输入建议
    labels = {label for task in tasks for label in task.labels}
    labels.update(f"{STATUS_PROPERTY}: {c['value']}" for c in status_values)
    labels.discard("")
    return {"source": source_info,
            "columns": out_columns,
            "labels": sorted(labels),
            "filters": column_filters,
            "group_by": group_by,
            "scope": scope,
            "counts": counts}
