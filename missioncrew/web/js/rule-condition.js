/* 自动处理规则的条件模型:标签表达式与可视化条件行互转的纯数据函数,不依赖 DOM。
   语义与 core/label_query.py 一致。可视化只表达一层「全部/任一」组合(每行可取反);
   括号嵌套或与/或混用的表达式没有对应的行结构,只能在表达式模式编辑。 */
const RULE_OPERATOR_CHARS = "&|!()";

function ruleSplitLabel(text) {
  const raw = String(text ?? "").trim();
  const index = raw.indexOf(":");
  if (index > 0) {
    const prop = raw.slice(0, index).trim(), value = raw.slice(index + 1).trim();
    if (prop && value) return [prop, value];
  }
  return ["", raw];
}

function ruleNormalizeLabel(text) {
  const [prop, value] = ruleSplitLabel(text);
  return prop ? `${prop}: ${value}` : value;
}

// AST:{type:"label",text} | {type:"has",prop} | {type:"not",node}
//     | {type:"and"|"or",items}(同类连写展平,便于转成条件行)
function parseRuleCondition(expr) {
  const source = String(expr ?? "");
  const tokens = [];
  let buffer = "";
  const flush = () => {
    const text = buffer.trim();
    if (text) tokens.push({ text });
    buffer = "";
  };
  for (let i = 0; i < source.length; i++) {
    const ch = source[i];
    if (RULE_OPERATOR_CHARS.includes(ch)) {
      flush();
      if ((ch === "&" || ch === "|") && source[i + 1] === ch) i++;
      tokens.push(ch);
    } else buffer += ch;
  }
  flush();
  if (!tokens.length) throw new Error("条件不能为空");
  let pos = 0;
  const peek = () => tokens[pos];
  const chain = (type, op, next) => {
    const items = [next()];
    while (peek() === op) { pos++; items.push(next()); }
    if (items.length === 1) return items[0];
    return { type, items: items.flatMap(item => item.type === type ? item.items : [item]) };
  };
  const parseOr = () => chain("or", "|", parseAnd);
  const parseAnd = () => chain("and", "&", parseNot);
  const parseNot = () => {
    const token = peek();
    if (token === "!") { pos++; return { type: "not", node: parseNot() }; }
    if (token === "(") {
      pos++;
      const node = parseOr();
      if (peek() !== ")") throw new Error("缺少右括号");
      pos++;
      return node;
    }
    if (!token) throw new Error("表达式意外结束");
    if (typeof token === "string") throw new Error(`运算符 ${token} 后缺少标签`);
    pos++;
    const [prop, value] = ruleSplitLabel(token.text);
    return prop && value === "*" ? { type: "has", prop }
      : { type: "label", text: ruleNormalizeLabel(token.text) };
  };
  const node = parseOr();
  if (pos !== tokens.length) {
    const extra = tokens[pos];
    throw new Error(`多余的内容：${typeof extra === "string" ? extra : extra.text}`);
  }
  return node;
}

// 规范写法:标签归一化、运算符两侧留空格、只在优先级需要处加括号。
function formatRuleCondition(node) {
  const atom = item => item.type === "label" || item.type === "has" || item.type === "not";
  switch (node.type) {
    case "label": return node.text;
    case "has": return `${node.prop}: *`;
    case "not": return "!" + (atom(node.node) ? formatRuleCondition(node.node)
      : `(${formatRuleCondition(node.node)})`);
    case "and": return node.items.map(item => item.type === "or"
      ? `(${formatRuleCondition(item)})` : formatRuleCondition(item)).join(" & ");
    default: return node.items.map(formatRuleCondition).join(" | ");
  }
}

// 规则与看板列按条件对应;比较规范写法,不受空格、&& 等书写差异影响。
function canonicalRuleQuery(expr) {
  try { return formatRuleCondition(parseRuleCondition(expr)); }
  catch (_) { return String(expr ?? "").trim(); }
}

function sameRuleQuery(left, right) {
  return canonicalRuleQuery(left).toLowerCase() === canonicalRuleQuery(right).toLowerCase();
}

/* 条件行:{field, op, value}。field 为空表示普通标签(op: is=包含 / not=不包含);
   field 为属性名时 op 为 is / not / set(已设置)/ unset(未设置)。 */
function ruleRowFromNode(node) {
  const negated = node.type === "not";
  const inner = negated ? node.node : node;
  if (inner.type === "has") return { field: inner.prop, op: negated ? "unset" : "set", value: "" };
  if (inner.type !== "label") return null;
  const [prop, value] = ruleSplitLabel(inner.text);
  return { field: prop, op: negated ? "not" : "is", value: prop ? value : inner.text };
}

function ruleConditionFromQuery(expr) {
  if (!String(expr ?? "").trim()) return { join: "all", rows: [] };
  const node = parseRuleCondition(expr);
  const join = node.type === "or" ? "any" : "all";
  const items = node.type === "and" || node.type === "or" ? node.items : [node];
  const rows = items.map(ruleRowFromNode);
  return rows.every(Boolean) ? { join, rows } : null;
}

function ruleRowError(row) {
  const field = String(row?.field ?? "").trim(), value = String(row?.value ?? "").trim();
  const hasOperator = text => [...RULE_OPERATOR_CHARS].some(ch => text.includes(ch));
  if (field && (field.includes(":") || hasOperator(field)))
    return "属性名不能包含冒号或 & | ! ( )";
  if (field.length > 256) return "属性名最多 256 个字符";
  if (row?.op === "set" || row?.op === "unset") return field ? "" : "请选择属性";
  if (!value) return "请填写取值";
  if (hasOperator(value)) return "取值含 & | ! ( ) 时无法写成条件，请改用表达式";
  if (value === "*") return "取值不能是 *，判断是否设置请选「已设置」";
  return "";
}

function ruleRowTerm(row) {
  const field = String(row.field ?? "").trim(), value = String(row.value ?? "").trim();
  if (row.op === "set") return `${field}: *`;
  if (row.op === "unset") return `!${field}: *`;
  const label = ruleNormalizeLabel(field ? `${field}: ${value}` : value);
  return row.op === "not" ? `!${label}` : label;
}

// 返回 {query, errors}:errors 与 rows 一一对应,有错的行不进入 query。
function ruleConditionToQuery(model) {
  const rows = Array.isArray(model?.rows) ? model.rows : [];
  const errors = rows.map(ruleRowError);
  const terms = rows.filter((_, index) => !errors[index]).map(ruleRowTerm);
  return { query: terms.join(model?.join === "any" ? " | " : " & "), errors };
}

function ruleFieldTitle(field) {
  const text = String(field ?? "").trim();
  return !text ? "标签" : text.toLowerCase() === "status" ? "状态" : text;
}

function describeRuleRow(row) {
  const field = ruleFieldTitle(row.field), value = String(row.value ?? "").trim();
  // 英文属性名与中文之间留空格,中文属性名(如「状态」)紧排
  const before = /^[\x21-\x7e]/.test(field) ? " " : "";
  const after = /[\x21-\x7e]$/.test(field) ? " " : "";
  if (row.op === "set") return `已设置${before}${field}`;
  if (row.op === "unset") return `未设置${before}${field}`;
  if (!String(row.field ?? "").trim())
    return `${row.op === "not" ? "不带" : "带"}标签「${value}」`;
  return `${field}${after}${row.op === "not" ? "不是" : "是"}「${value}」`;
}

// 可视化可表达的条件给出中文描述,否则返回 null(调用方改显示原表达式)。
function describeRuleCondition(expr) {
  try {
    const model = ruleConditionFromQuery(expr);
    if (!model?.rows.length) return null;
    const glue = model.join === "any" ? "，或" : "，且";
    return model.rows.map(describeRuleRow).reduce((text, part) =>
      text + glue + (/^[\x21-\x7e]/.test(part) ? " " : "") + part);
  } catch (_) { return null; }
}

// 由标签全集整理条件编辑的候选:属性(状态排首位)及各自取值、普通标签。
function ruleConditionChoices(labels) {
  const props = new Map([["status", { field: "status", values: [] }]]);
  const plain = [], seenPlain = new Set();
  for (const label of Array.isArray(labels) ? labels : []) {
    if (typeof label !== "string") continue;
    const [prop, value] = ruleSplitLabel(label);
    if (!prop) {
      if (value && !seenPlain.has(value.toLowerCase())) {
        seenPlain.add(value.toLowerCase()); plain.push(value);
      }
      continue;
    }
    const key = prop.toLowerCase();
    if (!props.has(key)) props.set(key, { field: prop, values: [] });
    const entry = props.get(key);
    if (!entry.values.some(item => item.toLowerCase() === value.toLowerCase()))
      entry.values.push(value);
  }
  const collate = (a, b) => a.localeCompare(b, "zh-CN", { sensitivity: "base", numeric: true });
  // 状态取值保留传入顺序(调用方先放数据源声明的工作流顺序),其余按字母序
  const [status, ...rest] = props.values();
  rest.sort((a, b) => collate(a.field, b.field));
  return {
    fields: [status, ...rest.map(item => ({ ...item, values: [...item.values].sort(collate) }))],
    plain: plain.sort(collate),
  };
}
