/** 提示词管理视图：全局模板的列表 / 编辑 / 另存 / 删除。 */

import { api } from "./api.js";
import { log } from "./log.js";
import { state } from "./state.js";
import { $, $$, escapeHtml, guard } from "./util.js";
import { refreshTemplates } from "./view_tasks.js";

export function initPrompts() {
  $("#btn-tpl-new").onclick = () => {
    const name = prompt("新模板文件名（.md）：", "custom_prompt.new.md");
    if (!name) return;
    $("#tpl-content").value = "";
    state.tplCurrent = name.endsWith(".md") ? name : name + ".md";
    state.tplInitialized = true;
    state.tplDirty = false;
    $("#tpl-name").value = state.tplCurrent;
    $("#tpl-title").textContent = state.tplCurrent;
    log("已切换到新模板，编辑后点「保存模板」", "info");
  };

  $("#tpl-content").addEventListener("input", () => { state.tplDirty = true; });

  $("#btn-tpl-save").onclick = () => guard(async () => {
    const name = $("#tpl-name").value.trim();
    if (!name) { alert("请填写模板文件名"); return; }
    const r = await api.saveTemplate(name, $("#tpl-content").value);
    state.tplCurrent = r.name;
    state.tplDirty = false;
    log(`模板已保存：${r.name}`, "ok");
    await refreshTemplateList();
  }, "保存模板");

  $("#btn-tpl-delete").onclick = () => guard(async () => {
    const name = state.tplCurrent;
    if (!confirm(`确认删除模板 ${name}？`)) return;
    await api.deleteTemplate(name);
    log(`模板已删除：${name}`, "ok");
    await refreshTemplateList();
  }, "删除模板");
}

export async function refreshTemplateList() {
  await refreshTemplates();
  renderTemplateList();
}

function renderTemplateList() {
  const ul = $("#tpllist");
  ul.innerHTML = "";
  state.templates.forEach((t) => {
    const li = document.createElement("li");
    li.innerHTML = `<div class="jname">${escapeHtml(t)}</div>
      <div class="jmeta">${t.startsWith("reflect") ? "反思翻译主提示词" : "自定义样式 / 术语"}</div>`;
    if (t === state.tplCurrent) li.classList.add("active");
    li.onclick = () => guard(() => openTemplate(t), "打开模板");
    ul.appendChild(li);
  });

  // 首次进入、或当前模板已被删掉且没有未保存的编辑 -> 自动打开一个
  if (!state.tplInitialized || (!state.templates.includes(state.tplCurrent) && !state.tplDirty)) {
    state.tplInitialized = true;
    state.tplCurrent = state.templates.includes(state.tplCurrent)
      ? state.tplCurrent : (state.templates[0] || "reflect.md");
    guard(() => openTemplate(state.tplCurrent), "打开模板");
  }
  $("#tpl-title").textContent = state.tplCurrent;
}

async function openTemplate(name) {
  state.tplCurrent = name;
  $("#tpl-name").value = name;
  $("#tpl-title").textContent = name;
  try {
    const r = await api.readTemplate(name);
    $("#tpl-content").value = r.content || "";
  } catch (e) {
    $("#tpl-content").value = "";
    log("读取模板失败：" + e.message, "error");
  }
  state.tplDirty = false;
  Array.from($("#tpllist").children).forEach((li) =>
    li.classList.toggle("active", li.textContent.startsWith(name)));
}
