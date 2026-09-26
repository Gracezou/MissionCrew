/* ---- 准则 / Skill / 自动化跨项目复制 ---- */

const PROJECT_IMPORT_TYPES = {
  guideline: { label: "准则", plural: "准则", idLabel: "name" },
  skill: { label: "Skill", plural: "Skills", idLabel: "id" },
  automation: { label: "自动化", plural: "自动化", idLabel: "id" },
};

function projectImportCandidates(resourceType) {
  if (resourceType === "guideline") return (projObj()?.guidelines || []).map(item => ({
    id: item.name, label: item.name, description: item.description || "",
  }));
  if (resourceType === "skill") return (projObj()?.skills || []).map(item => ({
    id: item.id, label: item.name || item.id, description: item.description || "",
  }));
  if (resourceType === "automation") return projAutomations().map(item => ({
    id: automationShortId(item), label: item.name || automationShortId(item),
    description: item.description || "",
  }));
  return [];
}

// 页头入口常驻：默认勾选当前页面选中的条目，未选中时勾选第一条。
function projectImportSelectedId(resourceType) {
  if (resourceType === "guideline") return selectedGuidelineName || "";
  if (resourceType === "skill") return selectedSkillId || "";
  if (resourceType === "automation")
    return (selectedAutomationId || "").replace(`${currentProject}:`, "");
  return "";
}

function syncProjectImportWarning() {
  const warning = document.getElementById("project-import-overwrite-warning");
  if (warning) warning.hidden =
    document.getElementById("project-import-strategy")?.value !== "overwrite";
}

function toggleProjectImportItems(checked) {
  document.querySelectorAll("#project-import-items input[type=checkbox]")
    .forEach(input => { input.checked = checked; });
}

function openProjectImportDialog(resourceType, preferredId = projectImportSelectedId(resourceType)) {
  const meta = PROJECT_IMPORT_TYPES[resourceType];
  if (!meta) return;
  const targets = (overview.projects || []).filter(project => project.id !== currentProject);
  if (!targets.length) {
    uiAlert("请先创建另一个项目，再复制资源。", `复制${meta.plural}`);
    return;
  }
  const candidates = projectImportCandidates(resourceType);
  if (!candidates.length) {
    uiAlert(`当前项目没有可复制的${meta.plural}。`, `复制${meta.plural}`);
    return;
  }
  const sourceName = projObj()?.name || currentProject;
  const targetOptions = targets.map(project =>
    `<option value="${esc(project.id)}">${esc(project.name || project.id)} (${esc(project.id)})</option>`
  ).join("");
  const checkedId = candidates.some(item => item.id === preferredId)
    ? preferredId : candidates[0].id;
  const items = candidates.map(item => {
    const checked = item.id === checkedId;
    return `<label class="project-import-item">
      <input type="checkbox" value="${esc(item.id)}" ${checked ? "checked" : ""}>
      <span><strong>${esc(item.label)}</strong>
        <code>${esc(meta.idLabel)}: ${esc(item.id)}</code>
        ${item.description ? `<small>${esc(item.description)}</small>` : ""}</span>
    </label>`;
  }).join("");
  openFormDialog(`复制${meta.plural}到其他项目`, `
    <p class="muted project-import-source">源项目：<strong>${esc(sourceName)}</strong>
      <span>仅复制已保存的当前版本。</span></p>
    <label>目标项目</label><select id="project-import-target">${targetOptions}</select>
    <label>冲突策略</label><select id="project-import-strategy" onchange="syncProjectImportWarning()">
      <option value="skip">跳过同名条目（默认）</option>
      <option value="overwrite">覆盖同名条目</option>
    </select>
    <div class="project-import-items-head"><label>选择${meta.plural}</label>
      <span><button class="ghost compact" type="button"
        onclick="toggleProjectImportItems(true)">全选</button>
      <button class="ghost compact" type="button"
        onclick="toggleProjectImportItems(false)">清空</button></span></div>
    <div class="project-import-items" id="project-import-items">${items}</div>
    ${resourceType === "automation" ? `<p class="muted">复制后的自动化一律停用，运行状态与创建角色会被清空。</p>
      <p class="project-import-warning" id="project-import-overwrite-warning" hidden>覆盖会丢弃目标项目原有脚本，且不进回收站；目标自动化正在运行时该条会失败。</p>` : ""}
  `, `<button class="action" type="button" data-type="${esc(resourceType)}"
      onclick="runProjectImport(this)">复制</button>
    <button class="ghost" type="button" onclick="fdlg.close()">取消</button>`);
}

function projectImportResultHtml(result) {
  const labels = {
    copied: "已复制", skipped: "已跳过", overwritten: "已覆盖", failed: "失败",
  };
  return `<div class="project-import-results">
    <p>复制结果：${result.results.length} 个条目</p>
    <ul>${result.results.map(item => `<li class="project-import-${esc(item.status)}">
      <code>${esc(item.id || "(空标识)")}</code>
      <strong>${esc(labels[item.status] || item.status)}</strong>
      ${item.reason ? `<span>${esc(item.reason)}</span>` : ""}
      ${item.target_id ? `<small>目标标识：${esc(item.target_id)}</small>` : ""}
    </li>`).join("")}</ul>
  </div>`;
}

async function runProjectImport(button) {
  const resourceType = button.dataset.type;
  const target = document.getElementById("project-import-target")?.value;
  const strategy = document.getElementById("project-import-strategy")?.value || "skip";
  const itemIds = [...document.querySelectorAll(
    "#project-import-items input[type=checkbox]:checked")].map(input => input.value);
  if (!target) { uiAlert("请选择目标项目"); return; }
  if (!itemIds.length) { uiAlert("请至少选择一个条目"); return; }
  const sourceProject = currentProject;
  // 请求期间禁用：覆盖模式下连点会给同一条目连写两个版本。成功后操作区整体
  // 换成"完成"，只有请求失败才恢复按钮供重试。
  button.disabled = true;
  let result;
  try {
    result = await api("POST", `/api/projects/${encodeURIComponent(target)}/import`, {
      source_project_id: sourceProject,
      resource_type: resourceType,
      item_ids: itemIds,
      conflict_strategy: strategy,
    });
  } catch (error) {
    button.disabled = false;
    throw error;
  }
  await loadOverview();
  document.getElementById("fdlg-body").innerHTML = projectImportResultHtml(result);
  document.getElementById("fdlg-actions").innerHTML =
    `<button class="action" type="button" onclick="fdlg.close()">完成</button>`;
  const failed = result.results.filter(item => item.status === "failed").length;
  toast(failed ? `复制完成，${failed} 项失败` : "复制完成", failed ? "error" : "success");
}
