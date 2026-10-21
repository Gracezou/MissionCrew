/* 任务看板的个人视图:纯数据转换,不依赖 DOM、项目状态或标签表达式解析器。
   精确标签筛选独立传参,避免 R&D、括号等标签被当成高级表达式运算符。 */
const TASKBOARD_VIEW_VERSION = 1;
const TASKBOARD_VIEW_MAX_LENGTH = 256 * 1024;

function taskboardViewProperty(value, allowOperators = false) {
  if (typeof value !== "string" || value.length > 256) return null;
  const field = value.trim();
  if (/[:\u0000-\u001f\u007f]/.test(field)
      || (!allowOperators && /[&|!()]/.test(field))) return null;
  return field;
}

function defaultTaskboardView(groupBy = "status", scope = "active") {
  return {
    version: TASKBOARD_VIEW_VERSION, search: "", facets: [], advanced: "",
    groupBy: taskboardViewProperty(groupBy) ?? "status",
    scope: ["active", "archived", "all"].includes(scope) ? scope : "active",
    sort: "updated_desc", hideEmpty: false, collapsed: [],
  };
}

function taskboardViewObject(raw) {
  try {
    if (typeof raw === "string") {
      if (raw.length > TASKBOARD_VIEW_MAX_LENGTH) return null;
      raw = JSON.parse(raw);
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    if (raw.version !== undefined && raw.version !== TASKBOARD_VIEW_VERSION) return null;
    return raw;
  } catch (_) { return null; }
}

function taskboardViewFacets(raw, fallback) {
  const copy = () => fallback.map(item => ({ ...item, values: [...item.values] }));
  if (!Array.isArray(raw) || raw.length > 100) return copy();
  const groups = new Map();
  let count = 0;
  for (const item of raw) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const field = taskboardViewProperty(item.field, true);
    if (field === null || !Array.isArray(item.values)) continue;
    count += item.values.length + (item.missing === true ? 1 : 0);
    // 超限整组回退,不截断标签文本或悄悄取前几个条件而改变筛选含义。
    if (count > 100) return copy();
    const key = field.toLowerCase();
    let group = groups.get(key);
    if (!group) {
      group = { field, values: [], missing: false, seen: new Set() };
      groups.set(key, group);
    }
    for (const value of item.values) {
      if (typeof value !== "string" || value.length > 2048) continue;
      const text = value.trim(), normalized = text.toLowerCase();
      if (text && !group.seen.has(normalized)) {
        group.values.push(text);
        group.seen.add(normalized);
      }
    }
    // missing_property 与后端的分组属性校验保持一致;纯标签没有“未设置”。
    group.missing ||= item.missing === true && !!field
      && taskboardViewProperty(field) !== null;
  }
  return [...groups.values()].filter(item => item.values.length || item.missing)
    .map(({ field, values, missing }) => ({ field, values, missing }));
}

function normalizeTaskboardView(raw, defaults) {
  const clean = (input, fallback) => {
    const value = taskboardViewObject(input);
    if (!value) return fallback;
    try {
      const text = (key, limit) => typeof value[key] === "string"
        && value[key].length <= limit ? value[key] : fallback[key];
      const collapsed = Array.isArray(value.collapsed) && value.collapsed.length <= 1000
        ? [...new Set(value.collapsed.filter(item => typeof item === "string"
          && item.length > 0 && item.length <= 4096))] : [...fallback.collapsed];
      const result = {
        version: TASKBOARD_VIEW_VERSION,
        search: text("search", 512),
        facets: taskboardViewFacets(value.facets, fallback.facets),
        advanced: text("advanced", 2000),
        groupBy: taskboardViewProperty(value.groupBy) ?? fallback.groupBy,
        scope: ["active", "archived", "all"].includes(value.scope)
          ? value.scope : fallback.scope,
        sort: ["updated_desc", "updated_asc", "system_updated_desc", "created_desc", "title_asc"]
          .includes(value.sort)
          ? value.sort : fallback.sort,
        hideEmpty: typeof value.hideEmpty === "boolean" ? value.hideEmpty : fallback.hideEmpty,
        collapsed,
      };
      return JSON.stringify(result).length <= TASKBOARD_VIEW_MAX_LENGTH ? result : fallback;
    } catch (_) { return fallback; }
  };
  return clean(raw, clean(defaults, defaultTaskboardView()));
}

function taskboardViewStorageKey(project, key, source) {
  // JSON 元组保证含冒号、斜线等字符的项目/面板/数据源之间仍无碰撞。
  return `missioncrew.taskboard-view.v${TASKBOARD_VIEW_VERSION}:`
    + JSON.stringify([String(project ?? ""), String(key ?? ""), String(source ?? "built-in")]);
}

function loadTaskboardView(project, key, source, defaults, storage) {
  const fallback = normalizeTaskboardView(null, defaults);
  try {
    const target = storage === undefined
      ? (typeof localStorage === "undefined" ? null : localStorage) : storage;
    const raw = target?.getItem(taskboardViewStorageKey(project, key, source));
    return normalizeTaskboardView(raw, fallback);
  } catch (_) { return fallback; }
}

function saveTaskboardView(project, key, source, view, storage) {
  try {
    const target = storage === undefined
      ? (typeof localStorage === "undefined" ? null : localStorage) : storage;
    if (!target) return false;
    target.setItem(taskboardViewStorageKey(project, key, source),
      JSON.stringify(normalizeTaskboardView(view)));
    return true;
  } catch (_) { return false; }
}

function taskboardFacetGroups(labels, view) {
  const normalized = normalizeTaskboardView(view);
  const groups = new Map();
  const selections = new Map(normalized.facets.map(item => [item.field.toLowerCase(), item]));
  const ensure = field => {
    const key = field.toLowerCase();
    if (!groups.has(key)) groups.set(key, {
      field, title: key === "status" ? "状态" : (field || "标签"), options: [], seen: new Set(),
    });
    return groups.get(key);
  };
  const add = (field, value) => {
    const group = ensure(field), key = value.toLowerCase();
    if (group.seen.has(key)) return;
    group.seen.add(key);
    group.options.push({ value, label: value, missing: false,
      selected: !!selections.get(field.toLowerCase())?.values
        .some(selected => selected.toLowerCase() === key) });
  };
  for (const label of Array.isArray(labels) ? labels : []) {
    if (typeof label !== "string") continue;
    const raw = label.trim(), index = raw.indexOf(":"), field = raw.slice(0, index).trim();
    const value = index > 0 ? raw.slice(index + 1).trim() : "";
    if (index > 0 && field && value) {
      if (taskboardViewProperty(field, true) !== null) add(field, value);
    } else if (raw) add("", raw);
  }
  // 选择优先于当前清单:任务删除、换显示范围后仍能看到并撤销既有筛选。
  for (const facet of normalized.facets) {
    ensure(facet.field);
    facet.values.forEach(value => add(facet.field, value));
  }
  if (normalized.groupBy) ensure(normalized.groupBy);
  const rank = field => field.toLowerCase() === "status" ? 0 : (field ? 2 : 1);
  return [...groups.values()].sort((a, b) => rank(a.field) - rank(b.field)
    || a.field.localeCompare(b.field, "zh-CN", { sensitivity: "base" }))
    .map(({ field, title, options }) => {
      if (field && taskboardViewProperty(field) !== null) options.push({
        value: "", label: "未设置", missing: true,
        selected: !!selections.get(field.toLowerCase())?.missing,
      });
      return { field, title, options };
    });
}

function taskboardViewParams(view) {
  const normalized = normalizeTaskboardView(view);
  const params = new URLSearchParams({ scope: normalized.scope, view_group_by: normalized.groupBy });
  if (normalized.search.trim()) params.set("search", normalized.search.trim());
  if (normalized.advanced.trim()) params.set("filter_query", normalized.advanced.trim());
  for (const facet of normalized.facets) {
    for (const value of facet.values) {
      params.append("filter_label", facet.field ? `${facet.field}: ${value}` : value);
    }
    if (facet.missing) params.append("missing_property", facet.field);
  }
  return params;
}

// 卡片的「更新时间」:外部系统提供的时间优先,没提供(0)时退回 MissionCrew 系统更新时间
function taskboardCardUpdatedAt(card) {
  const external = Number(card?.external_updated_at);
  return Number.isFinite(external) && external > 0 ? external : card?.updated_at;
}

function taskboardSortedCards(cards, sort) {
  const items = Array.isArray(cards) ? [...cards] : [];
  const timestamp = value => {
    if (typeof value === "string" && !value.trim()) return null;
    const number = typeof value === "number" || typeof value === "string" ? Number(value) : NaN;
    return Number.isFinite(number) ? number : null;
  };
  const time = card => sort === "created_desc" ? card?.created_at
    : sort === "system_updated_desc" ? card?.updated_at : taskboardCardUpdatedAt(card);
  return items.sort((a, b) => {
    let order = 0;
    if (sort === "title_asc") {
      order = String(a?.title ?? "").localeCompare(String(b?.title ?? ""), "zh-CN",
        { sensitivity: "base", numeric: true });
    } else {
      const left = timestamp(time(a)), right = timestamp(time(b));
      // 缺少时间的卡片在两种时间方向下都置后,避免把坏值当成刚刚更新。
      order = left === null ? (right === null ? 0 : 1)
        : (right === null ? -1 : (sort === "updated_asc" ? left - right : right - left));
    }
    const leftId = String(a?.id ?? a?.task_id ?? ""), rightId = String(b?.id ?? b?.task_id ?? "");
    return order || (leftId < rightId ? -1 : (leftId > rightId ? 1 : 0));
  });
}
