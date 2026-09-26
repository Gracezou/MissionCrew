/* ---------------- 新建项目(侧栏 + 号) ---------------- */
function openNewProject() {
  document.getElementById("np-id").value = "";
  document.getElementById("np-name").value = "";
  document.getElementById("np-desc").value = "";
  const templates = globalRoleTemplates();
  const defaultOrchestrator = defaultOrchestratorTemplate();
  document.getElementById("np-role-summary").textContent = templates.length
    ? (defaultOrchestrator
      ? `将复制 ${templates.length} 个全局角色模板，默认主控为 @${defaultOrchestrator.id}；并创建 general 频道。`
      : `将复制 ${templates.length} 个全局角色模板，但没有可作主控的模板（需既默认启用又不是仅人工点名），无法创建项目。`)
    : "当前没有全局角色模板，请先到全局设置中配置。";
  // 没有可作主控的模板时必定被服务端拒绝，直接置灰创建按钮；对话框仍可打开，
  // 让用户看到原因，而不是点一次才吃到 400。
  document.getElementById("np-create").disabled = !defaultOrchestrator;
  pdlg.showModal();
}

async function createProject() {
  const id = document.getElementById("np-id").value.trim();
  if (!id) { uiAlert("项目 id 不能为空"); return; }
  await api("POST", "/api/projects", {
    id,
    name: document.getElementById("np-name").value.trim() || id,
    description: document.getElementById("np-desc").value.trim(),
  });
  pdlg.close();
  await loadOverview();
  if (await setProject(id) === false) return;
  switchTab("proj");   // 引导补充章程与准则
  toast("项目已创建,请补充章程与准则", "success");
}
