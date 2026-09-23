/** 可拖动分隔条：左栏宽度 / 底部日志区高度（写进 localStorage，双击恢复默认）。 */

import { $, $$ } from "./util.js";

const KEY_W = "ui.sidebar.width";
const KEY_H = "ui.log.height";
const DEF_W = 280;
const DEF_H = 200;

let sideW = DEF_W;
let logH = DEF_H;

const clampW = (w) =>
  Math.min(Math.max(Math.round(w), 180), Math.max(window.innerWidth - 360, 180));
const clampH = (h) =>
  Math.min(Math.max(Math.round(h), 80), Math.max(window.innerHeight - 260, 120));

function setSideWidth(w) {
  sideW = w;
  document.documentElement.style.setProperty("--side-w", `${w}px`);
}

function setLogHeight(h) {
  logH = h;
  document.documentElement.style.setProperty("--log-h", `${h}px`);
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

export function initSplitter() {
  const savedW = Number(localStorage.getItem(KEY_W));
  if (savedW >= 180) setSideWidth(clampW(savedW));
  const savedH = Number(localStorage.getItem(KEY_H));
  if (savedH >= 80) setLogHeight(clampH(savedH));

  // 竖向分隔条：左右拖，改左栏宽度
  $$("[data-split='side']").forEach((el) => {
    el.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      const left = el.parentElement.getBoundingClientRect().left;
      beginDrag(
        el,
        "resizing",
        (e) => setSideWidth(clampW(e.clientX - left)),
        () => localStorage.setItem(KEY_W, String(sideW)),
      );
    });
    el.addEventListener("dblclick", () => {
      setSideWidth(clampW(DEF_W));
      localStorage.setItem(KEY_W, String(sideW));
    });
  });

  // 横向分隔条：上下拖，改日志区高度（日志区在分隔条下方，往上拖 = 变高）
  const logBar = $("[data-split='log']");
  if (logBar) {
    logBar.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      const bottom = document.body.getBoundingClientRect().bottom;
      beginDrag(
        logBar,
        "resizing-v",
        (e) => setLogHeight(clampH(bottom - e.clientY)),
        () => localStorage.setItem(KEY_H, String(logH)),
      );
    });
    logBar.addEventListener("dblclick", () => {
      setLogHeight(clampH(DEF_H));
      localStorage.setItem(KEY_H, String(logH));
    });
  }
}
