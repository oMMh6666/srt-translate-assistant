/** 入口：装配三个视图 + 统一定时器。 */

import { api } from "./api.js";
import { log } from "./log.js";
import { initSplitter } from "./splitter.js";
import { isViewActive, state } from "./state.js";
import { $, $$, guard } from "./util.js";
import { initKeys, refreshKeys } from "./view_keys.js";
import { initPrompts, refreshTemplateList } from "./view_prompts.js";
import {
  fillTaskSelects, initTasks, refreshJobs, refreshTemplates, tick as tasksTick,
} from "./view_tasks.js";

// 一套定时器管所有轮询（以前是 5s / 15s / 2.5s 三套各跑各的）
const TICK_MS = 5000;

async function tick() {
  await guard(() => refreshJobs(), "刷新任务列表");
  await guard(() => tasksTick(), "刷新详情");
  if (isViewActive("keys")) await guard(() => refreshKeys(), "刷新 Key");
}

async function init() {
  state.cfg = await api.config();
  fillTaskSelects(state.cfg);

  initTasks();
  initKeys();
  initPrompts();
  initSplitter();

  $("#btn-clearlog").onclick = () => { $("#logs").innerHTML = ""; };

  $$("#toptabs button").forEach((b) => {
    b.onclick = () => {
      $$("#toptabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      const v = b.dataset.view;
      $("#view-tasks").classList.toggle("active", v === "tasks");
      $("#view-prompts").classList.toggle("active", v === "prompts");
      $("#view-keys").classList.toggle("active", v === "keys");
      if (v === "keys") guard(() => refreshKeys(), "刷新 Key");
      if (v === "prompts") guard(() => refreshTemplateList(), "刷新模板");
    };
  });

  await refreshTemplates();
  await refreshJobs();
  await refreshKeys();

  setInterval(() => guard(tick, "定时刷新"), TICK_MS);
}

init().catch((e) => log("初始化失败：" + e.message, "error"));
