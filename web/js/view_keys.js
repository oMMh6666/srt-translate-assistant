/** API Key 视图：行内编辑 / 勾选批量 / 新增行。 */

import { api } from "./api.js";
import { log } from "./log.js";
import { state } from "./state.js";
import { $, $$, escapeHtml, guard } from "./util.js";

const BULK_LABEL = {
  enable: "启用", disable: "禁用", delete: "删除", reset_usage: "重置用量",
};

export function initKeys() {
  $("#btn-key-add").onclick = () => {
    state.keyEditId = null;
    state.keyDraft = true;
    renderKeys();
    const el = $("#draft-key");
    if (el) el.focus();
  };

  $("#key-selall").onchange = () => {
    const on = $("#key-selall").checked;
    state.keyChecked = new Set(on ? state.keys.map((k) => k.id) : []);
    renderKeys();
  };

  $$("[data-bulk]").forEach((b) => {
    b.onclick = () => {
      const action = b.dataset.bulk;
      const confirmText = action === "delete" ? "确认删除选中的 {n} 条 Key？" : "";
      guard(() => bulk(action, null, confirmText), "批量操作");
    };
  });
}

export async function refreshKeys() {
  let data;
  try {
    data = await api.keys();
  } catch {
    return;
  }
  state.keyStats = data.stats || state.keyStats;
  $("#keystat").textContent = `Key：可用 ${data.stats.available} / 共 ${data.stats.total}`;

  // 行内编辑 / 新增行的过程中不重画表格，否则用户输入会被冲掉
  if (state.keyEditId !== null || state.keyDraft) return;

  state.keys = data.keys || [];
  const valid = new Set(state.keys.map((k) => k.id));
  state.keyChecked = new Set([...state.keyChecked].filter((id) => valid.has(id)));
  renderKeys();
}

function renderKeys() {
  const tb = $("#keys-body");
  tb.innerHTML = "";

  state.keys.forEach((k) => tb.appendChild(keyRow(k)));
  if (state.keyDraft) tb.appendChild(draftRow());

  tb.querySelectorAll("input[data-ck]").forEach((cb) => {
    cb.onchange = () => {
      const id = Number(cb.dataset.ck);
      if (cb.checked) state.keyChecked.add(id); else state.keyChecked.delete(id);
      updateKeySelection();
    };
  });
  tb.querySelectorAll("[data-edit]").forEach((b) => {
    b.onclick = () => { state.keyEditId = Number(b.dataset.edit); renderKeys(); };
  });
  tb.querySelectorAll("[data-cancel]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.cancel === "draft") state.keyDraft = false;
      else state.keyEditId = null;
      renderKeys();
    };
  });
  tb.querySelectorAll("[data-save]").forEach((b) => {
    b.onclick = () => guard(
      () => (b.dataset.save === "draft" ? saveDraft() : saveRow(Number(b.dataset.save))),
      "保存 Key",
    );
  });
  tb.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => guard(
      () => bulk("delete", [Number(b.dataset.del)], `确认删除 Key #${b.dataset.del}？`),
      "删除 Key",
    );
  });

  updateKeySelection();
}

function keyRow(k) {
  const tr = document.createElement("tr");
  const checked = state.keyChecked.has(k.id) ? "checked" : "";
  const ck = `<td><input type="checkbox" data-ck="${k.id}" ${checked}></td>`;

  if (state.keyEditId === k.id) {
    tr.className = "editing";
    tr.innerHTML = `${ck}<td>${k.id}</td>
      <td><input class="k-in wide" data-f="api_key" value="${escapeHtml(k.api_key)}"></td>
      <td><input class="k-in" data-f="project_name" value="${escapeHtml(k.project_name)}"></td>
      <td>${k.usage_count}</td>
      <td>${k.is_active ? "启用" : "禁用"}</td>
      <td><button data-save="${k.id}">保存</button><button data-cancel="row">取消</button></td>`;
  } else {
    tr.innerHTML = `${ck}<td>${k.id}</td>
      <td class="mono" title="${escapeHtml(k.api_key)}">${escapeHtml(k.api_key_masked)}</td>
      <td>${escapeHtml(k.project_name)}</td>
      <td>${k.usage_count}</td>
      <td>${k.is_active ? '<span class="tag-on">启用</span>' : '<span class="tag-off">禁用</span>'}</td>
      <td><button data-edit="${k.id}">编辑</button><button data-del="${k.id}">删除</button></td>`;
  }
  return tr;
}

function draftRow() {
  const tr = document.createElement("tr");
  tr.className = "draft";
  tr.innerHTML = `<td></td><td>新增</td>
    <td><input class="k-in wide" id="draft-key" placeholder="API Key（必填）"></td>
    <td><input class="k-in" id="draft-name" placeholder="项目名（留空自动生成）"></td>
    <td>—</td><td>—</td>
    <td><button data-save="draft">保存</button><button data-cancel="draft">取消</button></td>`;
  return tr;
}

async function saveRow(id) {
  const tr = $(`#keys-body tr.editing input[data-f="api_key"]`).closest("tr");
  const body = {};
  tr.querySelectorAll("input[data-f]").forEach((i) => { body[i.dataset.f] = i.value.trim(); });
  if (!body.api_key) { alert("API Key 不能为空"); return; }

  await api.updateKey(id, body);
  state.keyEditId = null;
  log(`Key #${id} 已更新`, "ok");
  await refreshKeys();
}

async function saveDraft() {
  const apiKey = $("#draft-key").value.trim();
  if (!apiKey) { alert("请填写 API Key"); return; }

  const r = await api.addKeys([{ api_key: apiKey, project_name: $("#draft-name").value.trim() }]);
  state.keyDraft = false;
  (r.skipped || []).forEach((s) => log("跳过：" + s, "warn"));
  log(r.added ? `已新增 ${r.added} 条 Key` : "没有新增任何 Key", r.added ? "ok" : "warn");
  await refreshKeys();
}

function updateKeySelection() {
  const n = state.keyChecked.size;
  const count = $("#key-selcount");
  if (count) {
    count.textContent = `已选 ${n} 条`;
    count.classList.toggle("has-sel", n > 0);
  }
  const all = $("#key-selall");
  if (all) {
    all.checked = state.keys.length > 0 && n === state.keys.length;
    all.indeterminate = n > 0 && n < state.keys.length;
  }
  $$("[data-bulk]").forEach((b) => { b.disabled = n === 0; });
}

async function bulk(action, ids, confirmText) {
  const list = (ids || [...state.keyChecked]).slice();
  if (!list.length) return;
  if (confirmText && !confirm(confirmText.replace("{n}", list.length))) return;

  const r = await api.bulkKeys(list, action);
  log(`已${BULK_LABEL[action] || action} ${r.affected}/${list.length} 条 Key`, "ok");
  state.keyChecked.clear();
  await refreshKeys();
}
