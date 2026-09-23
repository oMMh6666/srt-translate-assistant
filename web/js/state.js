/** 集中状态：避免全局变量散在各处。 */

export const state = {
  cfg: null,

  // 任务视图
  jobs: [],
  current: null,      // 当前任务 id
  detail: null,       // 当前任务详情
  runningJob: null,   // 全局唯一运行中的任务 id
  keyStats: { total: 0, available: 0 },
  pickedFile: null,   // 新建任务时选中的字幕（File 对象）
  pairsFollow: true,  // 跟随最新批次（滚到刚翻完的那批）
  pairsDone: null,    // 上次看到的已完成批次数（SSE 缺失时的兜底基准）

  // 翻译记录
  apiLogs: [],
  apiLogId: null,
  alogFollow: true,
  alogBusy: false,

  // Key 视图
  keys: [],
  keyChecked: new Set(),
  keyEditId: null,
  keyDraft: false,

  // 提示词模板视图
  templates: [],
  tplCurrent: "reflect.md",
  tplInitialized: false,
  tplDirty: false,
};

export function findJob(id) {
  if (!id) return null;
  return state.jobs.find((j) => j.id === id) || null;
}

export function isViewActive(name) {
  const el = document.querySelector(`#view-${name}`);
  return !!el && el.classList.contains("active");
}
