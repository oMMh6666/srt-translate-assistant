/** 任务视图：列表 + 详情（字幕对照 / 运行参数 / 提示词 / 翻译记录）。 */

import { api } from "./api.js";
import { JobEventStream } from "./events.js";
import { log } from "./log.js";
import { isViewActive, state } from "./state.js";
import {
  $, $$, copyText, escapeHtml, fillDataList, fillSelect, fmtLen, guard, prettyPayload,
} from "./util.js";

// JOB_STATUS -> 中文 + 底色类名（底色见 style.css 的 #joblist li.jst-*）
const STATUS_MAP = {
  PENDING: ["未开始", "jst-idle"],
  RUNNING: ["进行中", "jst-run"],
  PAUSED: ["暂停中", "jst-pause"],
  DONE: ["已完成", "jst-done"],
  PARTIAL: ["部分完成", "jst-part"],
  NO_KEY: ["无可用Key", "jst-bad"],
  ERROR: ["出错停止", "jst-bad"],
};

// 走到 _export 的状态才算「产物已生成」，下载按钮据此解锁
const FINISHED_STATUS = new Set(["DONE", "PARTIAL"]);
const TERMINAL_STATUS = new Set(["DONE", "PARTIAL", "PAUSED", "NO_KEY", "ERROR"]);

const SUBTABS = ["pairs", "params", "prompts", "apilogs"];

const stream = new JobEventStream({
  log: (e) => log(e.data.message, e.data.level || "info"),
  batch: (e) => {
    updateRows(e.data.translations || {});
    log(e.data.message, e.data.status === "OK" ? "ok" : "warn");
    focusBatch(e.data.batch_index);
    if (e.data.progress) updateProgress(e.data.progress);
    guard(refreshJobs, "刷新任务列表");
    if (isSubtab("apilogs")) guard(() => loadApiLogs(true), "刷新翻译记录");
  },
  status: (e) => {
    const warn = ["NO_KEY", "ERROR"].includes(e.data.status);
    log("[状态] " + e.data.message, warn ? "warn" : "info");
    if (e.data.progress) updateProgress(e.data.progress);
    guard(refreshJobs, "刷新任务列表");
    // 终态：产物路径是这一轮才写进库的，重拉详情才拿得到 outputs_ready
    if (TERMINAL_STATUS.has(e.data.status)) guard(() => selectJob(state.current), "刷新详情");
  },
  exportEvent: (e) => log("[导出] " + e.data.message, "ok"),
  end: () => guard(refreshJobs, "刷新任务列表"),
});

function statusInfo(s) {
  return STATUS_MAP[s] || [String(s || ""), "jst-idle"];
}

function isSubtab(name) {
  const el = $(`#tab-${name}`);
  return !!el && !el.hidden;
}

// ------------------------------------------------------------------ 初始化

export function initTasks() {
  bindJobActions();
  bindSubtabs();
  bindApiLogActions();
  bindNewJobModal();
}

function bindJobActions() {
  $("#btn-start").onclick = () => guard(async () => {
    await api.startJob(state.current);
    log("任务已启动", "ok");
    setTimeout(() => guard(() => selectJob(state.current), "刷新详情"), 400);
  }, "启动");

  $("#btn-pause").onclick = () => guard(async () => {
    await api.pauseJob(state.current);
    log("已请求暂停（当前批次结束后停止）");
  }, "暂停");

  $("#btn-dl-srt").onclick = () => window.open(api.downloadUrl(state.current, "srt"));
  $("#btn-dl-ass").onclick = () => window.open(api.downloadUrl(state.current, "ass"));

  $("#btn-save-params").onclick = () => guard(async () => {
    await api.saveParams(state.current, readParamsForm());
    log("参数已保存", "ok");
    await selectJob(state.current);
  }, "保存参数");

  $("#btn-save-prompts").onclick = () => guard(async () => {
    await api.savePrompt(state.current, "system_prompt", $("#t-reflect").value);
    await api.savePrompt(state.current, "customer_prompt", $("#t-custom").value);
    log("提示词已保存到本任务", "ok");
  }, "保存提示词");

  $("#btn-load-reflect").onclick = () => loadTemplateInto("#load-reflect", "#t-reflect");
  $("#btn-load-custom").onclick = () => loadTemplateInto("#load-custom", "#t-custom");

  $$("[data-save-template]").forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.dataset.saveTemplate;
      const source = key === "system_prompt" ? "#t-reflect" : "#t-custom";
      const name = prompt("另存为全局模板文件名：",
        key === "system_prompt" ? "reflect.md" : "custom_prompt.md");
      if (!name) return;
      await guard(async () => {
        await api.saveTemplate(name, $(source).value);
        log(`已另存为模板 ${name}`, "ok");
        await refreshTemplates();
      }, "另存模板");
    };
  });

  $("#p-model").onchange = () => applyModelThinking("#p-model", "#p-thinking", true);
  $("#new-model").onchange = () => applyModelThinking("#new-model", "#new-thinking");
  $("#pairs-follow").onchange = (ev) => {
    state.pairsFollow = ev.target.checked;
    if (state.pairsFollow) focusBatch(lastDoneBatch());
  };
}

function bindSubtabs() {
  $$(".subtabs button").forEach((b) => {
    b.onclick = () => {
      $$(".subtabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      SUBTABS.forEach((t) => { $(`#tab-${t}`).hidden = t !== b.dataset.tab; });
      $("#pairs-follow-wrap").hidden = b.dataset.tab !== "pairs";
      // 切进「翻译记录」先拉一次，之后靠定时刷新保持最新
      if (b.dataset.tab === "apilogs") guard(() => loadApiLogs(true), "加载翻译记录");
    };
  });
}

function bindApiLogActions() {
  $("#btn-alogs-refresh").onclick = () => guard(() => loadApiLogs(), "刷新翻译记录");
  $("#btn-copy-req").onclick = () => guard(() => copyBox("#alog-req", "请求内容"), "复制");
  $("#btn-copy-resp").onclick = () => guard(() => copyBox("#alog-resp", "响应内容"), "复制");
  $("#alog-follow").onchange = (ev) => {
    state.alogFollow = ev.target.checked;
    if (state.alogFollow) guard(followLatest, "跟随最新");
  };
}

// ------------------------------------------------------------------ 列表

export async function refreshJobs() {
  const data = await api.jobs();
  state.jobs = data.jobs || [];
  state.runningJob = data.running_job || null;
  state.keyStats = data.keys || state.keyStats;

  $("#runflag").textContent = state.runningJob
    ? `● 运行中：${state.runningJob}` : "○ 无运行中任务";
  $("#runflag").style.color = state.runningJob ? "var(--ok)" : "var(--muted)";

  renderJobList();

  if (!state.current && state.jobs.length) {
    await selectJob(state.jobs[0].id);
    return;
  }

  const job = state.jobs.find((j) => j.id === state.current);
  if (job && state.detail && state.detail.status !== job.status) {
    state.detail.status = job.status;
    updateProgress(job.progress);
    updateDownloadButtons();
  }
  updateStartButton();
}

function renderJobList() {
  const ul = $("#joblist");
  ul.innerHTML = "";
  state.jobs.forEach((j) => {
    const li = document.createElement("li");
    const p = j.progress || {};
    const [label, cls] = statusInfo(j.status);
    li.classList.add(cls);
    const name = j.source_name || j.name;
    li.innerHTML = `<div class="jbody">
        <div class="jname">${escapeHtml(j.name)}</div>
        <div class="jmeta"><span class="jst">${escapeHtml(label)}</span>${p.ok || 0}/${p.total || 0} 批 · ${escapeHtml(name)}${j.has_source ? "" : " ⚠"}</div>
      </div>
      <button class="jdel" title="删除该任务（移到 log/.trash）">✕</button>`;
    if (j.id === state.current) li.classList.add("active");
    li.onclick = () => guard(() => selectJob(j.id), "打开任务");
    li.querySelector(".jdel").onclick = (ev) => { ev.stopPropagation(); deleteJob(j); };
    ul.appendChild(li);
  });
}

async function deleteJob(job) {
  if (!confirm(
    `确定删除任务「${job.name}」？\n\n任务数据库会被移到 log/.trash 目录下` +
    `（不进系统回收站，需要时可手动找回）。`,
  )) return;

  const r = await api.deleteJob(job.id);
  log(`任务「${job.name}」已删除，任务库已移到：${r.trashed_to}`, "warn");
  alert(`任务「${job.name}」已删除。\n\n任务库已移到：\n${r.trashed_to}`);

  if (state.current === job.id) {
    stream.closeAll();
    state.current = null;
    state.detail = null;
    $("#job-empty").hidden = false;
    $("#job-detail").hidden = true;
  }
  await refreshJobs();
  if (!state.current && state.jobs.length) await selectJob(state.jobs[0].id);
  updateStartButton();
}

// ------------------------------------------------------------------ 详情

export async function selectJob(id) {
  state.current = id;
  state.pairsDone = null;
  renderJobList();

  state.detail = await api.jobDetail(id, true);
  $("#job-empty").hidden = true;
  $("#job-detail").hidden = false;
  renderDetail();
  connectEvents();
}

function renderDetail() {
  const d = state.detail;
  const lines = (d.source && d.source.lines) || 0;
  $("#job-name").textContent = (d.source && d.source.filename) || d.job_id;
  $("#job-sub").textContent =
    `${d.params.MODEL_NAME || ""} · 每批 ${d.params.BATCH_SIZE || "-"} 条 · 状态 ${d.status}` +
    (lines ? ` · 字幕 ${lines} 条已入库` : " · ⚠ 字幕未入库");
  updateProgress(d.progress);

  const p = d.params;
  $("#p-model").value = p.MODEL_NAME || "";
  $("#p-lang").value = p.TARGET_LANGUAGE || "";
  applyModelThinking("#p-model", "#p-thinking", true);
  $("#p-batch").value = p.BATCH_SIZE ?? 20;
  $("#p-retrywait").value = p.RETRY_WAIT ?? 2;
  $("#p-reqtimeout").value = p.REQUEST_TIMEOUT ?? 600;
  $("#p-temp").value = p.TEMPERATURE ?? 0.7;
  $("#p-topp").value = p.TOP_P ?? 0.95;
  $("#p-maxtok").value = p.MAX_OUTPUT_TOKENS ?? 65536;

  $("#p-source-meta").textContent = lines
    ? `文件名：${d.source.filename}｜已入库 ${lines} 条（source_srt 表）`
    : "该任务尚未入库字幕";

  // 两条提示词随任务入库，页面键名与库里一致
  $("#t-reflect").value = d.prompts["system_prompt"] || "";
  $("#t-custom").value = d.prompts["customer_prompt"] || "";

  renderPairs(d.pairs || []);
  guard(() => loadApiLogs(), "加载翻译记录");
  updateStartButton();
  updateDownloadButtons();
}

function readParamsForm() {
  return {
    MODEL_NAME: $("#p-model").value,
    TARGET_LANGUAGE: $("#p-lang").value,
    THINKING_LEVEL: $("#p-thinking").value,
    BATCH_SIZE: Number($("#p-batch").value),
    RETRY_WAIT: Number($("#p-retrywait").value),
    REQUEST_TIMEOUT: Number($("#p-reqtimeout").value),
    TEMPERATURE: Number($("#p-temp").value),
    TOP_P: Number($("#p-topp").value),
    MAX_OUTPUT_TOKENS: Number($("#p-maxtok").value),
  };
}

function fillThinking(sel, model) {
  const levels = (state.cfg.thinking_levels || {})[model]
    || ["OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH"];
  fillSelect(sel, levels.map((l) => [l, l === "OFF" ? "OFF（不发送）" : l]));
}

/** 换模型 -> thinking 等级列表与默认值自动连动。 */
function applyModelThinking(modelSel, thinkSel, keepValue) {
  const model = $(modelSel).value;
  const info = state.cfg.model_thinking && state.cfg.model_thinking[model];
  const current = keepValue ? $(thinkSel).value : "";
  fillThinking(thinkSel, model);
  $(thinkSel).value =
    current && (!info || info.levels.includes(current))
      ? current
      : (info ? info.default : "");
}

// ------------------------------------------------------------------ 字幕对照

function renderPairs(pairs) {
  const tb = $("#pairs-body");
  tb.innerHTML = "";
  pairs.forEach((r) => {
    const tr = document.createElement("tr");
    tr.className = "st-" + r.status;
    tr.dataset.id = r.id;
    tr.dataset.batch = r.batch;
    tr.innerHTML = `<td class="idx">${r.index}</td>
      <td class="time">${escapeHtml(r.time)}</td>
      <td class="orig">${escapeHtml(r.original)}</td>
      <td class="trans">${escapeHtml(r.translation || "")}</td>
      <td class="idx">${r.batch}</td>`;
    tb.appendChild(tr);
  });
}

function updateRows(translations) {
  Object.entries(translations).forEach(([id, text]) => {
    const tr = $(`#pairs-body tr[data-id="${id}"]`);
    if (!tr) return;
    tr.querySelector(".trans").textContent = text || "";
    tr.classList.remove("st-PENDING");
    tr.classList.add("st-OK");
  });
}

/** 最近「翻译完」的一批（不是编号最大的一批 —— 最后一批往往还没翻）。 */
function lastDoneBatch() {
  let max = 0;
  $$("#pairs-body tr").forEach((tr) => {
    if (tr.classList.contains("st-PENDING")) return;
    const b = Number(tr.dataset.batch || 0);
    if (b > max) max = b;
  });
  return max;
}

/** 滚到某一批的第一条并短暂高亮（翻译进行时眼睛跟得上）。 */
function focusBatch(batchIndex) {
  if (!state.pairsFollow || !batchIndex || !isSubtab("pairs")) return;
  const rows = $$(`#pairs-body tr[data-batch="${batchIndex}"]`);
  if (!rows.length) return;

  // 先把容器归零再量：Chrome 的滚动锚定会在表格重建后偷偷改 scrollTop，
  // 带着旧偏移算出来的位置会一路滚到底
  const box = $("#tab-pairs");
  box.scrollTop = 0;
  const r = rows[0].getBoundingClientRect();
  const c = box.getBoundingClientRect();
  const target = box.scrollTop + r.top - c.top - (box.clientHeight / 2 - r.height / 2);
  box.scrollTo({ top: Math.max(target, 0), behavior: "smooth" });

  rows.forEach((tr) => {
    tr.classList.remove("flash");
    void tr.offsetWidth;  // 强制重排，连续两批相邻时动画才会重播
    tr.classList.add("flash");
    setTimeout(() => tr.classList.remove("flash"), 1900);
  });
}

// ------------------------------------------------------------------ 按钮 / 进度

function updateStartButton() {
  const btn = $("#btn-start");
  if (!btn || !state.detail) return;

  const p = state.detail.progress || {};
  const blocked = !!(state.runningJob && state.runningJob !== state.current);
  const noKey = (state.keyStats.available || 0) === 0;

  btn.classList.toggle("resume", p.ok > 0 && !blocked);
  btn.classList.toggle("start", !p.ok && !blocked);
  btn.textContent = noKey ? "⚠ 无可用 Key"
    : !p.ok ? "▶ 开始翻译"
      : p.total ? `⟳ 继续（已完成 ${p.ok}/${p.total} 批）`
        : "⟳ 继续翻译";
  btn.disabled = blocked || noKey;
  btn.title = blocked ? "已有任务在运行：" + state.runningJob
    : (noKey ? "没有可用 Key：请到「API Key 管理」启用或新增 Key" : "");
}

/** 下载按钮：没跑完 / 产物不在磁盘上就置灰，并在悬停时说明原因。 */
function updateDownloadButtons() {
  const btns = [["#btn-dl-srt", "srt", "SRT"], ["#btn-dl-ass", "ass", "ASS"]];
  if (!state.detail) {
    btns.forEach(([sel]) => { $(sel).disabled = true; $(sel).title = ""; });
    return;
  }

  const st = state.detail.status || "";
  const finished = FINISHED_STATUS.has(st);
  const p = state.detail.progress || {};
  const ready = state.detail.outputs_ready || {};

  const reason = finished
    ? "产物文件已不在磁盘上（可能被移走或清理），需要重跑一次任务才能导出"
    : `任务还没跑完：当前「${statusInfo(st)[0]}」，还剩 ${p.pending ?? "-"} 批未完成 —— 完成后才生成字幕文件`;

  btns.forEach(([sel, kind, label]) => {
    const btn = $(sel);
    const ok = finished && !!ready[kind];
    btn.disabled = !ok;
    btn.title = ok ? `下载 ${label}：${(state.detail.outputs || {})[kind] || ""}` : reason;
  });
}

function updateProgress(progress) {
  const p = progress || (state.detail && state.detail.progress);
  if (!p) return;
  $("#bar").style.width = (p.percent || 0) + "%";
  $("#progtxt").textContent =
    `${p.ok}/${p.total} 批 · 失败 ${p.error} · 待办 ${p.pending}`;
}

// ------------------------------------------------------------------ 翻译记录

async function loadApiLogs(keepSel = false) {
  if (!keepSel) state.apiLogId = null;
  if (!state.current) return;

  try {
    const r = await api.apiLogs(state.current);
    state.apiLogs = r.logs || [];
  } catch (e) {
    state.apiLogs = [];
    log("读取翻译记录失败：" + e.message, "error");
  }
  renderApiLogs();

  if (!keepSel) {
    $("#alog-meta").textContent = state.apiLogs.length
      ? "选中上方任意一条记录即可查看正文" : "该任务还没有 API 调用记录";
    $("#alog-req").value = "";
    $("#alog-resp").value = "";
  }
  if (state.alogFollow) await followLatest();
}

async function followLatest() {
  if (!state.apiLogs.length) return;
  const top = state.apiLogs[0];
  if (state.apiLogId === top.id) return;  // 已经是最新那条，别反复拉几十 KB 的正文
  await openApiLog(top.id, $(`#alogs-body tr[data-id="${top.id}"]`));
}

function renderApiLogs() {
  const tb = $("#alogs-body");
  if (!tb) return;
  tb.innerHTML = "";
  $("#alogs-count").textContent = state.apiLogs.length
    ? `共 ${state.apiLogs.length} 条调用记录` : "暂无调用记录";

  state.apiLogs.forEach((r) => {
    const tr = document.createElement("tr");
    tr.className = "st-" + (r.status || "");
    tr.dataset.id = r.id;
    if (r.id === state.apiLogId) tr.classList.add("sel");
    const key = r.key_id != null ? `#${r.key_id}` : "—";
    tr.innerHTML = `<td class="idx">${r.id}</td>
      <td class="time">${escapeHtml(r.timestamp || "")}</td>
      <td class="idx">${r.batch_index ?? "—"}</td>
      <td><span class="badge">${escapeHtml(r.status || "")}</span></td>
      <td class="idx" title="${escapeHtml(r.api_key || "")}">${escapeHtml(key)}</td>
      <td class="len">${fmtLen(r.request_len)} / ${fmtLen(r.response_len)}</td>`;
    // 手动点某一行 = 我要盯着这条看，关掉「跟随最新」免得一会儿又被拽走
    tr.onclick = () => {
      state.alogFollow = false;
      const cb = $("#alog-follow");
      if (cb) cb.checked = false;
      guard(() => openApiLog(r.id, tr), "打开记录");
    };
    tb.appendChild(tr);
  });
}

async function openApiLog(id, tr) {
  state.apiLogId = id;
  Array.from($("#alogs-body").children).forEach((x) => x.classList.remove("sel"));
  if (tr) tr.classList.add("sel");

  try {
    const r = await api.apiLogDetail(state.current, id);
    $("#alog-req").value = prettyPayload(r.request_payload);
    $("#alog-resp").value = prettyPayload(r.response_payload);
    $("#alog-meta").textContent =
      `#${r.id}｜批次 ${r.batch_index ?? "—"}｜状态 ${r.status}` +
      `｜Key ${r.key_id != null ? "#" + r.key_id + " " + (r.api_key || "") : "—"}` +
      `｜${r.timestamp || ""}`;
  } catch (e) {
    log("读取记录 #" + id + " 失败：" + e.message, "error");
  }
}

async function copyBox(sel, label) {
  const el = $(sel);
  if (!el.value) { alert(label + "为空，没有东西可复制"); return; }
  const ok = await copyText(el.value);
  log(ok ? `${label}已复制（${el.value.length} 字符）`
    : "复制失败，请手动选中文本按 Ctrl+C", ok ? "ok" : "warn");
}

// ------------------------------------------------------------------ SSE

function connectEvents() {
  if (state.detail && state.detail.running) stream.connect(state.current);
  else stream.closeAll();
}

// ------------------------------------------------------------------ 新建任务

function bindNewJobModal() {
  $("#btn-new").onclick = () => { $("#modal-new").hidden = false; };
  $$("[data-close]").forEach((b) => {
    b.onclick = () => { $(`#${b.dataset.close}`).hidden = true; };
  });

  // 浏览器原生文件选择器拿不到真实路径，所以上传内容到后端落盘
  $("#new-srt-file").onchange = () => {
    const file = $("#new-srt-file").files && $("#new-srt-file").files[0];
    if (!file) {
      state.pickedFile = null;
      $("#new-srt-info").textContent = "尚未选择文件";
      return;
    }
    state.pickedFile = file;
    $("#new-srt-info").textContent =
      `已选择：${file.name}（${(file.size / 1024).toFixed(1)} KB）— 创建时会嵌入任务库`;
  };

  $("#btn-create").onclick = () => guard(async () => {
    if (!state.pickedFile) { alert("请先选择字幕文件"); return; }

    const content = await state.pickedFile.text();
    const up = await api.uploadSrt(state.pickedFile.name, content);
    const r = await api.createJob({
      input_file: up.path,
      params: {
        MODEL_NAME: $("#new-model").value,
        TARGET_LANGUAGE: $("#new-lang").value,
        THINKING_LEVEL: $("#new-thinking").value,
        BATCH_SIZE: Number($("#new-batch").value),
      },
      system_template: $("#new-reflect-tpl").value,
      customer_template: $("#new-custom-tpl").value,
    });

    $("#modal-new").hidden = true;
    state.pickedFile = null;
    $("#new-srt-file").value = "";
    $("#new-srt-info").textContent = "尚未选择文件";
    log(`任务已创建（字幕逐条入库：${up.filename}）。点上方「▶ 开始翻译」才会运行。`, "ok");
    await refreshJobs();
    await selectJob(r.job_id);
  }, "创建任务");
}

async function loadTemplateInto(selSel, targetSel) {
  const name = $(selSel).value;
  if (!name) return;
  await guard(async () => {
    const r = await api.readTemplate(name);
    $(targetSel).value = r.content || "";
    log(`已载入模板 ${name}（还需点「保存提示词到本任务」才会写进该任务）`, "ok");
  }, "载入模板");
}

// ------------------------------------------------------------------ 定时刷新

/** 由 main.js 的统一定时器驱动：翻译记录跟随 + SSE 缺失时的兜底。 */
export async function tick() {
  if (state.alogBusy || !state.current || !isSubtab("apilogs")) return;
  state.alogBusy = true;
  try {
    await loadApiLogs(true);
  } finally {
    state.alogBusy = false;
  }
  await pairsFallback();
}

/**
 * 字幕对照的兜底：SSE 正常时批次事件会直接更新行，
 * 这里只在「没有事件流」（服务重启过 / 任务由别处启动）时整页重拉。
 */
async function pairsFallback() {
  if (!isSubtab("pairs") || stream.connected) return;
  const job = state.jobs.find((j) => j.id === state.current);
  if (!job || !job.progress) return;

  const done = (job.progress.ok || 0) + (job.progress.error || 0);
  if (state.pairsDone === null) { state.pairsDone = done; return; }
  if (done === state.pairsDone) return;
  state.pairsDone = done;
  await selectJob(state.current);
  focusBatch(lastDoneBatch());
}

// ------------------------------------------------------------------ 首屏填充

export function fillTaskSelects(cfg) {
  fillSelect("#p-model", cfg.models.map((m) => [m, m]));
  fillSelect("#new-model", cfg.models.map((m) => [m, m]));
  fillDataList("#lang-list", cfg.languages);
  fillDataList("#lang-list2", cfg.languages);

  const d = cfg.defaults;
  $("#new-model").value = d.MODEL_NAME;
  $("#new-lang").value = d.TARGET_LANGUAGE;
  $("#new-batch").value = d.BATCH_SIZE;
  applyModelThinking("#new-model", "#new-thinking");
}

// 供其它模块（模板管理）在模板列表变化时刷新下拉
export function refreshTemplateSelects(templates) {
  const keepR = $("#new-reflect-tpl").value || "reflect.md";
  const keepC = $("#new-custom-tpl").value || "custom_prompt.md";
  ["#new-reflect-tpl", "#new-custom-tpl", "#load-reflect", "#load-custom"].forEach((sel) => {
    fillSelect(sel, templates.map((t) => [t, t]));
  });
  if (templates.includes(keepR)) $("#new-reflect-tpl").value = keepR;
  if (templates.includes(keepC)) $("#new-custom-tpl").value = keepC;
}

export async function refreshTemplates() {
  const data = await api.templates();
  state.templates = data.templates || [];
  refreshTemplateSelects(state.templates);
  return state.templates;
}
