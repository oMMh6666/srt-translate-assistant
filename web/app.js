const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let CFG = null;
let JOBS = [];
let CURRENT = null;      // 当前任务 id
let DETAIL = null;       // 当前任务详情
let EVENT_SRC = null;
let RUNNING_JOB = null;  // 全局唯一运行中的任务
let KEY_STATS = { total: 0, available: 0 };  // Key 概览（没有可用 Key 时禁止启动）
let TEMPLATES = [];      // 全局提示词模板
let TPL_CURRENT = "reflect.md";
let TPL_INITIALIZED = false;
let TPL_DIRTY = false;   // 编辑中未保存，防止自动刷新覆盖
let PICKED_FILE = null;  // 新建任务时选中的字幕文件（File 对象）
let SIDE_W = 280;        // 左侧栏宽度（可拖动，写进 localStorage）
let LOG_H = 200;         // 底部运行日志区高度（可拖动，写进 localStorage）

let APILOGS = [];        // 当前任务的 api_logs 清单（不含正文）
let APILOG_ID = null;    // 当前选中展开的那条记录 id
let ALOG_FOLLOW = true;  // 跟随最新：运行中自动跳到最新一条（手动点行会取消）
let ALOG_BUSY = false;   // 正在刷新清单，避免定时器与手动刷新并发打同一接口
let PAIRS_FOLLOW = true; // 跟随最新批次：完成一批就滚到那批并高亮
let PAIRS_DONE = null;   // 上次看到「已完成批次数」，用于 SSE 断掉时兜底感知进度
let SSE_RETRY = 0;       // 事件流断了之后的重连次数（封顶 8 次，避免空转）

// JOB_STATUS -> 中文 + 底色类名（底色见 style.css 的 #joblist li.jst-*）
const STATUS_MAP = {
  PENDING:  ["未开始",  "jst-idle"],
  RUNNING:  ["进行中",  "jst-run"],
  PAUSED:   ["暂停中",  "jst-pause"],
  DONE:     ["已完成",  "jst-done"],
  PARTIAL:  ["部分完成", "jst-part"],
  NO_KEY:   ["无可用Key", "jst-bad"],
  ERROR:    ["出错停止", "jst-bad"],
};

function statusInfo(s) {
  return STATUS_MAP[s] || [String(s || ""), "jst-idle"];
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const msg = await res.text().catch(() => "");
    throw new Error(`${res.status} ${msg.slice(0, 200)}`);
  }
  return res.json();
}

function log(msg, level = "info") {
  const box = $("#logs");
  const div = document.createElement("div");
  div.className = "lv-" + level;
  const t = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  div.textContent = `[${t}] ${msg}`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------------------ 初始化

async function init() {
  CFG = await api("/api/config");
  fillSelect("#p-engine", CFG.engines.map((e) => [e.name, e.label]));
  fillSelect("#new-engine", CFG.engines.map((e) => [e.name, e.label]));
  fillSelect("#p-model", CFG.models.map((m) => [m, m]));
  fillSelect("#new-model", CFG.models.map((m) => [m, m]));
  fillDataList("#lang-list", CFG.languages);
  fillDataList("#lang-list2", CFG.languages);

  await refreshTemplates();

  // 新建任务默认值
  const d = CFG.defaults;
  $("#new-model").value = d.MODEL_NAME;
  $("#new-lang").value = d.TARGET_LANGUAGE;
  $("#new-batch").value = d.BATCH_SIZE;
  applyModelThinking("#new-model", "#new-thinking");

  await refreshJobs();
  await refreshKeys();
  initSplitter();
  $("#pairs-follow-wrap").hidden = false;  // 默认停在「字幕对照」页签
  setInterval(refreshJobs, 5000);
  setInterval(refreshKeys, 15000);
  // 只要停在某个详情页签就轮询：不看任务是否在跑、也不依赖 SSE
  // —— 服务重启过 / 任务在别的会话里跑时没有事件流，光靠 SSE 就只能手动刷新
  setInterval(() => guard(viewTick, "自动刷新详情"), 2500);
}

// ------------------------------------------- 可拖动分隔条（左栏 + 日志区）
// 拖到哪写到 localStorage，刷新后还在原处；双击分隔条恢复默认
function initSplitter() {
  const KEY_W = "ui.sidebar.width", DEF_W = 280;
  const KEY_H = "ui.log.height", DEF_H = 200;
  const clampW = (w) => Math.min(Math.max(Math.round(w), 180), Math.max(window.innerWidth - 360, 180));
  const clampH = (h) => Math.min(Math.max(Math.round(h), 80), Math.max(window.innerHeight - 260, 120));

  const savedW = Number(localStorage.getItem(KEY_W));
  if (savedW >= 180) setSideWidth(clampW(savedW));
  const savedH = Number(localStorage.getItem(KEY_H));
  if (savedH >= 80) setLogHeight(clampH(savedH));

  // 竖向分隔条：左右拖，改左栏宽度
  $$("[data-split='side']").forEach((el) => {
    el.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      const left = el.parentElement.getBoundingClientRect().left;
      beginDrag(el, "resizing", (e) => setSideWidth(clampW(e.clientX - left)),
        () => localStorage.setItem(KEY_W, String(SIDE_W)));
    });
    el.addEventListener("dblclick", () => {
      setSideWidth(clampW(DEF_W));
      localStorage.setItem(KEY_W, String(SIDE_W));
    });
  });

  // 横向分隔条：上下拖，改日志区高度（日志区在分隔条下方，往上拖 = 变高）
  const logBar = $("[data-split='log']");
  if (logBar) {
    logBar.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      const bottom = document.body.getBoundingClientRect().bottom;
      beginDrag(logBar, "resizing-v", (e) => setLogHeight(clampH(bottom - e.clientY)),
        () => localStorage.setItem(KEY_H, String(LOG_H)));
    });
    logBar.addEventListener("dblclick", () => {
      setLogHeight(clampH(DEF_H));
      localStorage.setItem(KEY_H, String(LOG_H));
    });
  }
}

function beginDrag(el, bodyCls, onMove, onEnd) {
  el.classList.add("active");
  document.body.classList.add(bodyCls);
  const move = (e) => onMove(e);
  const up = () => {
    el.classList.remove("active");
    document.body.classList.remove(bodyCls);
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
    onEnd();
  };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
}

function setSideWidth(w) {
  SIDE_W = w;
  document.documentElement.style.setProperty("--side-w", w + "px");
}

function setLogHeight(h) {
  LOG_H = h;
  document.documentElement.style.setProperty("--log-h", h + "px");
}

function fillSelect(sel, pairs) {
  const el = $(sel);
  el.innerHTML = "";
  pairs.forEach(([v, t]) => {
    const o = document.createElement("option");
    o.value = v; o.textContent = t; el.appendChild(o);
  });
}

function fillDataList(sel, items) {
  const el = $(sel);
  el.innerHTML = "";
  items.forEach((v) => {
    const o = document.createElement("option");
    o.value = v; el.appendChild(o);
  });
}

function fillThinking(sel, model) {
  const levels = CFG.thinking_levels[model] || ["OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH"];
  fillSelect(sel, levels.map((l) => [l, l === "OFF" ? "OFF（不发送）" : l]));
}

// 换模型 -> thinking 等级列表与默认值自动连动
// （3.5-lite / 3.5 / 3.6 默认 MINIMAL，3.7 / 3.8 默认 LOW）
function applyModelThinking(modelSel, thinkSel, keepValue) {
  const model = $(modelSel).value;
  const info = CFG.model_thinking && CFG.model_thinking[model];
  const current = keepValue ? $(thinkSel).value : "";
  fillThinking(thinkSel, model);
  $(thinkSel).value =
    current && (!info || info.levels.includes(current))
      ? current
      : (info ? info.default : "");
}

// ------------------------------------------------------------------ 任务列表

async function refreshJobs() {
  try {
    const data = await api("/api/jobs");
    JOBS = data.jobs;
    RUNNING_JOB = data.running_job || null;
    if (data.keys) KEY_STATS = data.keys;
    $("#runflag").textContent = RUNNING_JOB ? `● 运行中：${RUNNING_JOB}` : "○ 无运行中任务";
    $("#runflag").style.color = RUNNING_JOB ? "var(--ok)" : "var(--muted)";
    renderJobList();
    if (!CURRENT && JOBS.length) await selectJob(JOBS[0].db_path);
    updateStartButton();
    if (CURRENT) {
      const j = JOBS.find((x) => x.name === CURRENT || x.db_path.endsWith(CURRENT));
      if (j && DETAIL && DETAIL.status !== j.status) {
        DETAIL.status = j.status;
        updateProgress(j);
        updateDownloadButtons();
        // 刚跑完：产物路径是这一轮才写进库的，重拉一次详情才拿得到 outputs_ready
        if (FINISHED_STATUS.has(j.status)) selectJob(CURRENT);
      }
    }
  } catch (e) { /* 静默 */ }
}

function renderJobList() {
  const ul = $("#joblist");
  ul.innerHTML = "";
  JOBS.forEach((j) => {
    const li = document.createElement("li");
    const p = j.progress;
    const srtName = j.source_name || j.name;
    const [label, cls] = statusInfo(j.status);
    li.classList.add(cls);
    li.innerHTML = `<div class="jbody">
        <div class="jname">${escapeHtml(j.name)}</div>
        <div class="jmeta"><span class="jst">${escapeHtml(label)}</span>${p.ok}/${p.total} 批 · ${escapeHtml(srtName)}${j.has_source ? "" : " ⚠"}</div>
      </div>
      <button class="jdel" title="删除该任务（移到 log/.trash）">✕</button>`;
    if (j.db_path.endsWith(CURRENT) || j.name === CURRENT) li.classList.add("active");
    li.onclick = () => selectJob(j.db_path);
    li.querySelector(".jdel").onclick = (ev) => { ev.stopPropagation(); deleteJob(j); };
    ul.appendChild(li);
  });
}

// 单个删除：任务库整个挪进 log/.trash（不进系统回收站，可手动找回）
async function deleteJob(job) {
  const name = job.name;
  if (!confirm(
    `确定删除任务「${name}」？\n\n任务数据库会被移到 log/.trash 目录下` +
    `（不进系统回收站，需要时可手动找回）。`
  )) return;

  const r = await api(`/api/jobs/${encodeURIComponent(job.db_path)}`, { method: "DELETE" });
  log(`任务「${name}」已删除，任务库已移到：${r.trashed_to}`, "warn");
  alert(`任务「${name}」已删除。\n\n任务库已移到：\n${r.trashed_to}`);

  if (CURRENT === job.db_path || (CURRENT && job.db_path.endsWith(CURRENT))) {
    if (EVENT_SRC) { EVENT_SRC.close(); EVENT_SRC = null; }
    CURRENT = null;
    DETAIL = null;
    $("#job-empty").hidden = false;
    $("#job-detail").hidden = true;
  }
  await refreshJobs();
  if (!CURRENT && JOBS.length) await selectJob(JOBS[0].db_path);
  updateStartButton();
}

async function selectJob(dbPath) {
  CURRENT = dbPath;
  PAIRS_DONE = null;   // 换任务：重新以当前进度为基准
  SSE_RETRY = 0;
  renderJobList();
  try {
    DETAIL = await api("/api/jobs/" + encodeURIComponent(dbPath));
  } catch (e) {
    log("加载任务失败：" + e.message, "error");
    return;
  }
  $("#job-empty").hidden = true;
  $("#job-detail").hidden = false;
  renderDetail();
  connectEvents();
}

function renderDetail() {
  const d = DETAIL;
  const srcName = (d.source && d.source.filename) ||
    (d.params.INPUT_FILE ? d.params.INPUT_FILE.split(/[\\/]/).pop() : d.job_id);
  $("#job-name").textContent = srcName;
  const lines = (d.source && d.source.lines) || 0;
  $("#job-sub").textContent =
    `${d.params.MODEL_NAME || ""} · 每批 ${d.params.BATCH_SIZE || "-"} 条 · 状态 ${d.status}` +
    (lines ? ` · 字幕 ${lines} 条已入库` : " · ⚠ 字幕未入库");
  updateProgress();

  // 参数
  const p = d.params;
  $("#p-engine").value = p.ENGINE || "gemini";
  $("#p-model").value = p.MODEL_NAME || "";
  $("#p-lang").value = p.TARGET_LANGUAGE || "";
  applyModelThinking("#p-model", "#p-thinking", true);
  $("#p-batch").value = p.BATCH_SIZE ?? 20;
  $("#p-retrywait").value = p.RETRY_WAIT ?? 2;
  $("#p-reqtimeout").value = p.REQUEST_TIMEOUT ?? 600;
  $("#p-temp").value = p.TEMPERATURE ?? 0.7;
  $("#p-topp").value = p.TOP_P ?? 0.95;
  $("#p-maxtok").value = p.MAX_OUTPUT_TOKENS ?? 65536;

  // 内嵌字幕（只记文件名，不记路径）
  $("#p-source-meta").textContent = lines
    ? `文件名：${d.source.filename}｜已入库 ${lines} 条（source_srt 表）`
    : "该任务尚未入库字幕";

  // 两条提示词随任务入库（system_prompt / customer_prompt），页面用的键名与库里一致
  $("#t-reflect").value = d.prompts["system_prompt"] || "";
  $("#t-custom").value = d.prompts["customer_prompt"] || "";

  renderPairs(d.pairs);
  loadApiLogs();
  updateStartButton();
  updateDownloadButtons();
}

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
    if (tr) {
      tr.querySelector(".trans").textContent = text || "";
      tr.classList.remove("st-PENDING");
      if (!tr.classList.contains("st-FALLBACK")) tr.classList.add("st-OK");
    }
  });
}

// 未完成过任何批次 -> 「开始」；已完成至少一批 -> 「继续」
// 正在看「翻译记录」时跟着任务自动刷新（别的页签不需要白花一次请求）
function autoRefreshApiLogs() {
  if (!$("#tab-apilogs") || $("#tab-apilogs").hidden) return;
  if (!$("#alogs-count")) return;
  loadApiLogs(true);
}

// 2.5s 一轮的统一刷新：按「当前停在哪个页签」决定干什么
async function viewTick() {
  await alogsTick();   // 翻译记录：清单 +（跟随最新时）正文
  await pairsTick();   // 字幕对照：进度变了就重拉详情并跳到最新一批
}

// 翻译记录：页签可见就拉清单（不看运行状态，SSE 有没有都跟得上）
async function alogsTick() {
  if (ALOG_BUSY) return;
  const tab = $("#tab-apilogs");
  if (!tab || tab.hidden || !CURRENT) return;
  ALOG_BUSY = true;
  try {
    await loadApiLogs(true);
  } finally {
    ALOG_BUSY = false;
  }
}

// 字幕对照：SSE 正常时批次事件会直接更新行，这里只兜底 SSE 缺失的情况
// （服务重启、任务由别处启动）—— 发现完成批次数变了、又没有事件流，就重拉详情
async function pairsTick() {
  const tab = $("#tab-pairs");
  if (!tab || tab.hidden || !CURRENT) { PAIRS_DONE = null; return; }
  const j = JOBS.find((x) => x.db_path === CURRENT || x.name === CURRENT);
  if (!j || !j.progress) return;
  const done = (j.progress.ok || 0) + (j.progress.fallback || 0);
  if (PAIRS_DONE === null) { PAIRS_DONE = done; return; }
  if (done === PAIRS_DONE) return;
  PAIRS_DONE = done;
  if (EVENT_SRC) return;              // 有事件流就交给它，别重复拉
  await selectJob(CURRENT);           // SSE 没连上：整页重拉，进度和译文才不会落后
  focusBatch(lastDoneBatch());
}

// 最近「翻译完」的一批（不是编号最大的一批 —— 最后一批往往还没翻，滚过去只会到底）
function lastDoneBatch() {
  let max = 0;
  $$("#pairs-body tr").forEach((tr) => {
    if (tr.classList.contains("st-PENDING")) return;
    const b = Number(tr.dataset.batch || 0);
    if (b > max) max = b;
  });
  return max;
}

// 滚到某一批的第一条并短暂高亮（翻译进行时眼睛跟得上）
function focusBatch(batchIndex) {
  if (!PAIRS_FOLLOW || !batchIndex) return;
  const tab = $("#tab-pairs");
  if (!tab || tab.hidden) return;
  const rows = $$(`#pairs-body tr[data-batch="${batchIndex}"]`);
  if (!rows.length) return;
  // 先把容器归零再量：Chrome 的滚动锚定（scroll anchoring）会在表格重建后
  // 偷偷改 scrollTop，带着旧偏移算出来的位置会一路滚到底
  const box = $("#tab-pairs");
  box.scrollTop = 0;
  const r = rows[0].getBoundingClientRect();
  const c = box.getBoundingClientRect();
  const target = box.scrollTop + r.top - c.top - (box.clientHeight / 2 - r.height / 2);
  box.scrollTo({ top: Math.max(target, 0), behavior: "smooth" });
  rows.forEach((tr) => {
    tr.classList.remove("flash");
    // 强制重排，动画才会重新播放（连续两批相邻时第二排也要亮）
    void tr.offsetWidth;
    tr.classList.add("flash");
    setTimeout(() => tr.classList.remove("flash"), 1900);
  });
}

// ------------------------------------------------- 翻译记录（任务库 api_logs）

// 选了别的任务就把上一次的清单清掉，避免残留旧任务的记录行
// keepSel：运行期间自动刷新时保留已选中的那条，不至于看着看着被重置
async function loadApiLogs(keepSel = false) {
  if (!keepSel) APILOG_ID = null;
  if (!CURRENT) return;
  try {
    const r = await api(`/api/jobs/${encodeURIComponent(CURRENT)}/apilogs`);
    APILOGS = r.logs || [];
  } catch (e) {
    APILOGS = [];
    log("读取翻译记录失败：" + e.message, "error");
  }
  renderApiLogs();
  if (!keepSel) {
    $("#alog-meta").textContent = APILOGS.length
      ? "选中上方任意一条记录即可查看正文"
      : "该任务还没有 API 调用记录";
    $("#alog-req").value = "";
    $("#alog-resp").value = "";
  }
  // 跟随最新：新记录进来后自动跳到第一条（清单按 id 倒序，第一条即最新）
  if (ALOG_FOLLOW) await followLatest();
}

async function followLatest() {
  if (!APILOGS.length) return;
  const top = APILOGS[0];
  if (APILOG_ID === top.id) return;  // 已经是最新那条，别反复拉几十 KB 的正文
  const tr = $(`#alogs-body tr[data-id="${top.id}"]`);
  await openApiLog(top.id, tr);
}

function renderApiLogs() {
  const tb = $("#alogs-body");
  if (!tb) return;
  tb.innerHTML = "";
  $("#alogs-count").textContent =
    APILOGS.length ? `共 ${APILOGS.length} 条调用记录` : "暂无调用记录";

  APILOGS.forEach((r) => {
    const tr = document.createElement("tr");
    tr.className = "st-" + (r.status || "");
    tr.dataset.id = r.id;
    if (r.id === APILOG_ID) tr.classList.add("sel");
    const key = r.key_id != null ? `#${r.key_id}` : "—";
    tr.innerHTML = `<td class="idx">${r.id}</td>
      <td class="time">${escapeHtml(r.timestamp || "")}</td>
      <td class="idx">${r.batch_index ?? "—"}</td>
      <td><span class="badge">${escapeHtml(r.status || "")}</span></td>
      <td class="idx" title="${escapeHtml(r.api_key || "")}">${escapeHtml(key)}</td>
      <td class="len">${fmtLen(r.request_len)} / ${fmtLen(r.response_len)}</td>`;
    // 手动点某一行 = 我要盯着这条看，关掉「跟随最新」免得 3s 后又被拽走
    tr.onclick = () => {
      ALOG_FOLLOW = false;
      const cb = $("#alog-follow");
      if (cb) cb.checked = false;
      openApiLog(r.id, tr);
    };
    tb.appendChild(tr);
  });
}

function fmtLen(n) {
  n = Number(n || 0);
  return n >= 1024 ? `${(n / 1024).toFixed(1)}K` : String(n);
}

async function openApiLog(id, tr) {
  APILOG_ID = id;
  tbSelect(tr);
  try {
    const r = await api(`/api/jobs/${encodeURIComponent(CURRENT)}/apilogs/${id}`);
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

function tbSelect(tr) {
  Array.from($("#alogs-body").children).forEach((x) => x.classList.remove("sel"));
  if (tr) tr.classList.add("sel");
}

// 库里存的是压缩 JSON，展开成缩进好看些；不是 JSON 就原样显示
function prettyPayload(text) {
  if (!text) return "";
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

async function copyBox(sel, label) {
  const el = $(sel);
  if (!el.value) { alert(label + "为空，没有东西可复制"); return; }
  await copyText(el.value, label);
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    log(`${label}已复制到剪贴板（${text.length} 字符）`, "ok");
    return;
  } catch { /* 无剪贴板权限时退回手动选中 */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  log(ok ? `${label}已复制` : `复制失败，请手动选中文本按 Ctrl+C`, ok ? "ok" : "warn");
}

function updateStartButton() {
  const btn = $("#btn-start");
  if (!btn || !DETAIL) return;

  const p = DETAIL.progress || {};
  const done = (p.ok || 0) + (p.fallback || 0);
  const blocked = !!(RUNNING_JOB && RUNNING_JOB !== CURRENT);
  const noKey = (KEY_STATS.available || 0) === 0;  // 没有可用 Key：启动后也会立刻停

  btn.classList.toggle("resume", done > 0 && !blocked);
  btn.classList.toggle("start", done === 0 && !blocked);
  btn.textContent =
    noKey ? "⚠ 无可用 Key"
      : done === 0 ? "▶ 开始翻译"
        : p.total ? `⟳ 继续（已完成 ${p.ok}/${p.total} 批）`
          : "⟳ 继续翻译";
  btn.disabled = blocked || noKey;
  btn.title = blocked ? "已有任务在运行：" + RUNNING_JOB
    : (noKey ? "没有可用 Key：请到「API Key 管理」启用或新增 Key" : "");
}

// 已导出产物才算跑完：DONE / PARTIAL 都会走到 _export 写出两个文件
const FINISHED_STATUS = new Set(["DONE", "PARTIAL"]);

// 下载按钮：任务没跑完就置灰并说明原因，别让用户点了才收到「产物尚未生成」
function updateDownloadButtons() {
  const btns = [["#btn-dl-srt", "srt", "SRT"], ["#btn-dl-ass", "ass", "ASS"]];
  if (!DETAIL) {
    btns.forEach(([sel]) => { $(sel).disabled = true; $(sel).title = ""; });
    return;
  }

  const st = DETAIL.status || "";
  const finished = FINISHED_STATUS.has(st);
  const p = DETAIL.progress || {};
  const ready = DETAIL.outputs_ready || {};

  const reason = finished
    ? "产物文件已不在磁盘上（可能被移走或清理），需要重跑一次任务才能导出"
    : `任务还没跑完：当前「${statusInfo(st)[0]}」，还剩 ${p.pending ?? "-"} 批未完成 —— 完成后才生成字幕文件`;

  btns.forEach(([sel, kind, label]) => {
    const btn = $(sel);
    const ok = finished && !!ready[kind];
    btn.disabled = !ok;
    btn.title = ok ? `下载 ${label}：${(DETAIL.outputs || {})[kind] || ""}` : reason;
  });
}

function updateProgress(job) {
  const p = (job && job.progress) || (DETAIL && DETAIL.progress);
  if (!p) return;
  $("#bar").style.width = (p.percent || 0) + "%";
  $("#progtxt").textContent = `${p.ok}/${p.total} 批 · 兜底 ${p.fallback} · 失败 ${p.error} · 待办 ${p.pending}`;
}

// ------------------------------------------------------------------ SSE

function connectEvents() {
  if (EVENT_SRC) { EVENT_SRC.close(); EVENT_SRC = null; }
  if (!DETAIL || !DETAIL.running) { maybeReconnect(); return; }
  SSE_RETRY = 0;
  EVENT_SRC = new EventSource("/api/jobs/" + encodeURIComponent(CURRENT) + "/events");
  EVENT_SRC.onmessage = (ev) => {
    const e = JSON.parse(ev.data);
    if (e.type === "batch") {
      updateRows(e.translations || {});
      log(e.message, e.status === "OK" ? "ok" : "warn");
      focusBatch(e.batch_index);   // 眼睛跟着最新完成的一批走
      autoRefreshApiLogs();
      refreshJobs();
    } else if (e.type === "log") {
      log(e.message, e.level || "info");
    } else if (e.type === "status") {
      const warn = ["WAITING_QUOTA", "NO_KEY", "ERROR"].includes(e.status);
      log("[状态] " + e.message, warn ? "warn" : "info");
      refreshJobs();
    } else if (e.type === "export") {
      log("[导出] " + e.message, "ok");
    } else if (e.type === "done") {
      log("[完成] " + e.message, "ok");
      refreshJobs();
      if (DETAIL) loadApiLogs(true);
      selectJob(CURRENT);
    }
  };
  EVENT_SRC.addEventListener("end", () => {
    EVENT_SRC.close();
    EVENT_SRC = null;
    maybeReconnect();
  });
}

// 事件流结束但任务还在跑（典型场景：本地服务被重启过），隔几秒补连一次。
// 次数压实到 2 次、间隔 5s —— 之前一断开就重试会陷入「建了就断」的重连风暴，
// 新的连接永远停在 CONNECTING，反而更糟。反正下面还有 2.5s 轮询兜底
function maybeReconnect() {
  if (!CURRENT || SSE_RETRY >= 2) return;
  if (!(DETAIL && DETAIL.running)) { SSE_RETRY = 0; return; }
  SSE_RETRY += 1;
  setTimeout(async () => {
    if (!CURRENT) return;
    try {
      DETAIL = await api("/api/jobs/" + encodeURIComponent(CURRENT));
    } catch { return; }
    if (DETAIL && DETAIL.running) connectEvents();
  }, 5000);
}

// ------------------------------------------------------------------ 操作

async function guard(fn, name) {
  try { await fn(); }
  catch (e) { log(`${name}失败：${e.message}`, "error"); }
}

$("#btn-start").onclick = () => guard(async () => {
  await api(`/api/jobs/${encodeURIComponent(CURRENT)}/start`, { method: "POST" });
  log("任务已启动", "ok");
  setTimeout(async () => { await selectJob(CURRENT); }, 400);
}, "启动");

$("#btn-pause").onclick = () => guard(async () => {
  await api(`/api/jobs/${encodeURIComponent(CURRENT)}/pause`, { method: "POST" });
  log("已请求暂停（当前批次结束后停止）");
}, "暂停");

$("#btn-dl-srt").onclick = () => window.open(`/api/jobs/${encodeURIComponent(CURRENT)}/download?kind=srt`);
$("#btn-dl-ass").onclick = () => window.open(`/api/jobs/${encodeURIComponent(CURRENT)}/download?kind=ass`);

$("#btn-save-params").onclick = () => guard(async () => {
  const params = {
    ENGINE: $("#p-engine").value,
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
  await api(`/api/jobs/${encodeURIComponent(CURRENT)}/params`, {
    method: "PUT", body: JSON.stringify(params),
  });
  log("参数已保存", "ok");
  await selectJob(CURRENT);
}, "保存参数");

$("#btn-save-prompts").onclick = () => guard(async () => {
  const items = [
    ["system_prompt", $("#t-reflect").value],
    ["customer_prompt", $("#t-custom").value],
  ];
  for (const [name, content] of items) {
    await api(`/api/jobs/${encodeURIComponent(CURRENT)}/prompts`, {
      method: "PUT", body: JSON.stringify({ name, content }),
    });
  }
  log("提示词已保存到本任务", "ok");
}, "保存提示词");

// 任务内载入全局模板
$("#btn-load-reflect").onclick = () => loadTemplateInto("#load-reflect", "#t-reflect");
$("#btn-load-custom").onclick = () => loadTemplateInto("#load-custom", "#t-custom");

async function loadTemplateInto(selSel, targetSel) {
  const name = $(selSel).value;
  if (!name) return;
  await guard(async () => {
    const r = await api("/api/prompts/templates/" + encodeURIComponent(name));
    $(targetSel).value = r.content || "";
    log(`已载入模板 ${name}（还需点「保存提示词到本任务」才会写进该任务）`, "ok");
  }, "载入模板");
}

$$("[data-save-template]").forEach((btn) => {
  btn.onclick = async () => {
    const key = btn.dataset.saveTemplate;
    const mapBtn = { system_prompt: "#t-reflect", customer_prompt: "#t-custom" };
    const name = prompt("另存为全局模板文件名：", key === "system_prompt" ? "reflect.md" : "custom_prompt.md");
    if (!name) return;
    await guard(async () => {
      await api("/api/prompts/templates", {
        method: "POST",
        body: JSON.stringify({ name, content: $(mapBtn[key]).value }),
      });
      log(`已另存为模板 ${name}`, "ok");
      await refreshTemplates();
    }, "另存模板");
  };
});

$("#btn-clearlog").onclick = () => { $("#logs").innerHTML = ""; };

// 翻译记录：刷新清单 / 复制请求 / 复制响应
$("#btn-alogs-refresh").onclick = () => guard(() => loadApiLogs(), "刷新翻译记录");
$("#btn-copy-req").onclick = () => guard(() => copyBox("#alog-req", "请求内容"), "复制");
$("#btn-copy-resp").onclick = () => guard(() => copyBox("#alog-resp", "响应内容"), "复制");

// 「跟随最新」开关：勾上就立刻跳到最新一条；任务跑完/暂停后仍可手动点行查看
$("#alog-follow").onchange = (ev) => {
  ALOG_FOLLOW = ev.target.checked;
  if (ALOG_FOLLOW) guard(followLatest, "跟随最新");
};

// 「跟随最新批次」：勾上就立刻滚到最近完成的一批
$("#pairs-follow").onchange = (ev) => {
  PAIRS_FOLLOW = ev.target.checked;
  if (PAIRS_FOLLOW) focusBatch(lastDoneBatch());
};

// ------------------------------------------------------------------ 提示词管理

async function refreshTemplates() {
  const data = await api("/api/prompts/templates");
  TEMPLATES = data.templates;

  // 重建下拉时保留当前选择；首次进入默认 reflect.md / custom_prompt.md
  const keepR = $("#new-reflect-tpl").value || "reflect.md";
  const keepC = $("#new-custom-tpl").value || "custom_prompt.md";
  fillSelect("#new-reflect-tpl", TEMPLATES.map((t) => [t, t]));
  fillSelect("#new-custom-tpl", TEMPLATES.map((t) => [t, t]));
  fillSelect("#load-reflect", TEMPLATES.map((t) => [t, t]));
  fillSelect("#load-custom", TEMPLATES.map((t) => [t, t]));
  if (TEMPLATES.includes(keepR)) $("#new-reflect-tpl").value = keepR;
  if (TEMPLATES.includes(keepC)) $("#new-custom-tpl").value = keepC;

  const ul = $("#tpllist");
  ul.innerHTML = "";
  TEMPLATES.forEach((t) => {
    const li = document.createElement("li");
    li.innerHTML = `<div class="jname">${escapeHtml(t)}</div>
      <div class="jmeta">${t.startsWith("reflect") ? "反思翻译主提示词" : "自定义样式 / 术语"}</div>`;
    if (t === TPL_CURRENT) li.classList.add("active");
    li.onclick = () => openTemplate(t);
    ul.appendChild(li);
  });

  if (
    !TPL_INITIALIZED ||
    (!TEMPLATES.includes(TPL_CURRENT) && !TPL_DIRTY)
  ) {
    TPL_INITIALIZED = true;
    TPL_CURRENT = TEMPLATES.includes(TPL_CURRENT)
      ? TPL_CURRENT
      : (TEMPLATES[0] || "reflect.md");
    await openTemplate(TPL_CURRENT);
  }
  $("#tpl-title").textContent = TPL_CURRENT;
}

async function openTemplate(name) {
  TPL_CURRENT = name;
  $("#tpl-name").value = name;
  $("#tpl-title").textContent = name;
  try {
    const r = await api("/api/prompts/templates/" + encodeURIComponent(name));
    $("#tpl-content").value = r.content || "";
  } catch (e) {
    $("#tpl-content").value = "";
    log("读取模板失败：" + e.message, "error");
  }
  TPL_DIRTY = false;
  const ul = $("#tpllist");
  Array.from(ul.children).forEach((li) =>
    li.classList.toggle("active", li.textContent.startsWith(name))
  );
}

$("#tpl-content").addEventListener("input", () => { TPL_DIRTY = true; });

$("#btn-tpl-new").onclick = () => {
  const name = prompt("新模板文件名（.md）：", "custom_prompt.new.md");
  if (!name) return;
  $("#tpl-content").value = "";
  TPL_CURRENT = name.endsWith(".md") ? name : name + ".md";
  TPL_INITIALIZED = true;
  TPL_DIRTY = false;
  $("#tpl-name").value = TPL_CURRENT;
  $("#tpl-title").textContent = TPL_CURRENT;
  log("已切换到新模板，编辑后点「保存模板」", "info");
};

$("#btn-tpl-save").onclick = () => guard(async () => {
  const name = $("#tpl-name").value.trim();
  if (!name) { alert("请填写模板文件名"); return; }
  const r = await api("/api/prompts/templates", {
    method: "POST",
    body: JSON.stringify({ name, content: $("#tpl-content").value }),
  });
  TPL_CURRENT = r.name;
  TPL_DIRTY = false;
  log(`模板已保存：${r.name}`, "ok");
  await refreshTemplates();
}, "保存模板");

$("#btn-tpl-delete").onclick = () => guard(async () => {
  const name = TPL_CURRENT;
  if (!confirm(`确认删除模板 ${name}？`)) return;
  await api("/api/prompts/templates/" + encodeURIComponent(name), { method: "DELETE" });
  log(`模板已删除：${name}`, "ok");
  await refreshTemplates();
}, "删除模板");

// ------------------------------------------------------------------ 子标签 / 顶栏

$$(".subtabs button").forEach((b) => {
  b.onclick = () => {
    $$(".subtabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    ["pairs", "params", "prompts", "apilogs"].forEach((t) => {
      $("#tab-" + t).hidden = t !== b.dataset.tab;
    });
    // 切进「翻译记录」先拉一次：除此以外靠 2.5s 轮询保持最新
    if (b.dataset.tab === "apilogs") loadApiLogs(true);
    $("#pairs-follow-wrap").hidden = b.dataset.tab !== "pairs";
  };
});

$$("#toptabs button").forEach((b) => {
  b.onclick = () => {
    $$("#toptabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const v = b.dataset.view;
    $("#view-tasks").classList.toggle("active", v === "tasks");
    $("#view-prompts").classList.toggle("active", v === "prompts");
    $("#view-keys").classList.toggle("active", v === "keys");
    if (v === "keys") refreshKeys();
    if (v === "prompts") refreshTemplates().catch(() => {});
  };
});

// ------------------------------------------------------------------ Key

let KEYS = [];            // API Key 列表
let KEY_CHECKED = new Set();  // 勾选中的 id
let KEY_EDIT_ID = null;   // 正在行内编辑的行 id
let KEY_DRAFT = false;    // 是否正在新增一行（未提交）

async function refreshKeys() {
  let data;
  try {
    data = await api("/api/keys");
  } catch (e) { return; }

  $("#keystat").textContent =
    `Key：可用 ${data.stats.available} / 共 ${data.stats.total}`;

  // 行内编辑 / 新增行的过程中不重画表格，否则用户输入会被冲掉
  if (KEY_EDIT_ID !== null || KEY_DRAFT) return;

  KEYS = data.keys;
  const valid = new Set(KEYS.map((k) => k.id));
  KEY_CHECKED = new Set([...KEY_CHECKED].filter((id) => valid.has(id)));
  renderKeys();
}

function renderKeys() {
  const tb = $("#keys-body");
  tb.innerHTML = "";

  KEYS.forEach((k) => tb.appendChild(keyRow(k)));
  if (KEY_DRAFT) tb.appendChild(draftRow());

  tb.querySelectorAll("input[data-ck]").forEach((cb) => {
    cb.onchange = () => {
      const id = Number(cb.dataset.ck);
      cb.checked ? KEY_CHECKED.add(id) : KEY_CHECKED.delete(id);
      updateKeySelection();
    };
  });

  tb.querySelectorAll("[data-edit]").forEach((b) => {
    b.onclick = () => { KEY_EDIT_ID = Number(b.dataset.edit); renderKeys(); };
  });
  tb.querySelectorAll("[data-cancel]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.cancel === "draft") KEY_DRAFT = false;
      else KEY_EDIT_ID = null;
      renderKeys();
    };
  });
  tb.querySelectorAll("[data-save]").forEach((b) => {
    b.onclick = () => guard(
      () => (b.dataset.save === "draft" ? saveDraft() : saveRow(Number(b.dataset.save))),
      "保存 Key"
    );
  });
  tb.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => guard(
      () => bulk("delete", [Number(b.dataset.del)], `确认删除 Key #${b.dataset.del}？`),
      "删除 Key"
    );
  });

  updateKeySelection();
}

function keyRow(k) {
  const tr = document.createElement("tr");
  const checked = KEY_CHECKED.has(k.id) ? "checked" : "";
  const ck = `<td><input type="checkbox" data-ck="${k.id}" ${checked}></td>`;

  if (KEY_EDIT_ID === k.id) {
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

function readRowFields(tr) {
  const out = {};
  tr.querySelectorAll("input[data-f]").forEach((i) => {
    out[i.dataset.f] = i.value.trim();
  });
  return out;
}

async function saveRow(id) {
  const tr = $(`#keys-body tr.editing input[data-f="api_key"]`).closest("tr");
  const body = readRowFields(tr);
  if (!body.api_key) { alert("API Key 不能为空"); return; }
  await api(`/api/keys/${id}`, { method: "PUT", body: JSON.stringify(body) });
  KEY_EDIT_ID = null;
  log(`Key #${id} 已更新`, "ok");
  await refreshKeys();
}

async function saveDraft() {
  const apiKey = $("#draft-key").value.trim();
  if (!apiKey) { alert("请填写 API Key"); return; }
  const name = $("#draft-name").value.trim();
  const r = await api("/api/keys", {
    method: "POST",
    body: JSON.stringify([{ api_key: apiKey, project_name: name }]),
  });
  KEY_DRAFT = false;
  (r.skipped || []).forEach((s) => log("跳过：" + s, "warn"));
  log(r.added ? `已新增 ${r.added} 条 Key` : "没有新增任何 Key", r.added ? "ok" : "warn");
  await refreshKeys();
}

function selectedIds() {
  return [...KEY_CHECKED];
}

function updateKeySelection() {
  const n = selectedIds().length;
  const count = $("#key-selcount");
  if (count) {
    count.textContent = `已选 ${n} 条`;
    count.classList.toggle("has-sel", n > 0);
  }
  const all = $("#key-selall");
  if (all) {
    all.checked = KEYS.length > 0 && n === KEYS.length;
    all.indeterminate = n > 0 && n < KEYS.length;
  }
  $$("[data-bulk]").forEach((b) => { b.disabled = n === 0; });
}

async function bulk(action, ids, confirmText) {
  const list = (ids || selectedIds()).slice();
  if (!list.length) return;
  if (confirmText && !confirm(confirmText.replace("{n}", list.length))) return;
  const r = await api("/api/keys/bulk", {
    method: "POST", body: JSON.stringify({ ids: list, action }),
  });
  const label = { enable: "启用", disable: "禁用", delete: "删除", reset_usage: "重置用量" }[action] || action;
  log(`已${label} ${r.affected}/${list.length} 条 Key`, "ok");
  KEY_CHECKED.clear();
  await refreshKeys();
}

$("#key-selall").onchange = () => {
  const on = $("#key-selall").checked;
  KEY_CHECKED = new Set(on ? KEYS.map((k) => k.id) : []);
  renderKeys();
};

$("#btn-key-add").onclick = () => {
  KEY_EDIT_ID = null;
  KEY_DRAFT = true;
  renderKeys();
  const el = $("#draft-key");
  if (el) el.focus();
};

$$("[data-bulk]").forEach((b) => {
  b.onclick = () => {
    const a = b.dataset.bulk;
    const confirmText = a === "delete" ? "确认删除选中的 {n} 条 Key？" : "";
    guard(() => bulk(a, null, confirmText), "批量操作");
  };
});

// ------------------------------------------------------------------ 新建任务

$("#btn-new").onclick = () => { $("#modal-new").hidden = false; };
$$("[data-close]").forEach((b) => b.onclick = () => { $("#" + b.dataset.close).hidden = true; });

// 模型下拉与思考等级连动
$("#new-model").onchange = () => applyModelThinking("#new-model", "#new-thinking");
$("#p-model").onchange = () => applyModelThinking("#p-model", "#p-thinking");

// 用浏览器原生文件选择器（安全限制拿不到真实路径，所以上传内容到后端落盘）
$("#new-srt-file").onchange = async () => {
  const file = $("#new-srt-file").files && $("#new-srt-file").files[0];
  if (!file) { PICKED_FILE = null; $("#new-srt-info").textContent = "尚未选择文件"; return; }
  PICKED_FILE = file;
  $("#new-srt-info").textContent = `已选择：${file.name}（${(file.size / 1024).toFixed(1)} KB）— 创建时会嵌入任务库`;
};

$("#btn-create").onclick = () => guard(async () => {
  if (!PICKED_FILE) { alert("请先选择字幕文件"); return; }

  const content = await PICKED_FILE.text();
  const up = await api("/api/upload-srt", {
    method: "POST",
    body: JSON.stringify({ filename: PICKED_FILE.name, content }),
  });

  const body = {
    input_file: up.path,
    params: {
      ENGINE: $("#new-engine").value,
      MODEL_NAME: $("#new-model").value,
      TARGET_LANGUAGE: $("#new-lang").value,
      THINKING_LEVEL: $("#new-thinking").value,
      BATCH_SIZE: Number($("#new-batch").value),
    },
    reflect_template: $("#new-reflect-tpl").value,
    custom_template: $("#new-custom-tpl").value,
  };
  const r = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });

  $("#modal-new").hidden = true;
  PICKED_FILE = null;
  $("#new-srt-file").value = "";
  $("#new-srt-info").textContent = "尚未选择文件";
  log(`任务已创建（字幕逐条入库：${up.filename}）。点上方「▶ 开始翻译」才会运行。`, "ok");
  await refreshJobs();
  await selectJob(r.job_id);
}, "创建任务");

// ------------------------------------------------------------------ 工具

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init().catch((e) => log("初始化失败：" + e.message, "error"));
